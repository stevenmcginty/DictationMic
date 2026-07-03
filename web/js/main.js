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
      if ("serviceWorker" in navigator) {
        navigator.serviceWorker.register("sw.js").catch(() => {});
        // a new version just took over (sw.js skipWaiting + claim): reload so
        // this very launch runs it — never while a recording is in progress
        let reloaded = false;
        navigator.serviceWorker.addEventListener("controllerchange", () => {
          if (reloaded || location.hash === "#/mic") return;
          reloaded = true;
          location.reload();
        });
      }
      const { FirebaseAdapter, showAuthIfNeeded } = await import("./adapters/firebase.js");
      const adapter = new FirebaseAdapter();
      await showAuthIfNeeded(adapter);          // resolves once signed in
      await adapter.init();
      const { micAvailable, openMic } = await import("./speech.js");
      const app = new App(adapter, { showMic: micAvailable(), openMic });
      await app.start();
      wireAccountPop();
    }
  } catch (e) {
    fail(e.message || "Something went wrong.");
  }
}

boot();
