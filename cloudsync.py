"""
cloudsync — keeps notes\\ in step with the Firebase Realtime Database so the
phone PWA sees every dictation and the laptop gets every phone note.

Design (see also web/js/sync.js, the same logic in the browser):
- Pure REST via `requests` — no Firebase SDKs, PyInstaller stays happy.
- Auth: Firebase email/password. setup() swaps the password for a refresh
  token (stored in settings.json); ID tokens are minted from it as needed.
  The password itself is never kept.
- Push: every entry the NoteStore marks dirty is PATCHed with a server
  timestamp; the returned updatedAt becomes the entry's syncedRev. The files
  plus dirty flags ARE the retry queue — nothing else to persist.
- Pull: one SSE streaming connection. The first `put` is the whole snapshot
  (initial sync and reconcile are the same code path); later events arrive
  per note within a couple of seconds.
- Echo guard: anything with updatedAt <= syncedRev is our own write coming
  back. Conflicts (both sides edited while apart) resolve last-writer-wins.
- Deletes are tombstones ({deleted: true}); tombstones older than 30 days
  are physically removed during the snapshot pass.

Everything runs on two daemon threads and must never disturb dictation:
every network call is wrapped, and failure just means "try again next tick".
"""

import json
import queue
import threading
import time

API_KEY = "AIzaSyDmofv0p1-90ccEdsruYHQoTqDs5WpYQHU"
DB_URL = "https://dictationmic-sync-default-rtdb.europe-west1.firebasedatabase.app"

SIGNIN_URL = ("https://identitytoolkit.googleapis.com/v1/"
              "accounts:signInWithPassword?key=" + API_KEY)
SIGNUP_URL = ("https://identitytoolkit.googleapis.com/v1/"
              "accounts:signUp?key=" + API_KEY)
REFRESH_URL = "https://securetoken.googleapis.com/v1/token?key=" + API_KEY

TOMBSTONE_KEEP_MS = 30 * 24 * 3600 * 1000
SCAN_TICK_S = 5.0


def _now_ms():
    return int(time.time() * 1000)


