// Firebase adapter — phone side. IndexedDB is the local truth: every
// mutation lands there first plus an outbox entry, so notes made in a field
// with no signal survive force-closes and upload when the network returns.

import { notesDb, outboxDb } from "../db.js";
import { noteTitleFrom, uuid } from "../util.js";
import { signedIn, signIn, sendPasswordReset } from "../auth.js";
import { startSync, flush, syncStatus, pendingIds as syncPending } from "../sync.js";

const $ = id => document.getElementById(id);

export class FirebaseAdapter {
  kind = "firebase";

  constructor() {
    this._listeners = [];
    this._pending = new Set();
  }

  async init() {
    startSync({
      onChange: () => this._refresh(),
      onStateChange: () => {},
    });
    await this._refreshPending();
    return { needsAuth: false };
  }

  async _refresh() {
    await this._refreshPending();
    const notes = await this.list();
    this._listeners.forEach(cb => cb(notes));
  }

  async _refreshPending() {
    this._pending = await syncPending();
  }

  async list() {
    const notes = await notesDb.all();
    notes.sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
    return notes;
  }

  get(id) { return notesDb.get(id); }

  async create({ title, body }) {
    const id = uuid();
    const note = {
      id,
      title: (title || "").trim() || noteTitleFrom(body),
      body,
      createdAt: Date.now(),
      updatedAt: Date.now(),
      syncedRev: 0,
    };
    await notesDb.put(note);
    await this._queue(note);
    return note;
  }

  async createVoice({ audio, audioMime }) {
    const id = uuid();
    const note = {
      id,
      title: "Voice note (transcribing…)",
      body: "",
      createdAt: Date.now(),
      updatedAt: Date.now(),
      syncedRev: 0,
    };
    await notesDb.put(note);            // audio only rides the outbox — the
    await outboxDb.add({                // local mirror stays lightweight
      op: "put", id, queuedAt: Date.now(),
      payload: { title: note.title, body: "", createdAt: note.createdAt,
                 audio, audioMime, transcribed: false },
    });
    this._pending.add(id);
    flush();
    return note;
  }

  async update(id, body) {
    const note = await notesDb.get(id);
    if (!note) return null;
    Object.assign(note, { body, updatedAt: Date.now() });
    await notesDb.put(note);
    await this._queue(note);
    return note;
  }

  async rename(id, title) {
    const note = await notesDb.get(id);
    if (!note) return null;
    Object.assign(note, { title: title.trim() || note.title, updatedAt: Date.now() });
    await notesDb.put(note);
    await this._queue(note);
    return note;
  }

  async remove(id) {
    await notesDb.del(id);
    await outboxDb.add({ op: "delete", id, queuedAt: Date.now() });
    this._pending.add(id);
    flush();
    return { ok: true };
  }

  // Starring rides its own outbox op — no updatedAt bump, so a star never
  // reorders the list. The local record is the truth the moment it's written;
  // the op just carries it to the cloud (and on to the laptop) when there's
  // signal.
  async setStar(id, starred) {
    const note = await notesDb.get(id);
    if (!note) return null;
    const starredAt = Date.now();
    Object.assign(note, { starred: !!starred, starredAt });
    await notesDb.put(note);
    await outboxDb.add({
      op: "star", id, starred: !!starred, starredAt, queuedAt: Date.now(),
    });
    this._pending.add(id);
    flush();
    return note;
  }

  async _queue(note) {
    await outboxDb.add({
      op: "put", id: note.id, queuedAt: Date.now(),
      payload: { title: note.title, body: note.body, createdAt: note.createdAt },
    });
    this._pending.add(note.id);
    flush();
  }

  pendingIds() { return this._pending; }

  onChange(cb) { this._listeners.push(cb); }

  async status() { return syncStatus(); }
}

// ---------------------------------------------------------------------------
// sign-in overlay (hosted origin only, first run / revoked token)
// ---------------------------------------------------------------------------

export function showAuthIfNeeded() {
  if (signedIn()) return Promise.resolve();
  return new Promise(resolve => {
    const pane = $("authPane");
    pane.hidden = false;
    const btn = $("authBtn"), err = $("authError");
    const go = async () => {
      const email = $("authEmail").value.trim();
      const pw = $("authPassword").value;
      err.classList.remove("ok");
      if (!email || !pw) { err.textContent = "Fill in both boxes"; return; }
      btn.disabled = true;
      btn.textContent = "Signing in…";
      err.textContent = "";
      try {
        await signIn(email, pw);
        pane.hidden = true;
        resolve();
      } catch (e) {
        err.textContent = e.message;
        btn.disabled = false;
        btn.textContent = "Sign in";
      }
    };
    btn.addEventListener("click", go);
    pane.addEventListener("keydown", e => { if (e.key === "Enter") go(); });
    $("authForgot").addEventListener("click", async () => {
      const email = $("authEmail").value.trim();
      err.classList.remove("ok");
      if (!email) { err.textContent = "Type your email above first"; return; }
      err.textContent = "Sending the reset link…";
      try {
        await sendPasswordReset(email);
        err.classList.add("ok");
        err.textContent = `Reset link sent to ${email} — check your inbox`;
      } catch (e) {
        err.textContent = e.message;
      }
    });
    $("authEmail").focus();
  });
}
