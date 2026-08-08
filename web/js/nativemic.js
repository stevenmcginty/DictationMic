// The mic screen when the app is running inside the Android shell.
//
// Drop-in replacement for speech.js: same exports, same DOM, same buttons —
// main.js picks between the two by whether the native bridge is there. What
// changes is who does the listening. In a browser tab that's the Web Speech
// API, which stops the moment Chrome backgrounds the page and plays Android's
// recognition chime before every phrase. In the shell it's a microphone
// foreground service, which keeps going with the screen off and owns the note
// end to end — creating it, streaming it to the laptop as you talk, and saving
// it when the session ends.
//
// So this file listens rather than records: the shell pushes recorder state in
// (MainActivity polls the service and calls window.DictationMicShell.state),
// and the screen mirrors it. The transcript you see here is the same text the
// notification and the laptop's pill are showing.

const N = () => window.DictationMicNative;
const $ = id => document.getElementById(id);
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

const METER_BARS = 24;

let app = null;
let bound = false;
let meterTimer = null;
let last = { running: false, finals: "", partial: "", level: 0, savedAt: 0 };
let seenSavedAt = 0;
let handsFree = false;

export function micAvailable() { return !!N(); }

export function openMic(appInstance, opts = {}) {
  app = appInstance;
  if (!bound) { bind(); bound = true; }
  pull();                                  // redraw against a session already running
  if ((opts.handsFree || opts.autoStart) && !last.running) {
    handsFree = !!opts.handsFree;
    start();
  }
}

function bind() {
  const meter = $("micMeter");
  for (let i = 0; i < METER_BARS; i++) meter.append(document.createElement("i"));

  $("micMainBtn").addEventListener("click", () => {
    if (last.running) { handsFree = false; stop(); } else { start(); }
  });
  // The service saves the note itself the moment a session ends, so there is
  // nothing left for these to do by the time they could be pressed. They stay
  // hidden; the header's back button is the way out.
  $("micSaveBtn").hidden = true;
  $("micDiscardBtn").hidden = true;

  addEventListener("hashchange", () => {
    // Leaving the screen is NOT a reason to stop here — walking away mid
    // dictation is the entire point of the shell. Only an explicit stop ends it.
    if (location.hash !== "#/mic") handsFree = false;
  });

  // The shell pushes every change in recorder state through here. Merged, not
  // assigned: shellshare.js hangs its own hook off the same object and either
  // file can be the one that runs first.
  window.DictationMicShell = Object.assign(window.DictationMicShell || {}, {
    state(snapshot) { apply(snapshot); },
  });
}

function pull() {
  try { apply(JSON.parse(N().dictationState())); }
  catch { /* bridge not ready — the next push repaints */ }
}

function start() {
  try { N().startDictation(handsFree); }
  catch { app?.toast("Couldn't start the recorder"); }
  // Optimistic: the service takes a moment to come up and the first real
  // snapshot lands ~200ms later. Without this the button doesn't answer a tap.
  apply({ ...last, running: true, finals: "", partial: "" });
}

function stop() {
  try { N().stopDictation(); } catch { }
  apply({ ...last, running: false, partial: "" });
}

function apply(s) {
  const wasRunning = last.running;
  last = {
    running: !!s.running,
    finals: s.finals || "",
    partial: s.partial || "",
    level: Number(s.level) || 0,
    savedAt: Number(s.savedAt) || 0,
  };
  if (last.savedAt && last.savedAt !== seenSavedAt) {
    seenSavedAt = last.savedAt;
    onSaved();
  }
  if (last.running && !meterTimer) startMeter();
  if (!last.running && meterTimer) stopMeter();
  if (wasRunning !== last.running) app?.setBrandLive(last.running);
  render();
}

// The service has written the note and pushed it. Pull the list back so the
// dictation is on screen the moment you look at the phone again.
async function onSaved() {
  handsFree = false;
  try {
    app.notes = await app.adapter.list();
    app.renderList();
  } catch { /* the adapter's own listener will catch up */ }
  if (location.hash === "#/mic") {
    app?.toast("Saved — it'll appear on your computer");
    location.hash = "#/";
  }
}

function render() {
  const text = (last.finals + " " + last.partial).trim();
  $("micState").textContent = last.running ? "Listening…" : (text ? "Paused" : "Ready");
  $("micMainBtn").classList.toggle("live", last.running);
  $("micMeter").classList.toggle("live", last.running);
  $("micFinal").textContent = last.finals ? last.finals + " " : "";
  $("micInterim").textContent = last.partial || "";
  const t = $("micTranscript");
  t.scrollTop = t.scrollHeight;
}

// Same shape as speech.js's meter, but driven by the real microphone level the
// service reports rather than a synthetic wave.
function startMeter() {
  if (reducedMotion) return;
  const bars = $("micMeter").children;
  let phase = 0;
  meterTimer = setInterval(() => {
    phase += 0.6;
    const gain = 0.35 + last.level * 1.6;
    for (let i = 0; i < bars.length; i++) {
      const wave = Math.sin(phase + i * 0.55) * Math.sin(phase * 0.33 + i);
      bars[i].style.height = `${5 + Math.abs(wave) * 30 * gain + Math.random() * 4}px`;
    }
  }, 100);
}

function stopMeter() {
  clearInterval(meterTimer);
  meterTimer = null;
  for (const b of $("micMeter").children) b.style.height = "";
}
