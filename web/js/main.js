// Boot: pick the data adapter by origin, then hand over to the App.
//   127.0.0.1 / localhost  -> LocalAdapter (DictationMic.exe, notes\ folder)
//   anywhere else          -> FirebaseAdapter (IndexedDB + cloud, PWA)

import { App } from "./ui.js";

const $ = id => document.getElementById(id);
const isLocal = ["127.0.0.1", "localhost"].includes(location.hostname);

// Bumped with every deploy, shown under the email in the account popup. A
// phone runs whatever the service worker last cached, which is not necessarily
// what was last deployed — so "have you actually got the fix yet" was a
// guess. Now it's a number you can read off the screen.
const BUILD = 32;

function grabToken() {
  // the pill opens us as /#t=<per-run token>; keep it for this tab only
  const m = location.hash.match(/^#t=([\w-]+)/);
  if (m) {
    sessionStorage.setItem("dictmic-token", m[1]);
    history.replaceState(null, "", "#/");
  }
  return sessionStorage.getItem("dictmic-token") || "";
}

// Hosted only: tap the sync capsule to see whose account this is / sign out.
async function wireAccountPop() {
  const { email, signOut } = await import("./auth.js");
  const pop = $("accountPop");
  $("statusCapsule").addEventListener("click", e => {
    e.stopPropagation();
    $("accountEmail").textContent = email() || "(unknown)";
    $("accountBuild").textContent =
      `build ${BUILD}` + (shell() ? ` · app ${nativeVersion()}` : "");
    pop.hidden = !pop.hidden;
  });
  $("signOutBtn").addEventListener("click", () => {
    signOut();
    // The shell's recorder holds its own copy of the token — clear that too, or
    // dictations would keep uploading to an account the page has forgotten.
    if (shell()) { try { window.DictationMicNative.signOut(); } catch { } }
    location.reload();          // boot() shows the sign-in screen again
  });
  document.addEventListener("click", e => {
    if (!pop.hidden && !pop.contains(e.target)) pop.hidden = true;
  });
}

// Screenshots shared in from Android's share sheet (sw.js stashed them in
// the "dictmic-shared" cache). Claim each entry (delete) as it's read so a
// mid-boot reload — e.g. a new SW taking over — can never save it twice.
async function drainSharedFiles(app) {
  if (!("caches" in window)) return;
  try {
    const cache = await caches.open("dictmic-shared");
    const files = [];
    for (const req of await cache.keys()) {
      const res = await cache.match(req);
      await cache.delete(req);
      if (!res) continue;
      const blob = await res.blob();
      const name = decodeURIComponent(res.headers.get("X-Shared-Name") || "");
      files.push(new File([blob], name, { type: blob.type || "image/png" }));
    }
    if (files.length) await app.saveAnyFiles(files);
  } catch { /* never block boot on a share drop */ }
}

// How often the page is allowed to ask the network whether sw.js has changed.
// registration.update() is a real request for the file, and the triggers below
// fire every time the app comes back to the front — someone flicking between
// their notes and a chat would otherwise fetch it dozens of times a minute.
// Fifteen minutes is short enough that a deploy reaches a phone that gets
// picked up a few times an hour, and long enough that the traffic is a rounding
// error next to the sync stream that's already running.
const UPDATE_CHECK_MS = 15 * 60 * 1000;

let swReg = null;
let lastUpdateCheck = 0;

// The browser only looks for a new sw.js when register() is called or on a
// navigation. An installed PWA or the Android shell gets *resumed* rather than
// relaunched, so neither ever happens — the app can sit on a months-old version
// having never once asked. This is the ask.
function checkForUpdate() {
  if (!swReg || Date.now() - lastUpdateCheck < UPDATE_CHECK_MS) return;
  lastUpdateCheck = Date.now();
  swReg.update().catch(() => {});      // offline, or the host is having a moment
}

// Ask the worker that just took over what it is; it answers with its cache name
// ("dictmic-v34") and the bar shows the tail of it. The wait is capped because
// the worker being replaced shipped before this handshake existed and will
// never reply — a bar with no version on it is far better than no bar.
function workerVersion() {
  return new Promise(resolve => {
    const sw = navigator.serviceWorker.controller;
    if (!sw) { resolve(""); return; }
    const chan = new MessageChannel();
    chan.port1.onmessage = e =>
      resolve(String(e.data || "").replace(/^dictmic-/, ""));
    setTimeout(() => resolve(""), 1200);
    try { sw.postMessage("version", [chan.port2]); } catch { resolve(""); }
  });
}

// Shown only on the path where reloading by ourselves would be rude. "Later"
// hides it and nothing more: the new worker is already in charge, so the note
// they're protecting is the only thing keeping this page on the old code, and
// the moment they close it the idle rule below reloads anyway.
async function showUpdateBar(reload) {
  const bar = $("updateBar");
  if (!bar || !bar.hidden) return;
  $("updateVer").textContent = await workerVersion();
  bar.hidden = false;
  document.body.classList.add("update-ready");
  $("updateNowBtn").onclick = reload;
  $("updateLaterBtn").onclick = () => {
    bar.hidden = true;
    document.body.classList.remove("update-ready");
  };
}

// Inside the Android shell the page can be resumed without the WebView firing
// focus or visibilitychange — the same blind spot sync.js has, and the shell
// already solves it by calling DictationMicShell.resync() from onResume. That
// hook belongs to sync.js and is installed later in boot than this is, so
// rather than racing for the property we sit in front of it: whatever sync.js
// assigns is kept and still called, and the resume reaches the update check on
// its way past. Giving the shell a second hook of its own would mean a new APK,
// and the whole point of the shell is that a hosting deploy updates it without
// one.
function watchShellResume() {
  const shell = (window.DictationMicShell = window.DictationMicShell || {});
  let inner = shell.resync;
  Object.defineProperty(shell, "resync", {
    configurable: true,
    get: () => (...args) => { checkForUpdate(); if (inner) inner(...args); },
    set: fn => { inner = fn; },
  });
}

// The service worker claims this page the moment it activates (skipWaiting +
// clients.claim), which fires "controllerchange". Reloading on that blindly
// throws away whatever the user was doing — an open editor, a half-typed note —
// for a version they'd get on the next launch anyway.
//
// (This was once blamed for "+ New" closing itself. It wasn't the cause; that
// was the reconcile sweep in sync.js. It is still a bad reload.)
//
// Two guards:
//   - the very first claim (no controller at boot: fresh install, cleared
//     storage, first launch after the SW was unregistered) changes nothing
//     about the files this page is already running. Never reload for it.
//   - a genuine update only reloads while the user is on the list with nothing
//     open. Otherwise it stands up the bar and lets them choose — the new
//     worker is already in charge, so the next launch runs the new code
//     regardless, but they shouldn't have to wait that long to find out.
//
// The first guard used to be a `return` that skipped the whole function, which
// also skipped every check below it. It only ever needed to skip that one
// claim, so it now does exactly that: a deploy that lands during a long
// first session is a real update like any other.
function wireServiceWorker() {
  let firstClaim = !navigator.serviceWorker.controller;
  navigator.serviceWorker.register("sw.js").then(reg => {
    swReg = reg;
    lastUpdateCheck = Date.now();       // register() has just asked for us
  }).catch(() => {});

  let pending = false;
  const idle = () => {
    const h = location.hash;
    return h === "" || h === "#/";
  };
  const reload = () => location.reload();
  const apply = () => {
    if (!pending || !idle()) return;
    pending = false;
    reload();
  };
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (firstClaim) { firstClaim = false; return; }
    pending = true;
    apply();
    if (pending) showUpdateBar(reload);  // still here: they're mid-something
  });
  addEventListener("hashchange", apply);

  // Every way this app comes back to life. The interval alone can't be trusted
  // — a backgrounded tab has its timers throttled to a crawl, and that is
  // precisely the tab that has been stale the longest.
  addEventListener("focus", checkForUpdate);
  addEventListener("visibilitychange", () => {
    if (!document.hidden) checkForUpdate();
  });
  addEventListener("pageshow", e => { if (e.persisted) checkForUpdate(); });
  setInterval(checkForUpdate, UPDATE_CHECK_MS);
  watchShellResume();
}

