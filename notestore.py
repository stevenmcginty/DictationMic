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
        """Newest first: [{id, title, body, createdAt, updatedAt}]"""
        with self.lock:
            out = []
            for i, e in self.notes.items():
                if e.get("deletedLocally"):
                    continue
                try:
                    body = self._read_file(e["filename"])
                except OSError:
                    continue
                out.append({"id": i, "title": e["title"], "body": body,
                            "createdAt": e.get("createdAt") or 0,
                            "updatedAt": int(e.get("mtime", 0) * 1000)})
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
            return {"id": note_id, "title": e["title"], "body": body,
                    "createdAt": e.get("createdAt") or 0,
                    "updatedAt": int(e.get("mtime", 0) * 1000)}

    def entry(self, note_id):
        with self.lock:
            e = self.notes.get(note_id)
            return dict(e) if e else None

    def dirty_ids(self):
        with self.lock:
            return [i for i, e in self.notes.items() if e.get("dirty")]

    # ---------------- local mutations (UI / dictation) ----------------

    def create(self, title, body, note_id=None):
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

    def delete(self, note_id):
        with self.lock:
            e = self.notes.get(note_id)
            if not e:
                return False
            try:
                os.remove(self._path(e["filename"]))
            except OSError:
                pass
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
                    }
                    kind = "remote_create"
                else:
                    changed = False
                    if _hash(body) != e["hash"]:
                        self._write_file(e["filename"], body)
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
                    self._notify("create", note_id)
                changed = True

            # whatever is still orphaned was deleted on disk
            for note_id, e in orphans.items():
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
