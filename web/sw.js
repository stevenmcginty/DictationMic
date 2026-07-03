// Service worker — hosted origin only (main.js never registers it on
// localhost). App shell is precached; data traffic is always network-only.

const CACHE = "dictmic-v6";
const SHELL = [
  "./", "index.html", "styles.css", "config.js", "manifest.webmanifest",
  "js/main.js", "js/ui.js", "js/util.js", "js/db.js", "js/sync.js",
  "js/auth.js", "js/speech.js", "js/adapters/local.js", "js/adapters/firebase.js",
  "fonts/SpaceGrotesk.woff2", "fonts/JetBrainsMono.woff2",
  "icons/icon-192.png", "icons/icon-512.png",
];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE)
    .then(c => Promise.allSettled(SHELL.map(u => c.add(u))))
    .then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

// shell files answer from cache instantly, then refresh the cached copy in
// the background (stale-while-revalidate) — so even a deploy that forgets to
// bump CACHE reaches every phone by its next launch
const SHELL_PATHS = new Set(
  SHELL.map(u => new URL(u, self.registration.scope).pathname));

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET"
      || url.hostname.endsWith("googleapis.com")
      || url.hostname.includes("firebaseio.com")
      || url.hostname.includes("firebasedatabase.app")
      || url.pathname.startsWith("/api/")
      || !SHELL_PATHS.has(url.pathname)) {
    return;                                    // network only (data, downloads)
  }
  e.respondWith((async () => {
    const cache = await caches.open(CACHE);
    const hit = await cache.match(e.request, { ignoreSearch: true });
    const refresh = fetch(e.request).then(res => {
      if (res && res.ok) cache.put(e.request, res.clone());
      return res;
    });
    if (hit) { e.waitUntil(refresh.catch(() => {})); return hit; }
    return refresh;
  })());
});
