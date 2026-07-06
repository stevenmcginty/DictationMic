// Phone sync worker: flushes the IndexedDB outbox to the Realtime Database
// and follows it live with an EventSource. Mirror of cloudsync.py.

import { FIREBASE } from "../config.js";
import { notesDb, outboxDb, metaDb } from "./db.js";
import { idToken, uid, signedIn } from "./auth.js";

let state = "starting";       // ok | offline | needs-signin | error
let lastSync = 0;
let es = null;
let esBackoff = 1000;
let flushing = false;
let flushAgain = false;       // a queue() came in mid-flush — its entry was
                               // never in this flush's snapshot, so re-run
let onRemote = () => {};      // adapter refreshes the UI through this
let onState = () => {};
let retryTimer = null;        // failed flush → quick retry, backing off
let retryMs = 5000;
let lastBeat = 0;             // last time the live stream produced ANY event
let watchdog = null;          // force-reconnects a stream that's gone silent
let streamLive = false;       // is the live pull actually connected right now
let reconnectTimer = null;    // one pending reconnect (avoids retry storms)

function scheduleRetry() {
  if (retryTimer) return;
  retryTimer = setTimeout(() => { retryTimer = null; flush(); }, retryMs);
  retryMs = Math.min(retryMs * 2, 60000);
}

const notesUrl = (id, token) =>
  `${FIREBASE.databaseURL}/users/${uid()}/notes${id ? "/" + id : ""}.json?auth=${token}`;

function setState(s) {
  if (s !== state) { state = s; onState(state); }
}

export function syncStatus() {
  return { sync: state, lastSync };
}

export async function pendingIds() {
  try { return new Set((await outboxDb.all()).map(e => e.id)); }
  catch { return new Set(); }
}

// ids with a queued *body* op (put/title/delete). A queued star op is left out:
// the star is an independent field, so it must not block an incoming body edit —
// and an incoming star must not be blocked by a queued body op (see applyOne).
async function bodyPendingIds() {
  try {
    return new Set((await outboxDb.all())
      .filter(e => e.op !== "star").map(e => e.id));
  } catch { return new Set(); }
}

// ---------------------------------------------------------------------------
// outbox flush
// ---------------------------------------------------------------------------

export async function flush() {
  if (flushing) { flushAgain = true; return; }
  if (!navigator.onLine) { scheduleRetry(); return; }
  flushing = true;
  flushAgain = false;
  try {
    const entries = await outboxDb.all();          // key order = queue order
    if (!entries.length) {
      // An empty outbox is "synced" only when the live stream is actually
      // connected — not merely when a token exists. A wedged stream used to
      // still read "synced" here, hiding that nothing was arriving live.
      if (!signedIn()) setState("needs-signin");
      else if (streamLive) setState("ok");
      else if (!navigator.onLine) setState("offline");
      else setState("starting");                   // online but reconnecting
      return;
    }
    let token;
    try { token = await idToken(); }
    catch { setState("needs-signin"); scheduleRetry(); return; }
    for (const e of entries) {
      if (e.op === "star") {
        // Skip a star push that a newer star already superseded — a quick
        // re-toggle, or one we adopted from another device. Pushing the stale
        // value would clobber the newer star back onto the cloud.
        const cur = await notesDb.get(e.id);
        if (!cur || (Number(cur.starredAt) || 0) > e.starredAt) {
          await outboxDb.del(e.key);
          continue;
        }
      }
      const payload = e.op === "delete"
        ? { deleted: true, body: null, origin: "phone",
            updatedAt: { ".sv": "timestamp" } }
        : e.op === "star"
        // a star is its own field, carried by its own client timestamp — never
        // an updatedAt bump, so starring never reorders the list or drags an
        // old note under today's heading. RTDB PATCH merges it in, so this
        // touches neither title nor body.
        ? { starred: !!e.starred, starredAt: e.starredAt, origin: "phone" }
        : { ...e.payload, deleted: false, origin: "phone",
            updatedAt: { ".sv": "timestamp" } };
      let res;
      try {
        res = await fetch(notesUrl(e.id, token), {
          method: "PATCH", body: JSON.stringify(payload),
        });
      } catch { setState("offline"); scheduleRetry(); return; }
      if (res.status === 401) {
        try { token = await idToken(); }
        catch { setState("needs-signin"); scheduleRetry(); return; }
        continue;                                   // entry retried next flush
      }
      if (!res.ok) { setState("error"); scheduleRetry(); return; }
      const saved = await res.json();
      const rev = Number(saved.updatedAt) || Date.now();
      if (e.op === "delete") {
        const tombs = (await metaDb.get("tombstones")) || {};
        tombs[e.id] = rev;
        await metaDb.set("tombstones", tombs);
      } else if (e.op === "star") {
        // the star is already the local truth (setStar wrote it before
        // queueing); a star push carries no updatedAt, so there's no rev to
        // record and nothing to reconcile here.
      } else {
        const note = await notesDb.get(e.id);
        if (note) await notesDb.put({ ...note, syncedRev: rev, updatedAt: rev });
      }
      await outboxDb.del(e.key);
      lastSync = Date.now();
    }
    retryMs = 5000;                                 // healthy again
    setState("ok");
    onRemote();                                     // pending dots vanish
  } finally {
    flushing = false;
    if (flushAgain) { flushAgain = false; flush(); }
  }
}