// The Android shell (MainActivity) loads this very page and injects a bridge.
// Everything else about the app is identical either way — that's the point of
// the shell: one app, one sign-in, and a hosting deploy updates both.
const shell = () => !!window.DictationMicNative;
const nativeVersion = () => {
  try { return window.DictationMicNative.version() || "?"; } catch { return "?"; }
};

// One sign-in, two consumers. The page owns the account — it has the form, it
// talks to Identity Toolkit, it holds the refresh token. The shell's recorder
// needs the same account to upload notes while the page is asleep, so hand the
// token down once we know we're signed in. Cheap and idempotent: the bridge
// ignores it when nothing has changed.
async function handOverAccount() {
  if (!shell()) return;
  try {
    const { uid, email } = await import("./auth.js");
    const stored = JSON.parse(localStorage.getItem("dictmic-auth") || "null");
    if (!stored?.refreshToken) return;
    window.DictationMicNative.setAccount(email() || "", stored.refreshToken, uid());
  } catch { /* the recorder falls back to whatever it already had */ }
}

function fail(msg) {
  $("noteList").textContent = "";
  $("emptyState").hidden = false;
  $("emptyText").textContent = msg;
  $("statusText").textContent = "error";
  $("statusDot").className = "status-dot err";
}

async function boot() {
  try {
    if (isLocal) {
      const { LocalAdapter } = await import("./adapters/local.js");
      const adapter = new LocalAdapter(grabToken());
      await adapter.init();
      const app = new App(adapter, { showMic: false });
      await app.start();
    } else {
      if ("serviceWorker" in navigator) wireServiceWorker();
      const { FirebaseAdapter, showAuthIfNeeded } = await import("./adapters/firebase.js");
      const adapter = new FirebaseAdapter();
      await showAuthIfNeeded(adapter);          // resolves once signed in
      await adapter.init();
      await handOverAccount();
      // Inside the Android shell the recorder is a foreground service, not the
      // Web Speech API — same screen, same buttons, but it survives the screen
      // going off and doesn't chime before every phrase.
      const { micAvailable, openMic } = shell()
        ? await import("./nativemic.js")
        : await import("./speech.js");
      const app = new App(adapter, { showMic: micAvailable(), openMic });
      await app.start();
      wireAccountPop();
      drainSharedFiles(app);
      // Same idea, other shell: inside the APK a share (and a picked file)
      // comes over the native bridge rather than through the service worker.
      if (shell()) {
        const { wireShellShare } = await import("./shellshare.js");
        wireShellShare(app);
      }
    }
  } catch (e) {
    fail(e.message || "Something went wrong.");
  }
}

boot();