class CloudSync:
    def __init__(self, store, settings, save_settings, events, dbg=lambda m: None,
                 voice_stt=None):
        self.store = store
        self.settings = settings
        self.save_settings = save_settings
        self.events = events            # app event queue: ("sync_status", dict)
        self.dbg = dbg
        # voice_stt(webm_bytes) -> text: transcribe a phone voice note with
        # the app's Whisper. Returns None when the model is busy/not loaded
        # yet (we retry), "" when the audio held no speech.
        self.voice_stt = voice_stt
        self._q = queue.Queue()         # remote events -> worker
        self._stt_q = queue.Queue()     # (nid, record) voice notes to text
        self._stt_pending = set()
        self._stt_thread = None
        self._stop = threading.Event()
        self._id_token = None
        self._token_exp = 0.0
        self._sse_thread = None
        self._worker_thread = None
        self._state = "off"             # off | ok | offline | needs-signin | error
        self._last_sync = 0
        self._purged = False

    # ------------------------------------------------------------------
    # auth
    # ------------------------------------------------------------------

    def setup(self, email, password):
        """One-time sign-in (creates the account on first ever use).
        Returns (ok, message). Never stores the password."""
        import requests
        try:
            r = requests.post(SIGNIN_URL, json={
                "email": email, "password": password,
                "returnSecureToken": True}, timeout=15)
            if r.status_code != 200:
                err = (r.json().get("error") or {}).get("message", "")
                if err.startswith(("EMAIL_NOT_FOUND", "INVALID_LOGIN_CREDENTIALS")):
                    r2 = requests.post(SIGNUP_URL, json={
                        "email": email, "password": password,
                        "returnSecureToken": True}, timeout=15)
                    if r2.status_code != 200:
                        err2 = (r2.json().get("error") or {}).get("message", err)
                        if err2.startswith("EMAIL_EXISTS"):
                            return False, "Wrong password for that account"
                        return False, self._friendly(err2)
                    r = r2
                else:
                    return False, self._friendly(err)
            data = r.json()
        except Exception as ex:
            self.dbg(f"cloudsync setup: {ex}")
            return False, "Couldn't reach Firebase — check the internet"
        self.settings["sync_email"] = email
        self.settings["sync_refresh_token"] = data["refreshToken"]
        self.settings["sync_uid"] = data["localId"]
        self.settings["sync_enabled"] = True
        self.save_settings(self.settings)
        self._id_token = data["idToken"]
        self._token_exp = time.time() + int(data.get("expiresIn", 3600)) - 300
        return True, "Phone sync is on"

    @staticmethod
    def _friendly(err):
        if "WEAK_PASSWORD" in err:
            return "Password needs at least 6 characters"
        if "INVALID_EMAIL" in err:
            return "That email doesn't look right"
        if "TOO_MANY_ATTEMPTS" in err:
            return "Too many tries — wait a minute"
        return "Sign-in failed" + (f" ({err})" if err else "")

    def _token(self):
        """A valid ID token, refreshing if needed. None => needs sign-in."""
        if self._id_token and time.time() < self._token_exp:
            return self._id_token
        rt = self.settings.get("sync_refresh_token")
        if not rt:
            return None
        import requests
        try:
            r = requests.post(REFRESH_URL, data={
                "grant_type": "refresh_token", "refresh_token": rt}, timeout=15)
        except Exception:
            raise ConnectionError("offline")
        if r.status_code != 200:
            self.dbg(f"cloudsync: token refresh failed {r.status_code}")
            return None                                    # revoked
        data = r.json()
        self._id_token = data["id_token"]
        self._token_exp = time.time() + int(data.get("expires_in", 3600)) - 300
        if data.get("refresh_token") and data["refresh_token"] != rt:
            self.settings["sync_refresh_token"] = data["refresh_token"]
            self.save_settings(self.settings)
        return self._id_token

    def _notes_url(self, note_id=None, token=None):
        uid = self.settings.get("sync_uid")
        path = f"/users/{uid}/notes"
        if note_id:
            path += f"/{note_id}"
        return f"{DB_URL}{path}.json?auth={token}"

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self):
        if self._worker_thread is not None:
            return
        self.store.keep_deletes = True
        self._stop.clear()
        self._worker_thread = threading.Thread(
            target=self._worker, name="cloudsync-worker", daemon=True)
        self._sse_thread = threading.Thread(
            target=self._sse_reader, name="cloudsync-sse", daemon=True)
        self._worker_thread.start()
        self._sse_thread.start()
        if self.voice_stt is not None:
            self._stt_thread = threading.Thread(
                target=self._stt_worker, name="cloudsync-stt", daemon=True)
            self._stt_thread.start()
        # a local change should push within a beat, not at the next tick
        self.store.subscribe(lambda kind, nid:
                             None if kind.startswith("remote_")
                             else self._q.put(("local_change", None)))

    def disable(self):
        """Turn sync off and forget the tokens."""
        self._stop.set()
        self.store.keep_deletes = False
        for k in ("sync_refresh_token", "sync_uid"):
            self.settings[k] = ""
        self.settings["sync_enabled"] = False
        self.save_settings(self.settings)
        self._set_state("off")

    def status(self):
        return {"sync": self._state, "lastSync": self._last_sync}

    def _set_state(self, state):
        if state != self._state:
            self._state = state
            try:
                self.events.put(("sync_status", self.status()))
            except Exception:
                pass

    # ------------------------------------------------------------------
    # SSE pull
    # ------------------------------------------------------------------

    def _sse_reader(self):
        import requests
        backoff = 1
        while not self._stop.is_set():
            try:
                token = self._token()
                if token is None:
                    self._set_state("needs-signin")
                    return
                with requests.get(self._notes_url(token=token), stream=True,
                                  headers={"Accept": "text/event-stream"},
                                  timeout=(10, 60)) as r:
                    if r.status_code == 401:
                        self._id_token = None
                        continue
                    r.raise_for_status()
                    backoff = 1
                    event = None
                    # chunk_size=1: iter_lines otherwise buffers 512 bytes,
                    # which silently swallows small SSE events forever
                    for raw in r.iter_lines(chunk_size=1, decode_unicode=True):
                        if self._stop.is_set():
                            return
                        if raw is None or raw == "":
                            continue
                        if raw.startswith("event:"):
                            event = raw[6:].strip()
                        elif raw.startswith("data:"):
                            data = raw[5:].strip()
                            if event in ("put", "patch"):
                                try:
                                    self._q.put(("remote", (event, json.loads(data))))
                                except ValueError:
                                    pass
                            elif event == "auth_revoked":
                                self._id_token = None
                                break
                            elif event == "cancel":
                                break
            except Exception as ex:
                self.dbg(f"cloudsync sse: {ex}")
                self._set_state("offline")
            if self._stop.wait(backoff):
                return
            backoff = min(backoff * 2, 60)

    # ------------------------------------------------------------------
    # worker: scans, pushes, applies remote events
    # ------------------------------------------------------------------

    def _worker(self):
        last_scan = 0.0
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=SCAN_TICK_S)
            except queue.Empty:
                item = None
            try:
                if item and item[0] == "remote":
                    self._handle_remote(*item[1])
                if time.monotonic() - last_scan >= 2.0 or item is not None:
                    last_scan = time.monotonic()
                    self.store.scan()
                self._push_dirty()
            except ConnectionError:
                self._set_state("offline")
            except Exception as ex:
                self.dbg(f"cloudsync worker: {ex}")
                self._set_state("error")

    def _push_dirty(self):
        import requests
        dirty = self.store.dirty_ids()
        if not dirty:
            return
        token = self._token()
        if token is None:
            self._set_state("needs-signin")
            return
        for nid in dirty:
            e = self.store.entry(nid)
            if e is None:
                continue
            if e.get("deletedLocally"):
                payload = {"deleted": True, "body": None,
                           "origin": "laptop",
                           "updatedAt": {".sv": "timestamp"}}
            else:
                note = self.store.get(nid)
                if note is None:
                    continue
                payload = {"title": e["title"], "body": note["body"],
                           "createdAt": e.get("createdAt") or _now_ms(),
                           "deleted": False, "origin": "laptop",
                           "updatedAt": {".sv": "timestamp"}}
            try:
                r = requests.patch(self._notes_url(nid, token),
                                   json=payload, timeout=20)
            except Exception:
                self._set_state("offline")
                return                                    # retry next tick
            if r.status_code == 401:
                self._id_token = None
                return
            if r.status_code != 200:
                self.dbg(f"cloudsync push {nid}: {r.status_code} {r.text[:120]}")
                continue
            rev = int(r.json().get("updatedAt") or _now_ms())
            if e.get("deletedLocally"):
                self.store.drop_entry(nid, tombstone_rev=rev)
            else:
                self.store.mark_synced(nid, rev)
            self._last_sync = _now_ms()
        self._set_state("ok")

    # ------------------------------------------------------------------
    # applying remote events
    # ------------------------------------------------------------------

    def _handle_remote(self, kind, msg):
        path = (msg or {}).get("path", "/")
        data = (msg or {}).get("data")
        if path == "/":
            self._reconcile(data or {})
        else:
            nid = path.strip("/").split("/")[0]
            if "/" in path.strip("/"):
                # field-level patch — fetch nothing, fold into a full apply
                # next snapshot; cheap approximation: re-pull this note
                self._q.put(("remote", ("put", {"path": f"/{nid}",
                                                "data": self._fetch_note(nid)})))
                return
            self._apply_one(nid, data)
        self._last_sync = _now_ms()
        self._set_state("ok")

    def _fetch_note(self, nid):
        import requests
        try:
            token = self._token()
            r = requests.get(self._notes_url(nid, token), timeout=15)
            return r.json() if r.status_code == 200 else None
        except Exception:
            return None

    def _apply_one(self, nid, record):
        if record is None:                       # node physically removed
            e = self.store.entry(nid)
            if e is not None and not e.get("dirty"):
                self.store.apply_remote(nid, {"deleted": True,
                                              "updatedAt": _now_ms()})
            return
        # a phone voice note still waiting for text: queue it for Whisper
        # regardless of the echo guards below — after a restart the record
        # is old news to the file store but the audio still needs doing
        if (record.get("audio") and not record.get("transcribed")
                and not record.get("deleted")):
            self._queue_stt(nid, record)
        rev = int(record.get("updatedAt") or 0)
        e = self.store.entry(nid)
        if e is None:
            tomb = self.store.tombstones.get(nid)
            if tomb is not None and rev <= tomb:
                return                            # our delete echoing back
            if record.get("deleted"):
                self.store.tombstones[nid] = rev
                return
            self.store.apply_remote(nid, record)
            return
        if rev <= int(e.get("syncedRev") or 0):
            return                                # echo of our own push
        if e.get("dirty") and not record.get("deleted"):
            local_ms = int((e.get("mtime") or 0) * 1000)
            if local_ms >= rev:
                return                            # LWW: our edit is newer, push wins
        self.store.apply_remote(nid, record)

    def _reconcile(self, snapshot):
        seen = set()
        for nid, record in (snapshot or {}).items():
            if not isinstance(record, dict):
                continue
            seen.add(nid)
            self._apply_one(nid, record)
        # local notes that were synced before but are gone from the cloud
        # were deleted (and purged) while we were away
        for nid in list(self.store.notes.keys()):
            e = self.store.entry(nid)
            if (e and nid not in seen and int(e.get("syncedRev") or 0) > 0
                    and not e.get("dirty")):
                self.store.apply_remote(nid, {"deleted": True,
                                              "updatedAt": _now_ms()})
        if not self._purged:
            self._purged = True
            self._purge_tombstones(snapshot or {})

    # ------------------------------------------------------------------
    # phone voice notes -> Whisper -> text back to the cloud
    # ------------------------------------------------------------------

    def _queue_stt(self, nid, record):
        if self.voice_stt is None or nid in self._stt_pending:
            return
        self._stt_pending.add(nid)
        self._stt_q.put((nid, record))
        self.dbg(f"cloudsync stt: queued voice note {nid[:8]}")

    def _stt_worker(self):
        import base64
        import requests
        from notestore import note_title_from
        while not self._stop.is_set():
            try:
                nid, record = self._stt_q.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                raw = base64.b64decode(record.get("audio") or "")
            except Exception:
                raw = b""
            text = None
            if raw:
                try:
                    text = self.voice_stt(raw)
                except Exception as ex:
                    self.dbg(f"cloudsync stt {nid[:8]}: {ex}")
                    text = ""
            else:
                text = ""
            if text is None:
                # model still loading or desktop dictation in progress —
                # come back to this note in a few seconds
                if self._stop.wait(5.0):
                    return
                self._stt_q.put((nid, record))
                continue
            payload = {"body": text or "(nothing heard)",
                       "title": note_title_from(text) if text else "Voice note",
                       "transcribed": True, "audio": None,
                       "origin": "laptop",
                       "updatedAt": {".sv": "timestamp"}}
            try:
                token = self._token()
                r = requests.patch(self._notes_url(nid, token),
                                   json=payload, timeout=30)
                ok = r.status_code == 200
            except Exception as ex:
                self.dbg(f"cloudsync stt push {nid[:8]}: {ex}")
                ok = False
            if not ok:
                if self._stop.wait(10.0):
                    return
                self._stt_q.put((nid, record))
                continue
            self._stt_pending.discard(nid)
            self.dbg(f"cloudsync stt: transcribed {nid[:8]} "
                     f"({len(text or '')} chars)")

    def _purge_tombstones(self, snapshot):
        import requests
        cutoff = _now_ms() - TOMBSTONE_KEEP_MS
        try:
            token = self._token()
            for nid, record in snapshot.items():
                if (isinstance(record, dict) and record.get("deleted")
                        and int(record.get("updatedAt") or 0) < cutoff):
                    requests.delete(self._notes_url(nid, token), timeout=15)
        except Exception as ex:
            self.dbg(f"cloudsync purge: {ex}")
