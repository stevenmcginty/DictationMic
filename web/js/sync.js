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
      // an empty outbox is only "synced" if we can actually sync
      setState(signedIn() ? "ok" : "needs-signin");
      return;
    }
    let token;
    try { token = await idToken(); }
    catch { setState("needs-signin"); scheduleRetry(); return; }
    for (const e of entries) {
      const payload = e.op === "delete"
        ? { deleted: true, body: null, origin: "phone",
            updatedAt: { ".sv": "timestamp" } }
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
  es.addEventListener("put", ev => handle("put", ev));
  es.addEventListener("patch", ev => handle("patch", ev));
  es.addEventListener("auth_revoked", () => { es.close(); es = null; connect(); });
  es.addEventListener("cancel", () => { es.close(); es = null; scheduleReconnect(); });
  es.onopen = () => { esBackoff = 1000; };
  es.onerror = () => {
    if (es && es.readyState === EventSource.CLOSED) { es = null; scheduleReconnect(); }
    if (!navigator.onLine) setState("offline");
  };
}

function scheduleReconnect() {
  setTimeout(() => { if (!es) connect(); }, esBackoff);
  esBackoff = Math.min(esBackoff * 2, 60000);
}

async function handle(kind, ev) {
  let msg;
  try { msg = JSON.parse(ev.data); } catch { return; }
  const path = (msg?.path || "/").replace(/^\/+|\/+$/g, "");
  if (path === "") {
    await reconcile(msg.data || {});
  } else {
    const id = path.split("/")[0];
    if (path.includes("/")) {
      // sub-field patch: re-fetch the whole record once
      try {
        const token = await idToken();
        const res = await fetch(notesUrl(id, token));
        if (res.ok) await applyOne(id, await res.json());
      } catch { /* next snapshot heals it */ }
    } else {
      await applyOne(id, msg.data);
    }
  }
  lastSync = Date.now();
  setState("ok");
  onRemote();
}

async function applyOne(id, record) {
  const pending = await pendingIds();
  if (pending.has(id)) return;              // our queued intent wins until flushed
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
  const local = await notesDb.get(id);
  if (local && rev <= (local.syncedRev || 0)) return;    // echo
  await notesDb.put({
    id,
    title: record.title || "Note",
    body: record.body || "",
    createdAt: Number(record.createdAt) || rev,
    updatedAt: rev,
    syncedRev: rev,
  });
}

async function reconcile(snapshot) {
  const pending = await pendingIds();
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
  addEventListener("online", () => { flush(); if (!es) connect(); });
  addEventListener("visibilitychange", () => {
    if (!document.hidden) { flush(); if (!es) connect(); }
  });
  setInterval(flush, 60000);
}
