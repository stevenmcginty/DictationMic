// Web Speech dictation for the phone.
//
// Chrome on Android officially breaks continuous mode (crbug 40324711):
// results arrive as ever-growing concatenations of the whole session, runs
// end after nearly every phrase, and old text is re-delivered after silent
// restarts. iOS Safari has its own version of the same rot. So:
// - continuous mode is never used — every utterance is its own one-shot
//   recognition run, restarted while dictation is live
// - a run's text is rebuilt from scratch on every event and folded
//   (grow / replace / skip — never blind-append), shown as a scratch line
// - an utterance is committed to the note only when its run ends; a ~750ms
//   no-new-words timer ends the run rather than trusting isFinal
// - "no-speech" errors are routine — the restart loop absorbs them

const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
const $ = id => document.getElementById(id);
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

const COMMIT_MS = 750;         // silence that ends an utterance
const AUTO_STOP_MS = 60000;    // silence that ends the session
const METER_BARS = 24;

let app = null;
let rec = null;
let active = false;          // user wants to be dictating
let committed = "";          // finished utterances
let utterance = "";          // best hypothesis of the current run
let bound = false;
let meterTimer = null;
let commitTimer = null;
let idleTimer = null;
let wakeLock = null;
let restartCount = 0;
let lastRestart = 0;
// NOTE: no page-held getUserMedia keep-alive stream here. Holding the mic
// used to silence Chrome's per-run chime, but Android Chrome now refuses to
// feed SpeechRecognition while the page owns the mic — runs hear nothing,
// no-speech restarts loop forever and dictation looks dead. Recognition
// must have the mic to itself; the chime per phrase is the accepted cost.

export function micAvailable() { return !!SR; }

export function openMic(appInstance) {
  app = appInstance;
  if (!bound) { bind(); bound = true; }
  render();
}

// ---------------------------------------------------------------------------

function bind() {
  const meter = $("micMeter");
  for (let i = 0; i < METER_BARS; i++) meter.append(document.createElement("i"));

  $("micMainBtn").addEventListener("click", () => active ? stop() : start());
  $("micSaveBtn").addEventListener("click", save);
  $("micDiscardBtn").addEventListener("click", () => { reset(); render(); });

  addEventListener("hashchange", () => {
    if (location.hash !== "#/mic" && active) stop();
  });
  document.addEventListener("visibilitychange", async () => {
    if (!document.hidden && active && wakeLock === null) acquireWake();
  });
}

function start() {
  if (!SR) return;
  active = true;
  restartCount = 0;
  startRun();
  acquireWake();
  startMeter();
  armIdleTimer();
  render();
}

// Merge a new hypothesis into what we already have. Handles every observed
// misbehaviour: cumulative re-delivery ("so", "so if", "so if I"…) grows in
// place, duplicated tails are skipped, genuinely new text is appended.
const norm = s => s.toLowerCase().replace(/\s+/g, " ").trim();

function fold(acc, t) {
  if (!t) return acc;
  const a = norm(acc), b = norm(t);
  if (!a) return t + " ";
  if (b.startsWith(a)) return t + " ";     // cumulative: new text extends old
  if (a.endsWith(b)) return acc;           // duplicate re-delivery of the tail
  return acc.trimEnd() + " " + t + " ";
}

