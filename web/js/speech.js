// Voice recorder for the phone. No Web Speech API here any more: Chrome's
// recognition opens and closes the mic for every phrase (chiming each
// time) and its continuous mode is broken on Android (crbug 40324711).
// So the phone doesn't transcribe at all — it records one unbroken,
// silent audio track. The note syncs with the audio attached and the
// computer's Whisper (the same engine as desktop dictation) turns it into
// text, replacing the placeholder note as soon as a laptop sees it.

const $ = id => document.getElementById(id);
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

const AUTO_STOP_MS = 60000;          // this much silence ends the recording
const MAX_SESSION_MS = 10 * 60000;   // hard cap per voice note
const METER_BARS = 24;

let app = null;
let bound = false;
let recorder = null;
let chunks = [];
let recording = false;
let startedAt = 0;
let lastLoud = 0;
let meterTimer = null;
let wakeLock = null;
let micStream = null;
let audioCtx = null;
let analyser = null;
let meterData = null;

export function micAvailable() {
  return !!(navigator.mediaDevices?.getUserMedia && window.MediaRecorder);
}

export function openMic(appInstance) {
  app = appInstance;
  if (!bound) { bind(); bound = true; }
  render();
}

// ---------------------------------------------------------------------------

function bind() {
  const meter = $("micMeter");
  for (let i = 0; i < METER_BARS; i++) meter.append(document.createElement("i"));

  $("micMainBtn").addEventListener("click", () => recording ? stop() : start());

  addEventListener("hashchange", () => {
    if (location.hash !== "#/mic" && recording) stop();   // saves, not discards
  });
  document.addEventListener("visibilitychange", async () => {
    if (!document.hidden && recording && wakeLock === null) acquireWake();
  });
}

function pickMime() {
  for (const m of ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"]) {
    if (MediaRecorder.isTypeSupported(m)) return m;
  }
  return "";
}

async function start() {
  try {
    micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    app?.toast("Microphone blocked — allow it in your browser settings", 3500);
    return;
  }
  try {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const src = audioCtx.createMediaStreamSource(micStream);
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 256;
    src.connect(analyser);
    meterData = new Uint8Array(analyser.frequencyBinCount);
  } catch { analyser = null; }

  const mime = pickMime();
  const opts = { audioBitsPerSecond: 32000 };
  if (mime) opts.mimeType = mime;
  recorder = new MediaRecorder(micStream, opts);
  chunks = [];
  recorder.ondataavailable = e => { if (e.data && e.data.size) chunks.push(e.data); };
  recorder.onstop = onRecorded;
  recorder.start(1000);                 // chunk every second: nothing is lost
  recording = true;                     // even if the tab dies mid-recording
  startedAt = lastLoud = Date.now();
  acquireWake();
  startMeter();
  render();
}

function stop() {
  if (!recording) return;
  recording = false;
  try { recorder?.stop(); } catch { onRecorded(); }
  render();
}

async function onRecorded() {
  const mime = (recorder && recorder.mimeType) || "audio/webm";
  stopMeter();
  releaseMic();
  releaseWake();
  const blob = new Blob(chunks, { type: mime });
  chunks = [];
  render();
  if (blob.size < 2000) {
    app?.toast("Too short — nothing saved", 2000);
    return;
  }
  try {
    const audio = await blobB64(blob);
    await app.adapter.createVoice({ audio, audioMime: mime });
    app.notes = await app.adapter.list();
    app.renderList();
    app.toast("Saved — your computer will turn it into text");
    location.hash = "#/";
  } catch (e) {
    app?.toast((e && e.message) || "Couldn't save the recording", 3000);
  }
}

function blobB64(blob) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",")[1] || "");
    r.onerror = () => reject(new Error("Couldn't read the recording"));
    r.readAsDataURL(blob);
  });
}

// ---------------------------------------------------------------------------

function fmtElapsed() {
  const s = Math.floor((Date.now() - startedAt) / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function render() {
  $("micState").textContent = recording ? `Recording ${fmtElapsed()}` : "Ready";
  $("micMainBtn").classList.toggle("live", recording);
  $("micMeter").classList.toggle("live", recording);
  $("micSaveBtn").hidden = true;        // stopping saves automatically
  $("micDiscardBtn").hidden = true;
  $("micFinal").textContent = "";
  $("micInterim").textContent = recording
    ? "Recording — tap the button to finish. The text appears once your computer has written it up."
    : "Tap the mic and talk. It records silently — no beeps — and your computer turns it into text.";
  app?.setBrandLive(recording);
}

// the meter drives three things: the bars, the silence auto-stop and the
// on-screen timer — so it always runs while recording, even reduced-motion
function startMeter() {
  const bars = $("micMeter").children;
  let phase = 0;
  meterTimer = setInterval(() => {
    const now = Date.now();
    let level = 0;
    if (analyser) {
      analyser.getByteTimeDomainData(meterData);
      let sum = 0;
      for (let i = 0; i < meterData.length; i++) {
        const v = (meterData[i] - 128) / 128;
        sum += v * v;
      }
      level = Math.min(1, Math.sqrt(sum / meterData.length) * 4);
      if (level > 0.06) lastLoud = now;
    } else {
      lastLoud = now;                   // no analyser: never auto-stop
    }
    if (recording) {
      $("micState").textContent = `Recording ${fmtElapsed()}`;
      if (now - lastLoud > AUTO_STOP_MS || now - startedAt > MAX_SESSION_MS) {
        stop();
        return;
      }
    }
    if (!reducedMotion) {
      phase += 0.6;
      for (let i = 0; i < bars.length; i++) {
        const wave = Math.abs(Math.sin(phase * 0.9 + i * 0.7));
        bars[i].style.height = `${4 + level * (14 + wave * 26)}px`;
      }
    }
  }, 100);
}

function stopMeter() {
  clearInterval(meterTimer);
  meterTimer = null;
  for (const b of $("micMeter").children) b.style.height = "";
}

function releaseMic() {
  try { micStream?.getTracks().forEach(t => t.stop()); } catch { }
  try { audioCtx?.close(); } catch { }
  micStream = audioCtx = analyser = meterData = null;
}

async function acquireWake() {
  try { wakeLock = await navigator.wakeLock?.request("screen"); }
  catch { wakeLock = null; }
}

function releaseWake() {
  try { wakeLock?.release(); } catch { }
  wakeLock = null;
}