// ---------------------------------------------------------------------------
// live pull (EventSource — auth token goes in the query string)
// ---------------------------------------------------------------------------

async function connect() {
  if (es) { es.close(); es = null; }
  let token;
  try { token = await idToken(); }
  catch { setState("needs-signin"); return; }
  es = new EventSource(notesUrl(null, token));
  const beat = () => { lastBeat = Date.now(); };
  es.addEventListener("put", ev => { beat(); handle("put", ev); });
  es.addEventListener("patch", ev => { beat(); handle("patch", ev); });
  es.addEventListener("keep-alive", beat);         // RTDB pings ~every 30s
  es.addEventListener("auth_revoked", () => hardReconnect());
  es.addEventListener("cancel", () => hardReconnect());
  es.onopen = () => {
    esBackoff = 1000; lastBeat = Date.now(); streamLive = true; setState("ok");
  };
  es.onerror = () => {
    streamLive = false;
    if (!navigator.onLine) setState("offline");
    // The browser retries a dropped RTDB stream on its own — but always with
    // the ORIGINAL url, whose auth token may have since expired, so it can
    // silently loop forever without ever re-authenticating (the note only
    // shows up after a full page reload). Tear it down and reconnect with a
    // freshly minted token instead of trusting the built-in retry.
    hardReconnect();
  };
  startWatchdog();
}

// Close a dead/wedged stream and reconnect from scratch — connect() mints a
// fresh token, which is the whole point (the stale one is why it wedged).
function hardReconnect() {
  if (es) { try { es.close(); } catch { /* already gone */ } es = null; }
  streamLive = false;
  scheduleReconnect();
}

function scheduleReconnect() {
  if (reconnectTimer) return;                      // one reconnect in flight
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    if (!es) connect();
  }, esBackoff);
  esBackoff = Math.min(esBackoff * 2, 60000);
}

// A healthy stream emits a keep-alive (or data) at least every ~30s. Three
// missed beats means the socket died and the browser is quietly retrying it
// with a stale token — which never recovers on its own. Force a fresh reconnect.
function startWatchdog() {
  if (watchdog) return;
  watchdog = setInterval(() => {
    if (es && lastBeat && Date.now() - lastBeat > 95000) hardReconnect();
  }, 30000);
}

// Bring the live pull back the moment it's needed (network returned, tab
// refocused) without waiting on the watchdog's next tick.
function ensureLive() {
  if (!es) { esBackoff = 1000; connect(); return; }
  if (lastBeat && Date.now() - lastBeat > 60000) { esBackoff = 1000; hardReconnect(); }
}

async function handle(kind, ev) {
  let msg;
  try { msg = JSON.parse(ev.data); } catch { return; }
  const path = (msg?.path || "/").replace(/^\/+|\/+$/g, "");
  if (path === "") {
    // "put" at the root is the whole snapshot; "patch" names only the notes
    // that changed, and each carries only its changed fields — so merge those.
    if (kind === "patch") {
      for (const [id, part] of Object.entries(msg.data || {})) await applyPatch(id, part);
    } else {
      await reconcile(msg.data || {});
    }
  } else {
    const id = path.split("/")[0];
    if (path.includes("/")) {
      await refetch(id);                  // a single field moved: re-pull the whole note
    } else if (kind === "patch") {
      await applyPatch(id, msg.data);      // changed fields only — never the full note
    } else {
      await applyOne(id, msg.data);        // "put": the complete record (or a delete)
    }
  }
  lastSync = Date.now();
  setState("ok");
  onRemote();
}

async function refetch(id) {
  try {
    const token = await idToken();
    const res = await fetch(notesUrl(id, token));
    if (res.ok) await applyOne(id, await res.json());
  } catch { /* next snapshot heals it */ }
}

// The star is an independent last-writer-wins field: whichever device touched
// it most recently (by its own clock) wins, regardless of what happened to the
// body. Ties (equal starredAt) keep the local value — matching notestore.py's
// set_remote_star, so the two platforms never pick opposite winners. Notes that
// were never starred carry no star fields, so both sides read as starredAt 0.
function mergeStar(local, record) {
  const recAt = Number(record?.starredAt) || 0;
  const locAt = Number(local?.starredAt) || 0;
  return recAt > locAt
    ? { starred: !!record?.starred, starredAt: recAt }
    : { starred: !!local?.starred, starredAt: locAt };
}

