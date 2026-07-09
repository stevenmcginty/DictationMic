// Service worker — hosted origin only (main.js never registers it on
// localhost). App shell is precached; data traffic is always network-only.

const CACHE = "dictmic-v17";
const SHARED = "dictmic-shared";   // Android share-sheet drops wait here for main.js
const SHELL = [
  "./", "index.html", "styles.css", "config.js", "manifest.webmanifest",
  "js/main.js", "js/ui.js", "js/util.js", "js/db.js", "js/sync.js",
  "js/auth.js", "js/speech.js", "js/imgnote.js", "js/filenote.js",
  "js/adapters/local.js", "js/adapters/firebase.js",
  "fonts/SpaceGrotesk.woff2", "fonts/JetBrainsMono.woff2",
  "icons/icon-192.png", "icons/icon-512.png",
];

self.addEventListener("install", e => {
  // cache:"no-cache" = straight to the network, never the browser's HTTP
  // cache — otherwise a fresh install can precache hour-old files
  e.waitUntil(caches.open(CACHE)
    .then(c => Promise.allSettled(
      SHELL.map(u => c.add(new Request(u, { cache: "no-cache" })))))
    .then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(
      keys.filter(k => k !== CACHE && k !== SHARED).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

// shell files answer from cache instantly, then refresh the cached copy in
// the background (stale-while-revalidate) — so even a deploy that forgets to
// bump CACHE reaches every phone by its next launch
const SHELL_PATHS = new Set(
  SHELL.map(u => new URL(u, self.registration.scope).pathname));

// Android share sheet (manifest share_target): the WebAPK POSTs the shared
// screenshot here. The page never sees that request, so stash the files in
// their own cache and bounce to the app — main.js drains them into notes.
const SHARE_PATH = new URL("share-target", self.registration.scope).pathname;

async function receiveShare(request) {
  try {
    const form = await request.formData();
    const files = [...form.values()].filter(v => v instanceof File && v.size);
    const cache = await caches.open(SHARED);
    const stamp = Date.now();
    await Promise.all(files.map((f, i) => cache.put(
      new URL(`__shared__/${stamp}-${i}`, self.registration.scope).href,
      new Response(f, { headers: {
        "Content-Type": f.type || "application/octet-stream",
        "X-Shared-Name": encodeURIComponent(f.name || ""),
      }}))));
  } catch { /* bad form data — still land in the app */ }
  return Response.redirect(new URL("./#/", self.registration.scope).href, 303);
}

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method === "POST" && url.pathname === SHARE_PATH) {
    e.respondWith(receiveShare(e.request));
    return;
  }
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
    const refresh = fetch(e.request.url, { cache: "no-cache" }).then(res => {
      if (res && res.ok) cache.put(e.request, res.clone());
      return res;
    });
    if (hit) { e.waitUntil(refresh.catch(() => {})); return hit; }
    return refresh;
  })());
});
