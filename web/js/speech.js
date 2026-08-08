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
// - an utterance is committed to the note when its run ends; Android is
//   left to end runs itself (each forced end costs a system chime), with a
//   long watchdog for runs that hang open silently
// - "no-speech" errors are routine — the restart loop absorbs them
// - dictation NEVER gives up while it's switched on: empty runs restart
//   with growing spacing (fewer chimes in silence), speech snaps it back
//   to fast restarts, and only real mic errors or the user end the session

import { noteTitleFrom } from "./util.js";

const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
const $ = id => document.getElementById(id);
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

const RUN_WATCHDOG_MS = 15000; // a run silent this long is hung — recycle it
const AUTO_STOP_MS = 20000;    // silence that ends the session
// Hands-free is for talking with the phone out of sight, so a pause to think
// must not end the session the way a 20-second gap does on screen.
const HANDS_FREE_STOP_MS = 300000;
const LIVE_PUSH_MS = 1500;     // at most one live upload per this, newest wins
const METER_BARS = 24;

let app = null;
let rec = null;
let active = false;          // user wants to be dictating
let committed = "";          // finished utterances
let utterance = "";          // best hypothesis of the current run
let bound = false;
let meterTimer = null;
let watchdogTimer = null;
let restartTimer = null;
let idleTimer = null;
let wakeLock = null;
let silentRuns = 0;          // consecutive runs that heard nothing
let runHadText = false;
let liveNoteId = null;       // the note this session is filling in, once made
let livePushTimer = null;
let livePushing = false;     // one upload in flight
let livePushAgain = false;   // …and newer text arrived while it was
let livePush = Promise.resolve();   // that upload, so Save/Discard can wait
let liveEpoch = 0;           // bumped by discard/reset — voids in-flight pushes
let handsFree = false;       // opened by voice: speak state, don't time out fast
// NOTE: no page-held getUserMedia keep-alive stream here. Holding the mic
// used to silence Chrome's per-run chime, but Android Chrome now refuses to
// feed SpeechRecognition while the page owns the mic — runs hear nothing,
// no-speech restarts loop forever and dictation looks dead. Recognition
// must have the mic to itself; the chime per phrase is the accepted cost.

export function micAvailable() { return !!SR; }

export function openMic(appInstance, opts = {}) {
  app = appInstance;
  if (!bound) { bind(); bound = true; }
  render();
  // Launched hands-free (voice, or the "Dictate" launcher shortcut): the screen
  // is in a pocket, so every state change has to be audible instead of visible.
  if (opts.handsFree && !active) {
    handsFree = true;
    say("Listening", () => { if (!active) start(); });
  }
}

// ---------------------------------------------------------------------------
// Spoken status — the whole hands-free flow rests on this. An installed PWA is
// granted autoplay by Chrome, so this is audible over Bluetooth without a tap;
// in a browser tab it may be silently dropped, which costs nothing.
//
// Never speak while a run is live: the recogniser has the mic to itself and
// would happily transcribe our own voice back into the note. Callers that are
// about to start dictating pass `then` and start from there.
function say(text, then) {
  const synth = window.speechSynthesis;
  if (!synth || !text) { then?.(); return; }
  let done = false;
  const go = () => { if (!done) { done = true; then?.(); } };
  try {
    synth.cancel();
    const u = new SpeechSynthesisUtterance(text);
    u.lang = "en-GB";
    u.rate = 1.05;
    u.onend = go;
    u.onerror = go;
    synth.speak(u);
    // Android sometimes fires neither event (no voice pack, output switching
    // to the buds mid-utterance). Never let the session hang on it.
    setTimeout(go, 400 + text.length * 90);
  } catch { go(); }
}

// ---------------------------------------------------------------------------

function bind() {
  const meter = $("micMeter");
  for (let i = 0; i < METER_BARS; i++) meter.append(document.createElement("i"));

  // A tap means the screen is in hand — drop back to the on-screen behaviour
  // (short silence timeout, no spoken status) for the rest of the session.
  $("micMainBtn").addEventListener("click", () => {
    handsFree = false;
    active ? stop() : start();
  });
  $("micSaveBtn").addEventListener("click", () => save());
  $("micDiscardBtn").addEventListener("click", async () => {
    await discardLive();                   // it's already on the desktop — pull it back
    reset();
    render();
  });

  addEventListener("hashchange", () => {
    if (location.hash === "#/mic") return;
    handsFree = false;
    if (active) stop();
  });
  document.addEventListener("visibilitychange", async () => {
    if (!document.hidden && active && wakeLock === null) acquireWake();
  });
}

