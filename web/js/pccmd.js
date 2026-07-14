// Remote PC control (hosted + signed-in only). Type a command; it lands in
// the Realtime Database as a pending node under /users/<uid>/commands/<id>.
// A desktop listener (built separately) runs it and patches status/result
// back, which we follow live with an EventSource — the same REST-only,
// no-SDK approach as sync.js, so the service worker's RTDB bypass still holds.
//
// The RTDB node the phone writes is fixed: { text, createdAt, status, origin }
// with status "pending". The desktop writes back status ("done" | "failed" |
// "stale") plus a human "result" string, which we render on the row.

import { FIREBASE } from "../config.js";
import { idToken, uid, signedIn } from "./auth.js";
import { uuid, relTime } from "./util.js";

const $ = id => document.getElementById(id);

// Mirror of sync.js's notesUrl: auth token rides the query string so an
// EventSource (which can't set headers) can still authenticate.
const cmdsUrl = (id, token) =>
  `${FIREBASE.databaseURL}/users/${uid()}/commands${id ? "/" + id : ""}.json?auth=${token}`;

// id -> { id, text, createdAt, status, origin, result, localFailed }
let cmds = new Map();

let open = false;
let wired = false;
let tickTimer = null;          // re-render every few seconds: relTime + 25s hint

// live stream (only while the panel is open)
let es = null;
let esBackoff = 1000;
let reconnectTimer = null;

// The PC button shows only in the hosted PWA (Firebase adapter) once signed
// in. On localhost the LocalAdapter reports kind "local", so it stays hidden;
// signed-out is impossible in Firebase mode at boot but we still guard.
export function pcAvailable(adapter) {
  return adapter?.kind === "firebase" && signedIn();
}

export function togglePcPanel() {
  if (open) closePcPanel(); else openPcPanel();
}

export function openPcPanel() {
  const pop = $("pcPop");
  if (!pop) return;
  if (!wired) { wire(); wired = true; }
  open = true;
  pop.hidden = false;
  render();
  connect();
  tickTimer = setInterval(render, 5000);
  // add the outside-click closer on the next task so this very opening click
  // (already bubbling) can't immediately close the panel again
  setTimeout(() => document.addEventListener("click", onDocClick), 0);
  setTimeout(() => $("pcInput")?.focus(), 60);
}

export function closePcPanel() {
  open = false;
  const pop = $("pcPop");
  if (pop) pop.hidden = true;
  disconnect();
  if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
  document.removeEventListener("click", onDocClick);
}

function onDocClick(e) {
  if (!open) return;
  const pop = $("pcPop");
  if (!pop || pop.contains(e.target)) return;
  if (e.target.closest("#pcBtn")) return;      // the toggle owns that click
  closePcPanel();
}

function wire() {
  $("pcSendBtn")?.addEventListener("click", send);
  $("pcInput")?.addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); send(); }
  });
}

// ---------------------------------------------------------------------------
// send — one PATCH, never an outbox. A command must never fire later than the
// tap that made it, so a failed send is marked failed locally and dropped; it
// is never retried in the background (the desktop has a 2-min stale guard too).
// ---------------------------------------------------------------------------

async function send() {
  const input = $("pcInput");
  if (!input) return;
  const text = input.value.trim();
  if (!text) return;                            // empty never sends

  const id = uuid();
  cmds.set(id, {
    id, text, createdAt: Date.now(),            // optimistic; the server ms
    status: "pending", origin: "phone",         // overwrites this via the stream
    result: "", localFailed: false,
  });
  input.value = "";
  render();

  let token;
  try { token = await idToken(); }
  catch { markLocalFailed(id, "Sign in again to send commands"); return; }

  try {
    const res = await fetch(cmdsUrl(id, token), {
      method: "PATCH",
      body: JSON.stringify({
        text, createdAt: { ".sv": "timestamp" }, status: "pending", origin: "phone",
      }),
    });
    if (!res.ok) {
      markLocalFailed(id, res.status === 401
        ? "Sign in again to send commands"
        : "The cloud wouldn't take that — try again");
      return;
    }
    // success: leave it pending; the live stream carries status from here on
  } catch {
    markLocalFailed(id, "Couldn't reach the cloud — offline?");
  }
}

function markLocalFailed(id, msg) {
  const c = cmds.get(id);
  if (!c) return;
  c.status = "failed";
  c.localFailed = true;
  c.result = msg;
  cmds.set(id, c);
  render();
}

// ---------------------------------------------------------------------------
// live pull — same shape as sync.js: put at "/" is a full snapshot, put/patch
// at a child path is an incremental change; a stale token means reconnect.
// ---------------------------------------------------------------------------

