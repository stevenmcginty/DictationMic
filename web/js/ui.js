// All DOM logic for the notes app. Views: list / note / mic (routes via
// location.hash, panes toggled with body classes — see styles.css).

import { relTime, debounce, noteTitleFrom } from "./util.js";
import {
  isImageBody, imageKb, fileToImageBody, imageBodyToPngBlob,
  imageBodyToFile, photoTitle,
} from "./imgnote.js";

const $ = id => document.getElementById(id);

export class App {
  constructor(adapter, opts = {}) {
    this.adapter = adapter;
    this.opts = opts;              // {showMic, openMic}
    this.notes = [];
    this.filter = "";
    this.activeId = null;
    this._saveBody = debounce(() => this._commitBody(), 700);
    this._disarmTimer = null;
  }

  async start() {
    this.notes = await this.adapter.list();
    this._bind();
    this.renderList();
    this.adapter.onChange(notes => this._onRemote(notes));
    this._pollStatus();
    setInterval(() => this._pollStatus(), 3000);
    if (this.opts.showMic) $("micFab").hidden = false;
    this.route();
    addEventListener("hashchange", () => this.route());
  }

  // ---------------- routing ----------------

  route() {
    const h = location.hash;
    const note = h.match(/^#\/note\/([a-z0-9-]+)$/i);
    document.body.classList.remove("view-note", "view-mic");
    $("micPane").hidden = true;
    if (note) {
      this.openNote(note[1]);
    } else if (h === "#/mic" && this.opts.showMic) {
      document.body.classList.add("view-mic");
      $("micPane").hidden = false;
      this.opts.openMic?.(this);
    } else {
      this.activeId = null;
      this._saveBody.flush();
      $("noteInner").hidden = true;
      $("noteBlank").hidden = false;
      this._markActiveRow();
    }
  }

  // ---------------- list ----------------

  renderList() {
    const list = $("noteList");
    const q = this.filter.trim().toLowerCase();
    const shown = q
      ? this.notes.filter(n => (isImageBody(n.body)
          ? n.title + "\nimage photo"                 // don't grep base64 soup
          : n.title + "\n" + n.body).toLowerCase().includes(q))
      : this.notes;
    const pending = this.adapter.pendingIds();

    list.textContent = "";
    shown.forEach((n, i) => {
      const li = document.createElement("li");
      li.className = "note-row" + (n.id === this.activeId ? " active" : "");
      li.dataset.id = n.id;
      li.tabIndex = 0;
      li.style.animationDelay = `${Math.min(i, 14) * 12}ms`;

      const top = document.createElement("div");
      top.className = "note-row-top";
      const title = document.createElement("span");
      title.className = "note-row-title";
      title.textContent = n.title;
      const time = document.createElement("span");
      time.className = "note-row-time";
      time.textContent = relTime(n.updatedAt);
      top.append(title, time);

      const snippet = document.createElement("div");
      snippet.className = "note-row-snippet" + (isImageBody(n.body) ? " has-thumb" : "");
      if (isImageBody(n.body)) {
        const img = document.createElement("img");
        img.className = "note-thumb";
        img.src = n.body;
        img.alt = "";
        img.loading = "lazy";
        img.decoding = "async";
        snippet.append(img);
      } else {
        snippet.textContent = n.body.slice(0, 220);
      }

      li.append(top, snippet);
      if (pending.has(n.id)) {
        const dot = document.createElement("span");
        dot.className = "sync-dot";
        li.append(dot);
      }
      list.append(li);
    });

    $("emptyState").hidden = shown.length > 0;
    if (!shown.length) {
      $("emptyText").innerHTML = q
        ? `Nothing matches “${q.replace(/[<>&]/g, "")}”`
        : "Nothing here yet.<br>Tap the pill and talk.";
    }
    const parts = [`${this.notes.length} note${this.notes.length === 1 ? "" : "s"}`];
    if (pending.size) parts.push(`${pending.size} queued`);
    $("noteCount").textContent = parts.join(" · ");
  }

  _markActiveRow() {
    for (const li of $("noteList").children) {
      li.classList.toggle("active", li.dataset.id === this.activeId);
    }
  }

  _onRemote(notes) {
    this.notes = notes;
    this.renderList();
    if (this.activeId) {
      const n = this.notes.find(n => n.id === this.activeId);
      if (!n) {                                     // deleted elsewhere
        location.hash = "#/";
      } else if (document.activeElement !== $("noteBody")
                 && document.activeElement !== $("noteTitle")) {
        this._fillEditor(n);                        // don't stomp on typing
      }
    }
  }

  // ---------------- editor ----------------

  // a blank note for typing or pasting — same note flow as everything else
  async newNote() {
    try {
      const n = await this.adapter.create({ title: "New note", body: "" });
      this._cache(n);
      this.renderList();
      location.hash = `#/note/${n.id}`;
      setTimeout(() => $("noteBody").focus(), 80);  // after the router opens it
    } catch (e) { this.toast(e.message || "Couldn't create a note"); }
  }

  // ---------------- clipboard drops: images & text become notes ----------------

  async saveImageFiles(files) {
    const images = [...files].filter(f => f.type.startsWith("image/")).slice(0, 6);
    let saved = 0;
    for (const f of images) {
      try {
        const body = await fileToImageBody(f);
        const stem = (f.name || "").replace(/\.[^.]+$/, "").trim();
        const title = stem && !/^(image|img|download|unnamed)$/i.test(stem)
          ? stem.slice(0, 60) : photoTitle();
        this._cache(await this.adapter.create({ title, body }));
        saved++;
      } catch (e) { this.toast(e.message || "Couldn't save that image"); }
    }
    if (saved) {
      this.renderList();
      this.toast(saved === 1 ? "Image saved to notes" : `${saved} images saved`);
    }
    return saved;
  }

  async saveTextClip(text) {
    text = (text || "").replace(/\r\n/g, "\n").trim();
    if (!text) return;
    try {
      this._cache(await this.adapter.create({ title: noteTitleFrom(text), body: text }));
      this.renderList();
      this.toast("Saved to notes");
    } catch (e) { this.toast(e.message || "Couldn't save"); }
  }

  async saveDroppedTextFile(f) {
    const texty = f.type.startsWith("text/") || /\.(txt|md|markdown|csv|log|json|xml|ya?ml|ini|py|js|ts|html|css)$/i.test(f.name || "");
    if (!texty) {
      this.toast(`Can't save ${f.name || "that file"} — images and text only`);
      return;
    }
    if (f.size > 200 * 1024) {
      this.toast(`${f.name} is too big for a note`);
      return;
    }
    const text = await f.text();
    if (!text.trim()) return;
    const stem = (f.name || "").replace(/\.[^.]+$/, "").trim();
    try {
      this._cache(await this.adapter.create({
        title: stem.slice(0, 60) || noteTitleFrom(text), body: text,
      }));
      this.renderList();
      this.toast("Saved to notes");
    } catch (e) { this.toast(e.message || "Couldn't save"); }
  }

  openNote(id) {
    const n = this.notes.find(n => n.id === id);
    if (!n) { location.hash = "#/"; return; }
    this._saveBody.flush();
    this.activeId = id;
    this._fillEditor(n);
    $("noteInner").hidden = false;
    $("noteBlank").hidden = true;
    document.body.classList.add("view-note");
    this._disarmDelete();
    this._markActiveRow();
  }

  _fillEditor(n) {
    const image = isImageBody(n.body);
    $("noteTitle").value = n.title;
    $("noteBody").value = image ? "" : n.body;
    $("noteBody").hidden = image;
    $("noteImageWrap").hidden = !image;
    $("noteImage").src = image ? n.body : "";
    $("shareBtn").hidden = !image;
    this._renderMeta(n);
  }

  _renderMeta(n) {
    const created = n.createdAt ? new Date(n.createdAt).toLocaleDateString(
      undefined, { day: "numeric", month: "short", year: "numeric" }) : "";
    const bits = [];
    if (created) bits.push(`created ${created}`);
    if (isImageBody(n.body)) {
      bits.push(`image · ${imageKb(n.body)} kB`);
    } else {
      const words = n.body.trim() ? n.body.trim().split(/\s+/).length : 0;
      bits.push(`${words} word${words === 1 ? "" : "s"}`);
    }
    if (this.adapter.pendingIds().has(n.id)) bits.push("<b>queued</b>");
    $("noteMeta").innerHTML = bits.join(" · ");
  }

  _cache(updated) {
    const i = this.notes.findIndex(n => n.id === updated.id);
    if (i >= 0) this.notes[i] = updated; else this.notes.unshift(updated);
    this.notes.sort((a, b) => b.updatedAt - a.updatedAt);
  }

  async _commitBody() {
    const id = this.activeId;
    if (!id) return;
    const current = this.notes.find(n => n.id === id);
    if (current && isImageBody(current.body)) return;   // images aren't edited
    try {
      const updated = await this.adapter.update(id, $("noteBody").value);
      if (updated) {
        this._cache(updated);
        this.renderList();
        if (this.activeId === id) this._renderMeta(updated);
      }
    } catch (e) { this.toast(e.message || "Couldn't save"); }
  }

  async _commitTitle() {
    const id = this.activeId;
    const n = this.notes.find(n => n.id === id);
    const wanted = $("noteTitle").value.trim();
    if (!id || !n || !wanted || wanted === n.title) {
      if (n) $("noteTitle").value = n.title;
      return;
    }
    try {
      const updated = await this.adapter.rename(id, wanted);
      if (updated) {
        this._cache(updated);
        $("noteTitle").value = updated.title;   // server may suffix " (2)"
        this.renderList();
      }
    } catch (e) { this.toast(e.message || "Couldn't rename"); }
  }

  async _deleteActive() {
    const id = this.activeId;
    if (!id) return;
    try {
      await this.adapter.remove(id);
      this.notes = this.notes.filter(n => n.id !== id);
      location.hash = "#/";
      this.renderList();
      this.toast("Note deleted");
    } catch (e) { this.toast(e.message || "Couldn't delete"); }
  }

  _disarmDelete() {
    clearTimeout(this._disarmTimer);
    const btn = $("deleteBtn");
    btn.classList.remove("armed");
    btn.textContent = "Delete";
  }

  // ---------------- status ----------------

  async _pollStatus() {
    const s = await this.adapter.status();
    const dot = $("statusDot"), text = $("statusText");
    const pending = this.adapter.pendingIds().size;
    dot.className = "status-dot";
    if (pending) {
      dot.classList.add("pending");
      text.textContent = `${pending} queued`;
    } else if (s.sync === "ok") {
      dot.classList.add("ok");
      text.textContent = "synced";
    } else if (s.sync === "off") {
      text.textContent = "local";
    } else if (s.sync === "offline") {
      text.textContent = "offline";
    } else if (s.sync === "gone") {
      dot.classList.add("err");
      text.textContent = "app closed";
    } else if (s.sync === "needs-signin") {
      dot.classList.add("err");
      text.textContent = "sign in again";
    } else if (s.sync === "starting") {
      text.textContent = "connecting";
    } else {
      dot.classList.add("err");
      text.textContent = "sync error";
    }
  }

  // ---------------- misc ----------------

  toast(msg, ms = 2200) {
    const t = $("toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => t.classList.remove("show"), ms);
  }

  setBrandLive(on) { $("brandPill").classList.toggle("live", on); }

  // ---------------- events ----------------

  _bind() {
    // list interactions
    $("noteList").addEventListener("click", e => {
      const li = e.target.closest(".note-row");
      if (li) location.hash = `#/note/${li.dataset.id}`;
    });
    $("noteList").addEventListener("keydown", e => {
      const li = e.target.closest(".note-row");
      if (li && (e.key === "Enter" || e.key === " ")) {
        e.preventDefault();
        location.hash = `#/note/${li.dataset.id}`;
      }
    });

    // search (desktop + mobile inputs stay in step)
    const onSearch = e => {
      this.filter = e.target.value;
      for (const el of [$("search"), $("searchMobile")]) {
        if (el !== e.target) el.value = e.target.value;
      }
      this.renderList();
    };
    $("search").addEventListener("input", onSearch);
    $("searchMobile").addEventListener("input", onSearch);
    addEventListener("keydown", e => {
      if (e.key === "/" && !/INPUT|TEXTAREA/.test(document.activeElement.tagName)) {
        e.preventDefault();
        (innerWidth >= 900 ? $("search") : $("searchMobile")).focus();
      }
    });

    // editor
    $("noteBody").addEventListener("input", () => this._saveBody());
    $("noteTitle").addEventListener("blur", () => this._commitTitle());
    $("noteTitle").addEventListener("keydown", e => {
      if (e.key === "Enter") { e.preventDefault(); e.target.blur(); }
    });
    $("backBtn").addEventListener("click", () => { location.hash = "#/"; });
    $("micBackBtn").addEventListener("click", () => { location.hash = "#/"; });

    $("copyBtn").addEventListener("click", async () => {
      const n = this.notes.find(x => x.id === this.activeId);
      const image = n && isImageBody(n.body);
      try {
        if (image) {
          // promise-valued ClipboardItem keeps the user gesture alive
          await navigator.clipboard.write([
            new ClipboardItem({ "image/png": imageBodyToPngBlob(n.body) })]);
        } else {
          await navigator.clipboard.writeText($("noteBody").value);
        }
        const btn = $("copyBtn");
        btn.classList.add("flash");
        btn.textContent = "Copied";
        setTimeout(() => { btn.classList.remove("flash"); btn.textContent = "Copy"; }, 1400);
      } catch {
        this.toast(image ? "Couldn't copy — long-press the image instead"
                         : "Couldn't reach the clipboard");
      }
    });

    $("shareBtn").addEventListener("click", async () => {
      const n = this.notes.find(x => x.id === this.activeId);
      if (!n || !isImageBody(n.body)) return;
      const file = imageBodyToFile(n.body, n.title);
      if (navigator.canShare?.({ files: [file] })) {
        try { await navigator.share({ files: [file], title: n.title }); }
        catch { /* user closed the share sheet */ }
      } else {
        const a = document.createElement("a");     // desktop: download instead
        a.href = n.body;
        a.download = file.name;
        a.click();
      }
    });

    $("deleteBtn").addEventListener("click", () => {
      const btn = $("deleteBtn");
      if (!btn.classList.contains("armed")) {
        btn.classList.add("armed");
        btn.textContent = "Sure?";
        this._disarmTimer = setTimeout(() => this._disarmDelete(), 2600);
      } else {
        this._disarmDelete();
        this._deleteActive();
      }
    });

    $("micFab").addEventListener("click", () => { location.hash = "#/mic"; });
    $("newNoteBtn").addEventListener("click", () => this.newNote());

    // add an image: camera or gallery on the phone, file picker on desktop
    $("addImageBtn").addEventListener("click", () => $("imageInput").click());
    $("imageInput").addEventListener("change", async e => {
      await this.saveImageFiles(e.target.files);
      e.target.value = "";
    });

    // paste anywhere: an image always becomes a new note; text becomes one
    // too unless you're typing in a box (that stays a normal paste)
    document.addEventListener("paste", e => {
      const files = [...(e.clipboardData?.files || [])]
        .filter(f => f.type.startsWith("image/"));
      if (files.length) {
        e.preventDefault();
        this.saveImageFiles(files);
        return;
      }
      const tag = document.activeElement?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA") return;
      const text = e.clipboardData?.getData("text/plain");
      if (text?.trim()) {
        e.preventDefault();
        this.saveTextClip(text);
      }
    });

    // drag anything onto the window (desktop browsers): same rules
    this._dragDepth = 0;
    const hint = show => { $("dropHint").hidden = !show; };
    addEventListener("dragenter", e => {
      const types = e.dataTransfer ? [...e.dataTransfer.types] : [];
      if (types.includes("Files") || types.includes("text/plain")) {
        e.preventDefault();
        this._dragDepth++;
        hint(true);
      }
    });
    addEventListener("dragover", e => e.preventDefault());
    addEventListener("dragleave", () => {
      if (--this._dragDepth <= 0) { this._dragDepth = 0; hint(false); }
    });
    addEventListener("drop", async e => {
      e.preventDefault();
      this._dragDepth = 0;
      hint(false);
      const dt = e.dataTransfer;
      if (!dt) return;
      const files = [...dt.files];
      if (files.length) {
        await this.saveImageFiles(files.filter(f => f.type.startsWith("image/")));
        for (const f of files.filter(f => !f.type.startsWith("image/")).slice(0, 4)) {
          await this.saveDroppedTextFile(f);
        }
        return;
      }
      const text = dt.getData("text/plain");
      if (text?.trim()) this.saveTextClip(text);
    });

    // flush pending edits when leaving
    addEventListener("pagehide", () => this._saveBody.flush());
    addEventListener("visibilitychange", () => {
      if (document.hidden) this._saveBody.flush();
    });
  }
}
