"""
notestore — single owner of the notes\\ folder.

Every note is a plain .txt whose filename stem is its title, exactly as
before. This module adds a sidecar index (notes\\.sync-state.json) that gives
each file a stable id, remembers what has been synced, and detects local
creates / edits / renames / deletes by stat + content hash. Both the local
web UI and the cloud sync engine go through here, guarded by one lock.

Works standalone: with sync disabled the index is still maintained so the
notes UI has stable ids.
"""

import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid

# ----------------------------------------------------------------------------
# Title helpers (moved verbatim from app.py; app.py imports them back)
# ----------------------------------------------------------------------------

_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL",
                   *(f"COM{i}" for i in range(1, 10)),
                   *(f"LPT{i}" for i in range(1, 10))}

def sanitize_title(name):
    """A note title that Windows will accept as a file name."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", str(name))
    name = re.sub(r"\s{2,}", " ", name).strip(" .")
    if name.upper() in _RESERVED_NAMES:
        name = "Note " + name
    return name[:80].strip(" .")

def note_title_from(text):
    title = sanitize_title(" ".join(text.split()[:7]))
    return title[:60].strip(" .,!?;:") or "Note"


def _now_ms():
    return int(time.time() * 1000)

def _hash(body):
    return hashlib.sha1(body.encode("utf-8", "replace")).hexdigest()


class NoteStore:
    """All reads and writes of notes\\*.txt and the sync index."""

    INDEX_NAME = ".sync-state.json"
    TOMBSTONE_KEEP_MS = 30 * 24 * 3600 * 1000

    def __init__(self, notes_dir, dbg=lambda m: None):
        self.dir = notes_dir
        self.dbg = dbg
        self.lock = threading.RLock()
        # while True, deleted notes linger as pending tombstones until the
        # sync engine pushes them; while False they are dropped immediately
        self.keep_deletes = False
        self._listeners = []
        # index: id -> {filename, title, hash, size, mtime, createdAt,
        #               syncedRev, dirty, deletedLocally}
        self.notes = {}
        self.tombstones = {}   # id -> updatedAt of the applied remote delete
        os.makedirs(self.dir, exist_ok=True)
        self._load_index()
        self.scan()
        self._backfill_files()

    # ---------------- change notifications ----------------

    def subscribe(self, cb):
        """cb(kind, note_id) — kinds: create/update/rename/delete, and the
        remote_* variants for changes applied from the cloud."""
        self._listeners.append(cb)

    def _notify(self, kind, note_id):
        for cb in list(self._listeners):
            try:
                cb(kind, note_id)
            except Exception:
                pass

    # ---------------- index persistence ----------------

    def _index_path(self):
        return os.path.join(self.dir, self.INDEX_NAME)

    def _load_index(self):
        try:
            with open(self._index_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            self.notes = data.get("notes", {}) or {}
            self.tombstones = data.get("tombstones", {}) or {}
        except Exception:
            self.notes, self.tombstones = {}, {}

    def _save_index(self):
        cutoff = _now_ms() - self.TOMBSTONE_KEEP_MS
        self.tombstones = {i: t for i, t in self.tombstones.items() if t >= cutoff}
        data = {"version": 1, "notes": self.notes, "tombstones": self.tombstones}
        tmp = self._index_path() + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)
            os.replace(tmp, self._index_path())
        except OSError as ex:
            self.dbg(f"notestore: index save failed: {ex}")

    # ---------------- file helpers ----------------

    def _path(self, filename):
        return os.path.join(self.dir, filename)

    def _write_file(self, filename, body):
        os.makedirs(self.dir, exist_ok=True)
        tmp = self._path(filename) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp, self._path(filename))

    def _read_file(self, filename):
        with open(self._path(filename), "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    # ---------------- real files for file/image notes ----------------
    # A note whose body is a data URL (a dropped PDF, doc, spreadsheet or
    # photo) also lives as the actual file in notes\files — openable and
    # copyable like any document, whether it was dropped on the pill or
    # arrived from the phone. e["file"] tracks the copy we own so deletes
    # clean it up. The sync contract (data-URL bodies in .txt) is untouched.

    FILES_DIRNAME = "files"

    def files_dir(self):
        return os.path.join(self.dir, self.FILES_DIRNAME)

    def _unique_real_name(self, name):
        base, ext = os.path.splitext(name)
        cand, n = name, 2
        while os.path.exists(os.path.join(self.files_dir(), cand)):
            cand = f"{base} ({n}){ext}"
            n += 1
        return cand

    def _materialize(self, e, body, src_path=None):
        """Write/refresh the real file for a file or image note body.
        src_path: the original dropped file — copied verbatim when given
        (keeps photos full-resolution; the note body is the compressed
        sync copy)."""
        try:
            import dropnotes
            decoded = dropnotes.decode_file_body(body)
            if decoded:
                name, raw = decoded
            else:
                img = dropnotes.decode_image_body(body)
                if img is None:
                    self._unmaterialize(e)
                    return
                ext, raw = img
                name = (e.get("title") or "Image") + ext
            if src_path and os.path.isfile(src_path):
                name, raw = os.path.basename(src_path), None
            self._unmaterialize(e)
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", name).strip() or "file"
            os.makedirs(self.files_dir(), exist_ok=True)
            name = self._unique_real_name(name)
            dest = os.path.join(self.files_dir(), name)
            if raw is None:
                shutil.copy2(src_path, dest)
            else:
                with open(dest + ".tmp", "wb") as f:
                    f.write(raw)
                os.replace(dest + ".tmp", dest)
            e["file"] = name
        except Exception as ex:
            self.dbg(f"notestore: couldn't write real file: {ex}")

    def _unmaterialize(self, e):
        name = e.pop("file", None) if e else None
        if name:
            try:
                os.remove(os.path.join(self.files_dir(), name))
            except OSError:
                pass

    def _backfill_files(self):
        """One-off catch-up for file/image notes saved before notes\\files
        existed: give each its real file. Entries that already have one
        (even if the user then deleted it by hand) are left alone."""
        with self.lock:
            changed = False
            for e in self.notes.values():
                if "file" in e or e.get("deletedLocally"):
                    continue
                try:
                    body = self._read_file(e["filename"])
                except OSError:
                    continue
                if body.startswith("data:"):
                    self._materialize(e, body)
                    changed = "file" in e or changed
            if changed:
                self._save_index()

    def _unique_filename(self, title, keep_id=None):
        """'title.txt', suffixing ' (2)' while the name belongs to another
        note or an untracked file. keep_id's own current name never counts."""
        base = sanitize_title(title) or "Note"
        keep = (self.notes[keep_id]["filename"].lower()
                if keep_id and keep_id in self.notes else None)

        def taken(name):
            if name.lower() == keep:
                return False
            owner = self._filename_id(name)
            if owner is not None and owner != keep_id:
                return True
            return owner is None and os.path.exists(self._path(name))

        name, n = base + ".txt", 2
        while taken(name):
            name = f"{base} ({n}).txt"
            n += 1
        return name

    def _filename_id(self, filename):
        low = filename.lower()
        for i, e in self.notes.items():
            if e["filename"].lower() == low:
                return i
        return None

    # ---------------- public snapshots ----------------

    def all_notes(self):
        """Newest first: [{id, title, body, createdAt, updatedAt, starred, starredAt}]"""
        with self.lock:
            out = []
            for i, e in self.notes.items():
                if e.get("deletedLocally"):
                    continue
                try:
                    body = self._read_file(e["filename"])
                except OSError:
                    continue
                n = {"id": i, "title": e["title"], "body": body,
                     "createdAt": e.get("createdAt") or 0,
                     "updatedAt": int(e.get("mtime", 0) * 1000),
                     "starred": bool(e.get("starred")),
                     "starredAt": int(e.get("starredAt") or 0)}
                if e.get("calendar"):
                    n["calendar"] = e["calendar"]
                out.append(n)
            out.sort(key=lambda n: n["updatedAt"], reverse=True)
            return out

    def get(self, note_id):
        with self.lock:
            e = self.notes.get(note_id)
            if not e or e.get("deletedLocally"):
                return None
            try:
                body = self._read_file(e["filename"])
            except OSError:
                return None
            n = {"id": note_id, "title": e["title"], "body": body,
                 "createdAt": e.get("createdAt") or 0,
                 "updatedAt": int(e.get("mtime", 0) * 1000),
                 "starred": bool(e.get("starred")),
                 "starredAt": int(e.get("starredAt") or 0)}
            if e.get("calendar"):
                n["calendar"] = e["calendar"]
            return n

    def entry(self, note_id):
        with self.lock:
            e = self.notes.get(note_id)
            return dict(e) if e else None

    def dirty_ids(self):
        with self.lock:
            return [i for i, e in self.notes.items() if e.get("dirty")]

    def star_dirty_ids(self):
        """Notes whose star was toggled locally and not yet pushed. Kept apart
        from `dirty` so a star never re-pushes (or bumps) the note body."""
        with self.lock:
            return [i for i, e in self.notes.items() if e.get("starDirty")]

    def calendar_dirty_ids(self):
        """Notes whose calendar link hasn't reached the cloud yet. Like stars,
        the calendar field rides its own push and never bumps the body."""
        with self.lock:
            return [i for i, e in self.notes.items() if e.get("calendarDirty")]

    # ---------------- local mutations (UI / dictation) ----------------

    def create(self, title, body, note_id=None, src_path=None):
        with self.lock:
            note_id = note_id or uuid.uuid4().hex
            filename = self._unique_filename(title or note_title_from(body))
            self._write_file(filename, body)
            st = os.stat(self._path(filename))
            self.notes[note_id] = {
                "filename": filename, "title": os.path.splitext(filename)[0],
                "hash": _hash(body), "size": st.st_size, "mtime": st.st_mtime,
                "createdAt": _now_ms(), "syncedRev": 0, "dirty": True,
            }
            self._materialize(self.notes[note_id], body, src_path)
            self._save_index()
        self._notify("create", note_id)
        return self.get(note_id)

    def update(self, note_id, body):
        with self.lock:
            e = self.notes.get(note_id)
            if not e or e.get("deletedLocally"):
                return None
            self._write_file(e["filename"], body)
            st = os.stat(self._path(e["filename"]))
            e.update(hash=_hash(body), size=st.st_size, mtime=st.st_mtime, dirty=True)
            self._materialize(e, body)
            self._save_index()
        self._notify("update", note_id)
        return self.get(note_id)

    def rename(self, note_id, new_title):
        with self.lock:
            e = self.notes.get(note_id)
            if not e or e.get("deletedLocally"):
                return None
            new_name = self._unique_filename(new_title, keep_id=note_id)
            if new_name != e["filename"]:
                os.replace(self._path(e["filename"]), self._path(new_name))
                e["filename"] = new_name
            e["title"] = os.path.splitext(new_name)[0]
            e["dirty"] = True
            st = os.stat(self._path(new_name))
            e.update(size=st.st_size, mtime=st.st_mtime)
            self._save_index()
        self._notify("rename", note_id)
        return self.get(note_id)

    def set_star(self, note_id, starred):
        """Flag/unflag a note. The star is index-only metadata (never the .txt
        body), stamped with a local timestamp and marked to push on its own."""
        with self.lock:
            e = self.notes.get(note_id)
            if not e or e.get("deletedLocally"):
                return None
            e["starred"] = bool(starred)
            e["starredAt"] = _now_ms()
            e["starDirty"] = True
            self._save_index()
        self._notify("star", note_id)
        return self.get(note_id)

    def mark_star_synced(self, note_id, pushed_at):
        """The star reached the cloud — clear its pending flag, but only if no
        newer toggle happened while the push was in flight (pushed_at is the
        starredAt that was actually sent). Otherwise the newer star re-pushes."""
        with self.lock:
            e = self.notes.get(note_id)
            if e and int(e.get("starredAt") or 0) <= int(pushed_at or 0):
                e["starDirty"] = False
                self._save_index()

    # ---------------- calendar link (laptop is the only writer) ----------------

    def set_calendar(self, note_id, meta):
        """Stamp a note with its calendar event (or the failure to make one).
        meta: {status, provider, eventId, link, start, end, allDay, addedAt,
        bodyHash} — index-only metadata, pushed on its own like a star so the
        note keeps its place in the list."""
        with self.lock:
            e = self.notes.get(note_id)
            if not e or e.get("deletedLocally"):
                return None
            e["calendar"] = meta
            e["calendarDirty"] = True
            self._save_index()
        self._notify("calendar", note_id)
        return self.get(note_id)

    def mark_calendar_synced(self, note_id, pushed_added_at):
        """Clear the pending flag unless a newer link landed mid-push."""
        with self.lock:
            e = self.notes.get(note_id)
            if e and int((e.get("calendar") or {}).get("addedAt") or 0) \
                    <= int(pushed_added_at or 0):
                e["calendarDirty"] = False
                self._save_index()

    def set_remote_calendar(self, note_id, meta):
        """Adopt a calendar link arriving from the cloud (our own echo, or a
        fresh install pulling history). The laptop is the only writer, so
        there's no timestamp race to arbitrate — just never let an inbound
        copy clobber a link that is still waiting to push."""
        if not isinstance(meta, dict):
            return ""
        with self.lock:
            e = self.notes.get(note_id)
            if not e or e.get("deletedLocally") or e.get("calendarDirty"):
                return ""
            if e.get("calendar") == meta:
                return ""
            e["calendar"] = meta
            self._save_index()
        self._notify("remote_update", note_id)
        return "remote_update"

    def set_calendar_notified(self, note_id):
        """The pre-event heads-up fired — local bookkeeping only, never synced."""
        with self.lock:
            e = self.notes.get(note_id)
            if e:
                e["calendarNotifiedAt"] = _now_ms()
                self._save_index()

    def upcoming_calendar(self, within_ms, grace_ms=5 * 60 * 1000):
        """[(note_id, title, start_ms)] for timed events starting within
        within_ms that haven't had their pill heads-up yet. A note whose
        start slipped past while the laptop slept still fires for grace_ms."""
        now = _now_ms()
        out = []
        with self.lock:
            for i, e in self.notes.items():
                cal = e.get("calendar")
                if (not cal or e.get("deletedLocally")
                        or cal.get("status") != "ok" or cal.get("allDay")
                        or e.get("calendarNotifiedAt")):
                    continue
                start = int(cal.get("start") or 0)
                if now - grace_ms <= start <= now + within_ms:
                    out.append((i, e.get("title") or "Note", start))
        out.sort(key=lambda x: x[2])
        return out

    def calendar_agenda(self, within_ms):
        """Every linked event that is still ahead (or running right now),
        soonest first: [{id, title, start, end, allDay, link}]. Feeds the
        pill's calendar badge + dropdown — local index only, no network."""
        now = _now_ms()
        out = []
        with self.lock:
            for i, e in self.notes.items():
                cal = e.get("calendar")
                if (not cal or e.get("deletedLocally")
                        or cal.get("status") != "ok"):
                    continue
                start = int(cal.get("start") or 0)
                end = int(cal.get("end") or start)
                if end < now or start > now + within_ms:
                    continue
                out.append({"id": i, "title": e.get("title") or "Note",
                            "start": start, "end": end,
                            "allDay": bool(cal.get("allDay")),
                            "link": cal.get("link") or ""})
        out.sort(key=lambda x: x["start"])
        return out

    def delete(self, note_id):
        with self.lock:
            e = self.notes.get(note_id)
            if not e:
                return False
            try:
                os.remove(self._path(e["filename"]))
            except OSError:
                pass
            self._unmaterialize(e)
            if self.keep_deletes:
                # pending local delete until sync pushes the tombstone
                e["deletedLocally"] = True
                e["dirty"] = True
            else:
                self.notes.pop(note_id, None)
            self._save_index()
        self._notify("delete", note_id)
        return True

    def drop_entry(self, note_id, tombstone_rev=None):
        """Forget an entry once its delete has been pushed (or sync is off)."""
        with self.lock:
            self.notes.pop(note_id, None)
            if tombstone_rev:
                self.tombstones[note_id] = tombstone_rev
            self._save_index()

    def mark_synced(self, note_id, rev):
        """Record the server updatedAt after a successful push."""
        with self.lock:
            e = self.notes.get(note_id)
            if not e:
                return
            e["syncedRev"] = rev
            e["dirty"] = False
            self._save_index()

    # ---------------- remote application (cloud sync) ----------------

    def apply_remote(self, note_id, record):
        """Write a cloud record to disk and index in one step, so the next
        scan sees hash == index hash and nothing echoes back. Returns the
        kind of change applied ('' if nothing)."""
        with self.lock:
            rev = int(record.get("updatedAt") or 0)
            if record.get("deleted"):
                e = self.notes.pop(note_id, None)
                self.tombstones[note_id] = rev or _now_ms()
                if e and not e.get("deletedLocally"):
                    try:
                        os.remove(self._path(e["filename"]))
                    except OSError:
                        pass
                    self._unmaterialize(e)
                    self._save_index()
                    kind = "remote_delete"
                else:
                    self._save_index()
                    return ""
            else:
                body = record.get("body") or ""
                title = record.get("title") or "Note"
                e = self.notes.get(note_id)
                if e and e.get("deletedLocally"):
                    return ""      # local delete pending; push will decide
                if e is None:
                    filename = self._unique_filename(title)
                    self._write_file(filename, body)
                    st = os.stat(self._path(filename))
                    self.notes[note_id] = {
                        "filename": filename,
                        "title": os.path.splitext(filename)[0],
                        "hash": _hash(body), "size": st.st_size,
                        "mtime": st.st_mtime,
                        "createdAt": int(record.get("createdAt") or _now_ms()),
                        "syncedRev": rev, "dirty": False,
                        "starred": bool(record.get("starred")),
                        "starredAt": int(record.get("starredAt") or 0),
                    }
                    if isinstance(record.get("calendar"), dict):
                        self.notes[note_id]["calendar"] = record["calendar"]
                    self._materialize(self.notes[note_id], body)
                    kind = "remote_create"
                else:
                    changed = False
                    if _hash(body) != e["hash"]:
                        self._write_file(e["filename"], body)
                        self._materialize(e, body)
                        changed = True
                    want = sanitize_title(title) or "Note"
                    if want != os.path.splitext(e["filename"])[0]:
                        new_name = self._unique_filename(want, keep_id=note_id)
                        os.replace(self._path(e["filename"]), self._path(new_name))
                        e["filename"] = new_name
                        e["title"] = os.path.splitext(new_name)[0]
                        changed = True
                    st = os.stat(self._path(e["filename"]))
                    e.update(hash=_hash(body), size=st.st_size, mtime=st.st_mtime,
                             syncedRev=rev, dirty=False)
                    kind = "remote_update" if changed else ""
                self._save_index()
        if kind:
            self._notify(kind, note_id)
        return kind

    def set_remote_star(self, note_id, starred, starred_at):
        """Merge a star that arrived from the cloud. Last-writer-wins by the
        star's own timestamp, independent of the note body — an older remote
        star can't unstar something we starred more recently, and vice versa. A
        tie (equal starredAt) keeps the local value, exactly as web/js/sync.js
        mergeStar does, so the two platforms never pick opposite winners. No-op
        (returns '') for unknown/locally-deleted notes, ties and older stars."""
        starred_at = int(starred_at or 0)
        with self.lock:
            e = self.notes.get(note_id)
            if not e or e.get("deletedLocally"):
                return ""
            local_at = int(e.get("starredAt") or 0)
            if starred_at < local_at:
                # the cloud holds an older star than ours — a concurrent write
                # clobbered it (RTDB can only field-merge, never compare). Mark
                # it to re-push so every device converges on the newer star.
                if not e.get("starDirty"):
                    e["starDirty"] = True
                    self._save_index()
                return ""
            if starred_at == local_at:
                return ""                     # tie: keep local (echo of our push)
            e["starred"] = bool(starred)
            e["starredAt"] = starred_at
            e["starDirty"] = False            # this is the cloud's value already
            self._save_index()
        self._notify("remote_update", note_id)
        return "remote_update"

    # ---------------- local change detection ----------------

    def scan(self):
        """Reconcile the index with what is actually on disk. Returns True if
        anything changed (something new to push or show)."""
        with self.lock:
            try:
                names = [n for n in os.listdir(self.dir)
                         if n.lower().endswith(".txt")]
            except OSError:
                return False
            stats = {}
            for n in names:
                try:
                    stats[n] = os.stat(self._path(n))
                except OSError:
                    continue
            by_name = {e["filename"]: i for i, e in self.notes.items()
                       if not e.get("deletedLocally")}
            changed = False
            orphans = {}   # id -> entry whose file vanished

            # edits + vanished files
            for note_id, e in list(self.notes.items()):
                if e.get("deletedLocally"):
                    continue
                st = stats.get(e["filename"])
                if st is None:
                    orphans[note_id] = e
                    continue
                if (st.st_size, st.st_mtime) != (e.get("size"), e.get("mtime")):
                    try:
                        h = _hash(self._read_file(e["filename"]))
                    except OSError:
                        continue
                    e.update(size=st.st_size, mtime=st.st_mtime)
                    if h != e["hash"]:
                        e.update(hash=h, dirty=True)
                        self._notify("update", note_id)
                    changed = True

            # new files: rename detection by content hash, else create
            for n in names:
                if n in by_name or n not in stats:
                    continue
                try:
                    body = self._read_file(n)
                except OSError:
                    continue
                h = _hash(body)
                matches = [i for i, e in orphans.items() if e["hash"] == h]
                if len(matches) == 1:
                    note_id = matches.pop()
                    e = orphans.pop(note_id)
                    st = stats[n]
                    e.update(filename=n, title=os.path.splitext(n)[0],
                             size=st.st_size, mtime=st.st_mtime, dirty=True)
                    self._notify("rename", note_id)
                else:
                    note_id = uuid.uuid4().hex
                    st = stats[n]
                    self.notes[note_id] = {
                        "filename": n, "title": os.path.splitext(n)[0],
                        "hash": h, "size": st.st_size, "mtime": st.st_mtime,
                        "createdAt": _now_ms(), "syncedRev": 0, "dirty": True,
                    }
                    self._materialize(self.notes[note_id], body)
                    self._notify("create", note_id)
                changed = True

            # whatever is still orphaned was deleted on disk
            for note_id, e in orphans.items():
                self._unmaterialize(e)
                if self.keep_deletes:
                    e["deletedLocally"] = True
                    e["dirty"] = True
                else:
                    self.notes.pop(note_id, None)
                self._notify("delete", note_id)
                changed = True

            if changed:
                self._save_index()
            return changed