function start() {
  if (!SR) return;
  active = true;
  silentRuns = 0;
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
  runHadText = false;
  armRunWatchdog();

  rec.onresult = e => {
    let text = "";
    for (let i = 0; i < e.results.length; i++) {
      text = fold(text, e.results[i][0].transcript.trim());
    }
    text = text.trim();
    if (text && text !== utterance) {      // only real growth rearms timers
      utterance = text;
      runHadText = true;
      silentRuns = 0;
      armRunWatchdog();
      armIdleTimer();
      paint();
    }
  };

  rec.onerror = e => {
    if (e.error === "audio-capture") {
      active = false;
      const msg = "Can't reach the microphone — is another app using it?";
      app?.toast(msg, 3500);
      if (handsFree) { handsFree = false; say(msg); }
      finishRun();
      render();
      return;
    }
    if (e.error === "not-allowed" || e.error === "service-not-allowed") {
      active = false;
      app?.toast("Microphone blocked — allow it in Chrome's site settings", 3500);
      if (handsFree) {
        handsFree = false;
        say("The microphone is blocked. Open DictationMic and allow it.");
      }
      finishRun();
      render();
    }
    // "no-speech"/"aborted"/"network" fall through to onend's restart
  };

  rec.onend = () => {
    clearTimeout(watchdogTimer);
    commitUtterance();
    if (!active) { render(); return; }
    // Still live: ALWAYS come back for the next utterance. Runs that heard
    // something restart at once; empty ones space out (each restart is a
    // chime on Android, and silence doesn't need a fast loop).
    silentRuns = runHadText ? 0 : silentRuns + 1;
    scheduleRestart(runHadText ? 150 : Math.min(8000, 300 * 2 ** silentRuns));
  };

  rec.start();
}

function scheduleRestart(delay) {
  clearTimeout(restartTimer);
  restartTimer = setTimeout(() => {
    if (!active) return;
    try { startRun(); }
    catch { scheduleRestart(600); }        // engine still busy — retry, never die
  }, delay);
}

function armRunWatchdog() {
  // Android sometimes leaves a run open that will never hear anything —
  // recycle it so the session can't wedge. Normal runs end themselves first.
  clearTimeout(watchdogTimer);
  watchdogTimer = setTimeout(() => { try { rec?.stop(); } catch { } },
                             RUN_WATCHDOG_MS);
}

// The only way to finish a note when the phone is in a pocket. Matched on the
// tail of a finished utterance so it can't fire off a word said mid-sentence,
// and stripped out so it never lands in the note.
const STOP_PHRASE = /[\s,.]*\b(?:end note|save note|stop dictation|finish note)\b[\s,.!]*$/i;

function commitUtterance() {
  if (utterance) {
    let ending = false;
    if (handsFree && STOP_PHRASE.test(utterance)) {
      utterance = utterance.replace(STOP_PHRASE, "");
      ending = true;
    }
    committed = fold(committed, utterance);
    utterance = "";
    paint();
    if (ending) { setTimeout(endHandsFreeOrStop, 0); return; }
    // Nothing used to leave the phone until the Save tap, so the desktop pill
    // was a whole dictation plus a decision behind. Every finished utterance
    // now goes up as it lands and the pill fills in live. When the session is
    // ending, don't sit on the last one — send it at once.
    if (active) schedulePublish(); else publishLive();
  }
}

// One upload per LIVE_PUSH_MS, always carrying the newest text: a burst of
// short utterances collapses into a single push rather than queueing.
function schedulePublish() {
  if (livePushTimer) return;
  livePushTimer = setTimeout(() => {
    livePushTimer = null;
    publishLive();
  }, LIVE_PUSH_MS);
}

// Returns the upload in flight so Save and Discard can wait on it rather than
// racing it — a Save that overtook a create would make a second note for the
// same dictation, on the desktop as well as here.
function publishLive() {
  if (livePushing) { livePushAgain = true; return livePush; }
  livePush = doPublish();
  return livePush;
}

