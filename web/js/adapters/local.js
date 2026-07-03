// Local adapter — talks to the 127.0.0.1 REST API inside DictationMic.exe,
// which reads and writes the notes\ folder directly. Fully offline.

export class LocalAdapter {
  kind = "local";

  constructor(token) {
    this.token = token;
    this._listeners = [];
    this._lastJson = "";
  }

  async init() {
    await this.list();                       // fail fast if token/server bad
    this._poll = setInterval(() => this._check(), 4000);
    return { needsAuth: false };
  }

  _headers() {
    return { "X-DictMic-Token": this.token, "Content-Type": "application/json" };
  }

  async _fetch(path, opts = {}) {
    const res = await fetch(path, { ...opts, headers: this._headers() });
    if (res.status === 403) throw new Error("Reopen My notes from the pill's right-click menu.");
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `HTTP ${res.status}`);
    return res.json();
  }

  async list() {
    const notes = await this._fetch("/api/notes");
    this._lastJson = JSON.stringify(notes);
    return notes;
  }

  get(id) { return this._fetch(`/api/notes/${id}`); }

  create({ title, body }) {
    return this._fetch("/api/notes", { method: "POST", body: JSON.stringify({ title, body }) });
  }

  update(id, body) {
    return this._fetch(`/api/notes/${id}`, { method: "PUT", body: JSON.stringify({ body }) });
  }

  rename(id, title) {
    return this._fetch(`/api/notes/${id}/title`, { method: "PUT", body: JSON.stringify({ title }) });
  }

  remove(id) { return this._fetch(`/api/notes/${id}`, { method: "DELETE" }); }

  async status() {
    try {
      return await this._fetch("/api/status");
    } catch {
      return { sync: "gone", lastSync: 0 };
    }
  }

  pendingIds() { return new Set(); }

  onChange(cb) { this._listeners.push(cb); }

  async _check() {
    // cheap change detection: notify the UI when the folder content moved
    try {
      const notes = await this._fetch("/api/notes");
      const json = JSON.stringify(notes);
      if (json !== this._lastJson) {
        this._lastJson = json;
        this._listeners.forEach(cb => cb(notes));
      }
    } catch { /* server briefly away — next tick */ }
  }
}
