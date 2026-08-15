// Local adapter — talks to the 127.0.0.1 REST API inside DictationMic.exe,
// which reads and writes the notes\ folder directly. Fully offline.

export class LocalAdapter {
  kind = "local";

  constructor(token) {
    this.token = token;
    this._listeners = [];
    this._rev = null;
  }

  async init() {
    await this.list();                       // fail fast if token/server bad
    // A second, not four: /api/rev is a few bytes, so the poll can be quick
    // without costing anything. It used to re-fetch every note on every tick
    // and diff the serialised result, which put a note dictated on the phone
    // up to four seconds behind the window that was meant to be watching for
    // it — and churned the whole notes folder through JSON to do it.
    this._poll = setInterval(() => this._check(), 1000);
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
    // Read the revision first. Taking it after the notes could miss a change
    // that landed between the two calls; taking it before means at worst one
    // redundant re-fetch on the next tick, which is the harmless direction.
    const rev = await this._rev_now();
    const notes = await this._fetch("/api/notes");
    if (rev !== null) this._rev = rev;
    return notes;
  }

  async _rev_now() {
    try { return (await this._fetch("/api/rev")).rev; }
    catch { return null; }
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

  setStar(id, starred) {
    return this._fetch(`/api/notes/${id}/star`, {
      method: "PUT", body: JSON.stringify({ starred: !!starred }),
    });
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
    // Cheap change detection: ask for a counter, and only pay for the notes
    // when it has moved. An older DictationMic.exe has no /api/rev, so a null
    // means "can't tell" and we fall back to fetching and diffing as before —
    // the window keeps working while the app catches up.
    try {
      const rev = await this._rev_now();
      if (rev !== null) {
        if (rev === this._rev) return;
        this._rev = rev;
      }
      const notes = await this._fetch("/api/notes");
      if (rev === null) {                 // no counter: diff as we always did
        const json = JSON.stringify(notes);
        if (json === this._lastJson) return;
        this._lastJson = json;
      }
      this._listeners.forEach(cb => cb(notes));
    } catch { /* server briefly away — next tick */ }
  }
}