// The cloud can only field-merge a bare PATCH; it can't compare timestamps. So
// if a star arrives OLDER than ours, the cloud is holding a stale value that a
// concurrent write clobbered — re-queue ours so every device converges on the
// newer star. (Guarded against piling up duplicate re-asserts.)
async function reassertStar(id, local) {
  if ((await outboxDb.all()).some(e => e.op === "star" && e.id === id)) return;
  await outboxDb.add({
    op: "star", id, starred: !!local.starred,
    starredAt: Number(local.starredAt) || 0, queuedAt: Date.now(),
  });
  flush();
}

// Fold an incoming record/patch's star into the local note BEFORE the body-sync
// guards, so the star always converges independently of body state (this is the
// mirror of cloudsync.py applying remote stars above its dirty guards). Returns
// the local note after any adoption. A no-op when the source carries no star.
async function mergeStarInto(id, local, src) {
  if (!local || !src || src.deleted) return local;
  if (!("starred" in src) && src.starredAt == null) return local;
  const recAt = Number(src.starredAt) || 0;
  const locAt = Number(local.starredAt) || 0;
  if (recAt > locAt) {
    const updated = { ...local, starred: !!src.starred, starredAt: recAt };
    await notesDb.put(updated);
    return updated;
  }
  if (recAt < locAt) await reassertStar(id, local);
  return local;
}

async function applyOne(id, record) {
  const bodyPending = await bodyPendingIds();
  let local = await notesDb.get(id);
  if (record && typeof record === "object") {
    local = await mergeStarInto(id, local, record);   // star merges regardless
  }
  if (bodyPending.has(id)) return;          // queued body intent wins until flushed
  const tombs = (await metaDb.get("tombstones")) || {};
  if (record === null || record.deleted) {
    const rev = Number(record?.updatedAt) || Date.now();
    if (tombs[id] && rev <= tombs[id]) return;
    tombs[id] = rev;
    await metaDb.set("tombstones", tombs);
    await notesDb.del(id);
    return;
  }
  const rev = Number(record.updatedAt) || 0;
  if (local && rev <= (local.syncedRev || 0)) return;   // body echo (star done above)
  const star = mergeStar(local, record);
  await notesDb.put({
    id,
    title: record.title || "Note",
    body: record.body || "",
    createdAt: Number(record.createdAt) || rev,
    updatedAt: rev,
    syncedRev: rev,
    ...star,
  });
}

// A Realtime-Database "patch" hands us only the fields that changed. Fold them
// onto the note we already hold, so renaming a note never drops its body and
// editing a body never drops its title. A patch for a note we've never seen —
// or a delete — can't be merged, so fetch/handle the whole record instead.
async function applyPatch(id, part) {
  if (part == null) return;
  const bodyPending = await bodyPendingIds();
  let local = await notesDb.get(id);
  if (!part.deleted) local = await mergeStarInto(id, local, part);  // star first
  if (bodyPending.has(id)) return;          // queued body intent wins until flushed
  if (part.deleted) { await applyOne(id, part); return; }
  if (!local) { await refetch(id); return; }
  const rev = Number(part.updatedAt) || 0;
  if (rev <= (local.syncedRev || 0)) return;   // body echo (star handled above)
  const star = mergeStar(local, part);
  await notesDb.put({
    ...local,
    ...(part.title != null ? { title: part.title } : {}),
    ...(part.body != null ? { body: part.body } : {}),
    ...(part.createdAt != null ? { createdAt: Number(part.createdAt) } : {}),
    ...star,
    updatedAt: rev,
    syncedRev: rev,
  });
}

async function reconcile(snapshot) {
  // Only a queued *body* op protects a note from the "gone from the cloud"
  // sweep — a pending star must not (it can't meaningfully resurrect a deleted
  // note, and keeping it would diverge from cloudsync.py, which guards on the
  // body-`dirty` flag alone). Its stale star op is dropped by flush's guard
  // once the note is deleted here.
  const pending = await bodyPendingIds();
  const seen = new Set();
  for (const [id, record] of Object.entries(snapshot)) {
    if (record && typeof record === "object") {
      seen.add(id);
      await applyOne(id, record);
    }
  }
  // synced notes missing from the snapshot were deleted+purged elsewhere
  for (const n of await notesDb.all()) {
    if (!seen.has(n.id) && (n.syncedRev || 0) > 0 && !pending.has(n.id)) {
      await notesDb.del(n.id);
    }
  }
}

// ---------------------------------------------------------------------------

export function startSync({ onChange, onStateChange } = {}) {
  onRemote = onChange || onRemote;
  onState = onStateChange || onState;
  connect();
  flush();
  addEventListener("online", () => { flush(); ensureLive(); });
  addEventListener("visibilitychange", () => {
    if (!document.hidden) { flush(); ensureLive(); }
  });
  setInterval(flush, 60000);
}
