// Boot: pick the data adapter by origin, then hand over to the App.
//   127.0.0.1 / localhost  -> LocalAdapter (DictationMic.exe, notes\ folder)
//   anywhere else          -> FirebaseAdapter (IndexedDB + cloud, PWA)

import { App } from "./ui.js";

const $ = id => document.getElementById(id);
const isLocal = ["127.0.0.1", "localhost"].includes(location.hostname);

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
    pop.hidden = !pop.hidden;
  });
  $("signOutBtn").addEventListener("click", () => {
    signOut();
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

// The service worker claims this page the moment it activates (skipWaiting +
// clients.claim), which fires "controllerchange". Reloading on that blindly is
// what made a freshly-tapped "+ New" note vanish a few seconds after opening:
// the reload landed on #/note/<id> before the note had come back from the
// adapter, openNote() found nothing and bounced to the list.
//
// Two guards now:
//   - the very first claim (no controller at boot: fresh install, cleared
//     storage, first launch after the SW was unregistered) changes nothing
//     about the files this page is already running. Never reload for it.
//   - a genuine update only reloads while the user is on the list with nothing
//     open. Otherwise it waits — the new worker is already in charge, so the
//     next launch runs the new code regardless.
function wireServiceWorker() {
  const hadController = !!navigator.serviceWorker.controller;
  navigator.serviceWorker.register("sw.js").catch(() => {});
  if (!hadController) return;

  let pending = false;
  const idle = () => {
    const h = location.hash;
    return h === "" || h === "#/";
  };
  const apply = () => {
    if (!pending || !idle()) return;
    pending = false;
    location.reload();
  };
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    pending = true;
    apply();
  });
  addEventListener("hashchange", apply);
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
      const { micAvailable, openMic } = await import("./speech.js");
      const app = new App(adapter, { showMic: micAvailable(), openMic });
      await app.start();
      wireAccountPop();
      drainSharedFiles(app);
    }
  } catch (e) {
    fail(e.message || "Something went wrong.");
  }
}

boot();