async function connect() {
  if (es) { try { es.close(); } catch { /* gone */ } es = null; }
  let token;
  try { token = await idToken(); } catch { return; }
  es = new EventSource(cmdsUrl(null, token));
  es.addEventListener("put", ev => onEvent("put", ev));
  es.addEventListener("patch", ev => onEvent("patch", ev));
  es.addEventListener("auth_revoked", reconnect);   // token expired mid-stream
  es.addEventListener("cancel", reconnect);
  es.onopen = () => { esBackoff = 1000; };
  es.onerror = () => reconnect();
}

// Tear the stream down and rebuild it with a freshly minted token — the
// browser's own retry reuses the original (now-stale) token and can loop
// forever, exactly the trap sync.js documents.
function reconnect() {
  if (es) { try { es.close(); } catch { /* gone */ } es = null; }
  if (!open) return;                            // panel closed → stay down
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    if (open && !es) connect();
  }, esBackoff);
  esBackoff = Math.min(esBackoff * 2, 60000);
}

function disconnect() {
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  if (es) { try { es.close(); } catch { /* gone */ } es = null; }
}

function onEvent(kind, ev) {
  let msg;
  try { msg = JSON.parse(ev.data); } catch { return; }
  const path = (msg?.path || "/").replace(/^\/+|\/+$/g, "");
  const data = msg.data;
  if (path === "") {
    if (kind === "patch") {
      for (const [id, part] of Object.entries(data || {})) mergeCmd(id, part);
    } else {
      // full snapshot: rebuild, but keep local-only failures (never in cloud)
      const keep = [...cmds.values()].filter(c => c.localFailed);
      cmds = new Map();
      for (const [id, rec] of Object.entries(data || {})) {
        if (rec && typeof rec === "object") cmds.set(id, normalize(id, rec));
      }
      for (const c of keep) if (!cmds.has(c.id)) cmds.set(c.id, c);
    }
  } else {
    const seg = path.split("/");
    const id = seg[0];
    if (seg.length > 1) {                       // a single field changed
      const cur = cmds.get(id);
      if (cur) { cur[seg[1]] = data; cmds.set(id, normalize(id, cur)); }
    } else if (kind === "patch") {
      mergeCmd(id, data);
    } else if (data == null) {
      cmds.delete(id);                          // command removed on the PC
    } else {
      cmds.set(id, normalize(id, data));
    }
  }
  render();
}

function mergeCmd(id, part) {
  if (part == null) return;
  const cur = cmds.get(id) || { id };
  cmds.set(id, normalize(id, { ...cur, ...part }));
}

function normalize(id, rec) {
  return {
    id,
    text: typeof rec.text === "string" ? rec.text : "",
    createdAt: Number(rec.createdAt) || Date.now(),
    status: rec.status || "pending",
    origin: rec.origin || "phone",
    result: rec.result != null ? String(rec.result) : "",
    localFailed: !!rec.localFailed,
  };
}

// ---------------------------------------------------------------------------
// render — last 10, newest first. Every string here goes through textContent,
// so command text and the PC's result can never inject markup.
// ---------------------------------------------------------------------------

function dotClass(c) {
  if (c.status === "done") return "ok";
  if (c.status === "failed") return "err";
  if (c.status === "stale") return "stale";
  return "pending";                             // pulses
}

function detailFor(c) {
  if (c.status === "done")   return { text: c.result || "Done", cls: "ok" };
  if (c.status === "failed") return { text: c.result || "Couldn't run that", cls: "err" };
  if (c.status === "stale")  return { text: c.result || "Too old to run — send again", cls: "stale" };
  // pending
  if (Date.now() - (c.createdAt || 0) > 25000) {
    return { text: "SENT · PC not answering — is the pill running?", cls: "dim" };
  }
  return { text: "SENT", cls: "dim" };
}

function render() {
  const list = $("pcList");
  if (!list) return;
  const rows = [...cmds.values()]
    .sort((a, b) => (b.createdAt || 0) - (a.createdAt || 0))
    .slice(0, 10);

  list.textContent = "";
  if (!rows.length) {
    const li = document.createElement("li");
    li.className = "pc-empty mono";
    li.textContent = "No commands yet";
    list.append(li);
    return;
  }

  for (const c of rows) {
    const li = document.createElement("li");
    li.className = "pc-row";

    const dot = document.createElement("span");
    dot.className = "status-dot " + dotClass(c);

    const body = document.createElement("div");
    body.className = "pc-row-body";

    const cmd = document.createElement("span");
    cmd.className = "pc-cmd";
    cmd.textContent = c.text;

    const meta = document.createElement("div");
    meta.className = "pc-meta";
    const d = detailFor(c);
    const msg = document.createElement("span");
    msg.className = "pc-msg " + d.cls;
    msg.textContent = d.text;
    const time = document.createElement("span");
    time.className = "pc-time mono";
    time.textContent = relTime(c.createdAt);
    meta.append(msg, time);

    body.append(cmd, meta);
    li.append(dot, body);
    list.append(li);
  }
}