function startRun() {
  rec = new SR();
  rec.continuous = false;                  // the only mode Android honours
  rec.interimResults = true;
  rec.lang = "en-GB";

  rec.onresult = e => {
    let text = "";
    for (let i = 0; i < e.results.length; i++) {
      text = fold(text, e.results[i][0].transcript.trim());
    }
    text = text.trim();
    if (text && text !== utterance) {      // only real growth rearms timers
      utterance = text;
      armCommitTimer();
      armIdleTimer();
      paint();
    }
  };

  rec.onerror = e => {
    if (e.error === "audio-capture") {
      active = false;
      app?.toast("Can't reach the microphone — is another app using it?", 3500);
      finishRun();
      render();
      return;
    }
    if (e.error === "not-allowed" || e.error === "service-not-allowed") {
      active = false;
      app?.toast("Microphone blocked — allow it in Chrome's site settings", 3500);
      finishRun();
      render();
    }
    // "no-speech"/"aborted"/"network" fall through to onend's restart
  };

  rec.onend = () => {
    commitUtterance();
    if (!active) { render(); return; }
    // still live: restart for the next utterance, but not in a tight loop
    const now = Date.now();
    restartCount = now - lastRestart < 700 ? restartCount + 1 : 0;
    lastRestart = now;
    if (restartCount > 8) {
      active = false;
      app?.toast("Speech recognition keeps stopping — try again", 3000);
      finishRun();
      render();
      return;
    }
    setTimeout(() => { if (active) { try { startRun(); } catch { } } }, 120);
  };

  rec.start();
}

function commitUtterance() {
  clearTimeout(commitTimer);
  if (utterance) {
    committed = fold(committed, utterance);
    utterance = "";
    paint();
  }
}

function armCommitTimer() {
  clearTimeout(commitTimer);
  // no new words for COMMIT_MS: end the run — onend commits and restarts
  commitTimer = setTimeout(() => { try { rec?.stop(); } catch { } }, COMMIT_MS);
}

function stop() {
  active = false;
  clearTimeout(commitTimer);
  try { rec?.stop(); } catch { }           // onend commits the last utterance
  finishRun();
  render();
}

function finishRun() {
  clearTimeout(commitTimer);
  clearTimeout(idleTimer);
  stopMeter();
  releaseWake();
  app?.setBrandLive(false);
}

function reset() {
  committed = "";
  utterance = "";
  clearTimeout(commitTimer);
  paint();
}

async function save() {
  commitUtterance();
  const text = committed.replace(/\s+/g, " ").trim();
  if (!text) { reset(); render(); return; }
  try {
    await app.adapter.create({ body: text });
    reset();
    app.notes = await app.adapter.list();
    app.renderList();
    app.toast("Saved — it'll appear on your computer");
    location.hash = "#/";
  } catch (e) {
    app.toast(e.message || "Couldn't save");
  }
}

// ---------------------------------------------------------------------------

function paint() {
  const done = committed.replace(/\s+/g, " ").trim();
  $("micFinal").textContent = done ? done + " " : "";
  $("micInterim").textContent = utterance || "";
  const t = $("micTranscript");
  t.scrollTop = t.scrollHeight;
}

function render() {
  const hasText = !!(committed + utterance).trim();
  $("micState").textContent = active ? "Listening…" : (hasText ? "Paused" : "Ready");
  $("micMainBtn").classList.toggle("live", active);
  $("micMeter").classList.toggle("live", active);
  $("micSaveBtn").hidden = active || !hasText;
  $("micDiscardBtn").hidden = active || !hasText;
  app?.setBrandLive(active);
  paint();
}

function armIdleTimer() {
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => { if (active) stop(); }, AUTO_STOP_MS);
}

function startMeter() {
  if (reducedMotion) return;
  const bars = $("micMeter").children;
  let phase = 0;
  meterTimer = setInterval(() => {
    phase += 0.6;
    for (let i = 0; i < bars.length; i++) {
      const wave = Math.sin(phase + i * 0.55) * Math.sin(phase * 0.33 + i);
      bars[i].style.height = `${5 + Math.abs(wave) * 30 + Math.random() * 6}px`;
    }
  }, 100);
}

function stopMeter() {
  clearInterval(meterTimer);
  meterTimer = null;
  for (const b of $("micMeter").children) b.style.height = "";
}

async function acquireWake() {
  try { wakeLock = await navigator.wakeLock?.request("screen"); }
  catch { wakeLock = null; }
}

function releaseWake() {
  try { wakeLock?.release(); } catch { }
  wakeLock = null;
}
