// All DOM logic for the notes app. Views: list / note / mic (routes via
// location.hash, panes toggled with body classes — see styles.css).

import { relTime, dayKey, dayHeading, debounce, noteTitleFrom } from "./util.js";
import {
  isImageBody, imageKb, fileToImageBody, imageBodyToPngBlob,
  imageBodyToFile, photoTitle,
} from "./imgnote.js";
import {
  isFileBody, fileMeta, fmtBytes, fileBodyToFile, fileToFileBody, SHEET_EXT_RE,
} from "./filenote.js";

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
    const searchText = n => {
      if (isImageBody(n.body)) return n.title + "\nimage photo";
      if (isFileBody(n.body)) {                       // don't grep base64 soup
        const f = fileMeta(n.body);
        return `${n.title}\n${f.name}\n${f.ext}\nfile document`;
      }
      return n.title + "\n" + n.body;
    };
    const shown = q
      ? this.notes.filter(n => searchText(n).toLowerCase().includes(q))
      : this.notes;
    const pending = this.adapter.pendingIds();

    list.textContent = "";
    let lastDay = null;                 // notes are newest-first, so each
    shown.forEach((n, i) => {           // day's notes sit together already
      const day = n.updatedAt ? dayKey(n.updatedAt) : lastDay;
      if (day !== lastDay) {
        lastDay = day;
        const head = document.createElement("li");
        head.className = "day-head";
        head.textContent = dayHeading(n.updatedAt);
        list.append(head);
      }
      const li = document.createElement("li");
      li.className = "note-row" + (n.id === this.activeId ? " active" : "");
      li.dataset.id = n.id;
      li.tabIndex = 0;
      // image/file rows can be dragged straight out of the list as real files
      if (isImageBody(n.body) || isFileBody(n.body)) li.draggable = true;
      li.style.animationDelay = `${Math.min(i, 14) * 12}ms`;

      const top = document.createElement("div");
      top.className = "note-row-top";
      const title = document.createElement("span");
      title.className = "note-row-title";
      title.textContent = n.title;
      const time = document.createElement("span");
      time.className = "note-row-time";
      time.textContent = relTime(n.updatedAt);
      const del = document.createElement("button");
      del.className = "row-del";
      del.textContent = "×";
      del.setAttribute("aria-label", `Delete ${n.title}`);
      top.append(title, time, del);

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
      } else if (isFileBody(n.body)) {
        const f = fileMeta(n.body);
        const chip = document.createElement("span");
        chip.className = "file-chip";
        const badge = document.createElement("span");
        badge.className = "file-badge small";
        badge.textContent = f.ext || "FILE";
        const label = document.createElement("span");
        label.className = "file-chip-name";
        label.textContent = `${f.name} · ${fmtBytes(f.bytes)}`;
        chip.append(badge, label);
        snippet.append(chip);
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

  // one router for every picked/dropped/pasted file: images -> image notes,
  // small text -> editable text notes, any other document -> file note
  async saveAnyFiles(files) {
    for (const f of [...files].slice(0, 10)) {
      if (f.type.startsWith("image/")) {
        await this.saveImageFiles([f]);
        continue;
      }
      // spreadsheets are never editable text — they stay real file notes so
      // Open can hand them to Excel (same rule as the pill's dropnotes.py)
      const sheet = SHEET_EXT_RE.test(f.name || "") || f.type === "text/csv";
      const texty = !sheet && (f.type.startsWith("text/")
        || /\.(txt|md|markdown|log|json|xml|ya?ml|ini|py|js|ts|html|css)$/i.test(f.name || ""));
      if (texty && f.size <= 200 * 1024) {
        await this.saveDroppedTextFile(f);
        continue;
      }
      try {
        const body = await fileToFileBody(f);
        const stem = (f.name || "").replace(/\.[^.]+$/, "").trim();
        this._cache(await this.adapter.create({
          title: stem.slice(0, 60) || (f.name || "File").slice(0, 60), body,
        }));
        this.renderList();
        this.toast(`${f.name || "File"} saved to notes`);
      } catch (e) {
        this.toast(e.message || `Couldn't save ${f.name || "that file"}`);
      }
    }
  }

  async saveDroppedTextFile(f) {
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
    const file = !image && isFileBody(n.body);
    $("noteTitle").value = n.title;
    $("noteBody").value = image || file ? "" : n.body;
    $("noteBody").hidden = image || file;
    $("noteImageWrap").hidden = !image;
    $("noteImage").src = image ? n.body : "";
    $("noteFileWrap").hidden = !file;
    if (file) {
      const f = fileMeta(n.body);
      $("noteFileBadge").textContent = f.ext || "FILE";
      $("noteFileName").textContent = f.name;
      $("noteFileSize").textContent = fmtBytes(f.bytes);
    }
    $("shareBtn").hidden = !(image || file);
    $("copyBtn").hidden = file;                 // nothing sensible to copy
    this._renderMeta(n);
  }

  _renderMeta(n) {
    const created = n.createdAt ? new Date(n.createdAt).toLocaleDateString(
      undefined, { day: "numeric", month: "short", year: "numeric" }) : "";
    const bits = [];
    if (created) bits.push(`created ${created}`);
    if (isImageBody(n.body)) {
      bits.push(`image · ${imageKb(n.body)} kB`);
    } else if (isFileBody(n.body)) {
      const f = fileMeta(n.body);
      bits.push(`${f.ext || "file"} · ${fmtBytes(f.bytes)}`);
    } else {
      const words = n.body.trim() ? n.body.trim().split(/\s+/).length : 0;
      bits.push(`${words} word${words === 1 ? "" : "s"}`);
    }
    if (this.adapter.pendingIds().has(n.id)) bits.push("<b>saved · syncing…</b>");
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
    if (current && (isImageBody(current.body)
        || isFileBody(current.body))) return;   // images/files aren't edited
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

  async _deleteActive() { return this._deleteNote(this.activeId); }

  async _deleteNote(id) {
    if (!id) return;
    try {
      await this.adapter.remove(id);
      this.notes = this.notes.filter(n => n.id !== id);
      if (this.activeId === id) location.hash = "#/";
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
      // everything pending IS saved on this device — say so, plus why
      // it hasn't reached the cloud yet
      dot.classList.add("pending");
      text.textContent = s.sync === "offline" ? "saved · offline"
        : s.sync === "needs-signin" ? "saved · sign in to sync"
        : s.sync === "error" ? "saved · retrying"
        : "saved · sending…";
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
    // list interactions (the row × arms first — a stray tap can't delete)
    $("noteList").addEventListener("click", e => {
      const del = e.target.closest(".row-del");
      if (del) {
        e.stopPropagation();
        const id = del.closest(".note-row")?.dataset.id;
        if (!del.classList.contains("armed")) {
          for (const b of $("noteList").querySelectorAll(".row-del.armed")) {
            b.classList.remove("armed");
            b.textContent = "×";
          }
          del.classList.add("armed");
          del.textContent = "sure?";
          setTimeout(() => {
            del.classList.remove("armed");
            del.textContent = "×";
          }, 2600);
        } else {
          this._deleteNote(id);
        }
        return;
      }
      const li = e.target.closest(".note-row");
      if (li) location.hash = `#/note/${li.dataset.id}`;
    });
    $("noteList").addEventListener("keydown", e => {
      if (e.target.closest(".row-del")) return;   // Enter there = the ×
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

    // editor — saves as you type; Ctrl+S just reassures
    addEventListener("keydown", e => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
        e.preventDefault();
        this._saveBody.flush();
        this.toast("Saved — notes save themselves as you type");
      }
    });
    $("noteBody").addEventListener("input", () => this._saveBody());
    $("noteTitle").addEventListener("blur", () => this._commitTitle());
    $("renameBtn").addEventListener("click", () => {
      const t = $("noteTitle");
      t.focus();
      t.select();
    });
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

    // real-file helpers shared by Share's desktop fallback, Open, and Download
    const triggerDownload = file => {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(file);
      a.download = file.name;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 30000);
    };
    const openInTab = blob => {
      const url = URL.createObjectURL(blob);
      const win = open(url, "_blank");
      if (!win) this.toast("Pop-up blocked — use Share instead");
      setTimeout(() => URL.revokeObjectURL(url), 60000);
    };

    $("shareBtn").addEventListener("click", async () => {
      const n = this.notes.find(x => x.id === this.activeId);
      if (!n) return;
      let file;
      if (isImageBody(n.body)) file = imageBodyToFile(n.body, n.title);
      else if (isFileBody(n.body)) file = fileBodyToFile(n.body);
      else return;
      if (navigator.canShare?.({ files: [file] })) {
        try { await navigator.share({ files: [file], title: n.title }); }
        catch { /* user closed the share sheet */ }
      } else {
        triggerDownload(file);       // desktop: download instead
      }
    });

    // file notes: Open hands the file to the device, never a viewer in
    // here. PDFs, images and spreadsheets go to a new tab (browsers render
    // those — CSV/TSV as plain text, since there's no built-in table
    // viewer); everything else — Word docs and friends — downloads under
    // its real name so the default app opens it.
    $("fileOpenBtn").addEventListener("click", () => {
      const n = this.notes.find(x => x.id === this.activeId);
      if (!n || !isFileBody(n.body)) return;
      const f = fileMeta(n.body);
      const file = fileBodyToFile(n.body);
      const sheet = SHEET_EXT_RE.test(f.name) || f.mime === "text/csv";
      if (f.mime === "application/pdf" || f.mime.startsWith("image/")) {
        return openInTab(file);
      }
      if (sheet) {
        return openInTab(new Blob([file], { type: "text/plain;charset=utf-8" }));
      }
      triggerDownload(file);
      this.toast(`${file.name} is in your Downloads — it opens in your usual app`);
    });

    $("fileDownloadBtn").addEventListener("click", () => {
      const n = this.notes.find(x => x.id === this.activeId);
      if (!n || !isFileBody(n.body)) return;
      const file = fileBodyToFile(n.body);
      triggerDownload(file);
      this.toast(`${file.name} is in your Downloads`);
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

    // add a file: any document from Files/Explorer becomes a file note
    $("addFileBtn").addEventListener("click", () => $("fileInput").click());
    $("fileInput").addEventListener("change", async e => {
      await this.saveAnyFiles(e.target.files);
      e.target.value = "";
    });

    // dragging a note OUT of the app: hand the other side a real file,
    // never the base64 body. DownloadURL means dropping on the desktop or
    // Explorer writes the actual image/document; the text flavour is just
    // the filename, so a terminal or chat gets one line, not base64 soup.
    const dragOut = (e, n) => {
      if (!n) return;
      let file;
      if (isImageBody(n.body)) file = imageBodyToFile(n.body, n.title);
      else if (isFileBody(n.body)) file = fileBodyToFile(n.body);
      else return;                        // text notes keep the normal drag
      if (this._dragUrl) URL.revokeObjectURL(this._dragUrl);
      const url = this._dragUrl = URL.createObjectURL(file);
      e.dataTransfer.clearData();
      e.dataTransfer.setData("DownloadURL", `${file.type}:${file.name}:${url}`);
      e.dataTransfer.setData("text/plain", file.name);
      e.dataTransfer.effectAllowed = "copy";
    };
    // only our own drag-outs carry the DownloadURL flavour — checking the
    // drag itself (not a flag) can't get stuck if a mid-drag list re-render
    // eats the dragend event
    const ownDrag = dt =>
      dt && [...dt.types].some(t => t.toLowerCase() === "downloadurl");
    $("noteList").addEventListener("dragstart", e => {
      const li = e.target.closest(".note-row");
      if (li) dragOut(e, this.notes.find(n => n.id === li.dataset.id));
    });
    for (const id of ["noteImage", "noteFileWrap"]) {
      $(id).addEventListener("dragstart", e =>
        dragOut(e, this.notes.find(n => n.id === this.activeId)));
    }
    addEventListener("dragend", () => {
      if (this._dragUrl) {                // let a slow drop finish writing
        const u = this._dragUrl;
        this._dragUrl = null;
        setTimeout(() => URL.revokeObjectURL(u), 30000);
      }
    });

    // paste anywhere: files/images always become new notes; text becomes one
    // too unless you're typing in a box (that stays a normal paste)
    document.addEventListener("paste", e => {
      const files = [...(e.clipboardData?.files || [])];
      if (files.length) {
        e.preventDefault();
        this.saveAnyFiles(files);
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
      if (ownDrag(e.dataTransfer)) return;   // one of our notes on its way out
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
      if (!dt || ownDrag(dt)) return;     // don't re-save a note onto itself

      const files = [...dt.files];
      if (files.length) {
        await this.saveAnyFiles(files);
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