async function doPublish() {
  const text = committed.replace(/\s+/g, " ").trim();
  if (!text || !app?.adapter) return;
  // Discard can land while this upload is in flight. It can't cancel the
  // request, so it bumps the epoch instead and we clean up after ourselves —
  // otherwise a create that finishes late resurrects a note the user has
  // already thrown away, on the desktop as well as here.
  const epoch = liveEpoch;
  livePushing = true;
  try {
    if (liveNoteId) {
      await app.adapter.update(liveNoteId, text);
    } else {
      const made = await app.adapter.create({ body: text });
      if (epoch !== liveEpoch) {
        await app.adapter.remove(made.id).catch(() => {});
        return;
      }
      liveNoteId = made.id;
    }
    if (epoch !== liveEpoch) return;
    app.notes = await app.adapter.list();
    app.renderList();
  } catch {
    // offline, or the desktop is asleep — the outbox is holding it and the
    // next utterance (or the Save) tries again. Never interrupt dictation.
  } finally {
    livePushing = false;
    if (livePushAgain) {
      livePushAgain = false;
      if (epoch === liveEpoch) schedulePublish();
    }
  }
}

// Throw away the note this session has been building — used by Discard, which
// has to reach the desktop too now that the note got there before the decision.
async function discardLive() {
  clearTimeout(livePushTimer);
  livePushTimer = null;
  liveEpoch++;                  // any upload still in flight now cleans up
  await livePush.catch(() => {});   // …once it has actually landed
  const id = liveNoteId;
  liveNoteId = null;
  if (!id) return;
  try { await app.adapter.remove(id); } catch { /* outbox retries the delete */ }
  try {
    app.notes = await app.adapter.list();
    app.renderList();
  } catch { /* list refresh is cosmetic */ }
}

function stop() {
  active = false;
  try { rec?.stop(); } catch { }           // onend commits the last utterance
  finishRun();
  render();
}

function finishRun() {
  clearTimeout(watchdogTimer);
  clearTimeout(restartTimer);
  clearTimeout(idleTimer);
  stopMeter();
  releaseWake();
  app?.setBrandLive(false);
}

function reset() {
  committed = "";
  utterance = "";
  liveNoteId = null;          // next dictation starts a note of its own
  liveEpoch++;                // and a late push can't attach itself to it
  paint();
}

async function save({ spoken = false } = {}) {
  commitUtterance();          // session is over, so this kicks a push straight off
  clearTimeout(livePushTimer);
  livePushTimer = null;
  await livePush.catch(() => {});
  const text = committed.replace(/\s+/g, " ").trim();
  if (!text) {
    await discardLive();
    reset();
    render();
    if (spoken) say("Nothing to save");
    return;
  }
  try {
    if (liveNoteId) {
      await app.adapter.update(liveNoteId, text);
      // The title was derived from the first utterance, which may have been
      // only a word or two. Settle it on the finished wording — rename pushes
      // title and body together, so this is one op, not two.
      const title = noteTitleFrom(text);
      const cur = await app.adapter.get(liveNoteId).catch(() => null);
      if (cur && cur.title !== title) await app.adapter.rename(liveNoteId, title);
    } else {
      await app.adapter.create({ body: text });
    }
    liveNoteId = null;
    reset();
    app.notes = await app.adapter.list();
    app.renderList();
    app.toast("Saved — it'll appear on your computer");
    if (spoken) say(`Saved. ${wordCount(text)} words, on your computer.`);
    location.hash = "#/";
  } catch (e) {
    app.toast(e.message || "Couldn't save");
    if (spoken) say("Couldn't save that — it's still on the phone");
  }
}

const wordCount = s => (s.match(/\S+/g) || []).length;

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
  idleTimer = setTimeout(
    () => { if (active) endHandsFreeOrStop(); },
    handsFree ? HANDS_FREE_STOP_MS : AUTO_STOP_MS);
}

// With no screen there is no Save button to reach for, so a hands-free session
// that ends — by the spoken stop phrase or by running out of silence — commits
// itself and says so.
function endHandsFreeOrStop() {
  stop();
  if (!handsFree) return;
  handsFree = false;
  save({ spoken: true });
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
