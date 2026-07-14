"""
remotecmd — runs voice commands the phone sends, so "open Claude Code" or
"open chrome" tapped on the phone fires on the laptop within about a second.

Design (a close cousin of cloudsync.py — read that first):
- Pure REST via `requests`, no Firebase SDKs, same DB and auth as cloudsync.
- The phone writes a command node under the user's own subtree:
      /users/{uid}/commands/{cmdId}
  shaped like:
      { "text": "open claude code",
        "createdAt": <server ms>,     # phone writes {".sv":"timestamp"}
        "status": "pending",
        "origin": "phone" }
  createdAt is resolved server-side before it reaches us, so it's a
  trustworthy server timestamp.
- One SSE streaming connection pulls the whole /commands node (first `put`
  is the full snapshot, later events arrive per node in a couple of
  seconds). A worker thread runs each pending command through the SAME
  pipeline a spoken command uses — exact hot words (voicecmd), then the
  Gemini brain — and PATCHes the outcome straight back onto the node:
      done:   {"status":"done",   "result":"<toast>", "doneAt":{".sv":...}}
      failed: {"status":"failed", "result":"<error>", "doneAt":{".sv":...}}
      stale:  {"status":"stale",  "result":"PC saw this too late", ...}
- A command must NEVER run twice: its id goes into an in-memory _handled
  set the instant we pick it up, BEFORE executing, because an SSE reconnect
  re-delivers the whole snapshot (still marked pending) and we'd otherwise
  run it again.
- Stale guard: anything older than two minutes is marked stale and skipped
  (the laptop was asleep / offline when it was sent). This compares the
  local PC clock against the server createdAt; the PC clock is NTP-synced
  so the ~1s skew is negligible against the 2-minute window.
- Purge: once per process, after the first snapshot, command nodes older
  than 24h are deleted — the queue never accumulates junk.

Everything runs on two daemon threads and must never disturb dictation:
every network call is wrapped, and failure just means "try again next tick".
Gating: nothing starts (or keeps running) unless settings["remote_commands"]
is truthy and sync credentials exist; the flag is re-checked at every
reconnect, every worker wakeup, and again immediately before a command runs.
"""

import json
import queue
import threading
import time

import requests           # module-level so tests can swap in a fake:
                          # `remotecmd.requests = FakeRequests()`
import voicecmd            # for execute_actions() — the brain's action runner

from cloudsync import DB_URL, REFRESH_URL   # reuse the proven URLs/API key

STALE_MS = 120_000                    # older than this -> too late to run
PURGE_AGE_MS = 24 * 3600 * 1000       # command nodes older than a day are junk
WAKE_TICK_S = 5.0                     # worker wakeup so flag/stop are noticed


def _now_ms():
    return int(time.time() * 1000)


def _as_ms(value):
    """A createdAt/doneAt as an int, or None if it isn't a real number
    (e.g. still an unresolved {".sv":"timestamp"} — never happens over SSE
    but be defensive)."""
    return int(value) if isinstance(value, (int, float)) else None


class RemoteCommands:
    def __init__(self, settings, save_settings, events, voicecmds, brain,
                 dbg=lambda m: None):
        # settings is the app's SHARED dict — mutate + save_settings, never
        # re-read from disk (cloudsync works exactly this way).
        self.settings = settings
        self.save_settings = save_settings
        self.events = events            # app event queue: ("toast", str)
        self.voicecmds = voicecmds      # voicecmd.VoiceCommands instance
        self.brain = brain              # brain.Brain instance
        self.dbg = dbg
        self._q = queue.Queue()         # SSE events -> worker
        self._stop = threading.Event()
        self._sse_thread = None
        self._worker_thread = None
        self._id_token = None
        self._token_exp = 0.0
        self._nodes = {}                # cmdId -> node, current queue state
        self._handled = set()           # cmdIds already run/stale/skipped
        self._purged = False

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Idempotent. Spawns the daemon threads unless the feature is off
        or there are no sync credentials. Safe to call again after stop()."""
        if not self.settings.get("remote_commands"):
            return
        if not (self.settings.get("sync_uid")
                and self.settings.get("sync_refresh_token")):
            self.dbg("remotecmd: no sync credentials — not starting")
            return
        if self._worker_thread is not None and self._worker_thread.is_alive():
            return                                  # already running
        # A FRESH stop event every run: threads capture it at spawn, so the
        # threads a previous stop() told to quit can never be un-stopped by
        # this start() clearing a shared flag out from under them.
        self._stop = threading.Event()
        stop = self._stop
        self._worker_thread = threading.Thread(
            target=self._worker, args=(stop,),
            name="remotecmd-worker", daemon=True)
        self._sse_thread = threading.Thread(
            target=self._sse_reader, args=(stop,),
            name="remotecmd-sse", daemon=True)
        self._worker_thread.start()
        self._sse_thread.start()
        self.dbg("remotecmd: started")

    def stop(self):
        """Signal the threads to exit and reset the handles so a later
        start() spawns fresh ones (cloudsync.disable() forgets to do this,
        which permanently no-ops any restart — we must not repeat that)."""
        self._stop.set()
        self._worker_thread = None
        self._sse_thread = None
        self.dbg("remotecmd: stopped")

    @property
    def running(self):
        t = self._worker_thread
        return bool(t is not None and t.is_alive() and not self._stop.is_set())

    # ------------------------------------------------------------------
    # auth  (a straight copy of CloudSync._token — same refresh token)
    # ------------------------------------------------------------------

    def _token(self):
        """A valid ID token, refreshing if needed. None => sign-in revoked;
        raises ConnectionError when the network is down."""
        if self._id_token and time.time() < self._token_exp:
            return self._id_token
        rt = self.settings.get("sync_refresh_token")
        if not rt:
            return None
        try:
            r = requests.post(REFRESH_URL, data={
                "grant_type": "refresh_token", "refresh_token": rt}, timeout=15)
        except Exception:
            raise ConnectionError("offline")
        if r.status_code != 200:
            self.dbg(f"remotecmd: token refresh failed {r.status_code}")
            return None                                    # revoked
        data = r.json()
        self._id_token = data["id_token"]
        self._token_exp = time.time() + int(data.get("expires_in", 3600)) - 300
        if data.get("refresh_token") and data["refresh_token"] != rt:
            self.settings["sync_refresh_token"] = data["refresh_token"]
            self.save_settings(self.settings)
        return self._id_token

    def _url(self, cmd_id=None, token=None):
        uid = self.settings.get("sync_uid")
        path = f"/users/{uid}/commands"
        if cmd_id:
            path += f"/{cmd_id}"
        return f"{DB_URL}{path}.json?auth={token}"

    # ------------------------------------------------------------------
    # SSE pull  (a straight copy of CloudSync._sse_reader)
    # ------------------------------------------------------------------

    def _sse_reader(self, stop):
        backoff = 1
        while not stop.is_set():
            if not self.settings.get("remote_commands"):
                return                                     # feature turned off
            try:
                token = self._token()
                if token is None:
                    self.dbg("remotecmd: no token — sign-in needed")
                    return
                with requests.get(self._url(token=token), stream=True,
                                  headers={"Accept": "text/event-stream"},
                                  timeout=(10, 60)) as r:
                    if r.status_code == 401:
                        self._id_token = None
                        continue
                    r.raise_for_status()
                    backoff = 1
                    event = None
                    # Byte-at-a-time so a small live event is never stuck
                    # waiting for a read buffer to fill — but assembled into a
                    # bytearray ourselves: iter_lines(chunk_size=1) re-scans
                    # its whole pending buffer on every byte (O(n^2)). See the
                    # long note in cloudsync._sse_reader.
                    buf = bytearray()
                    for ch in r.iter_content(chunk_size=1):
                        if stop.is_set():
                            return
                        if not ch:
                            continue
                        if ch != b"\n":
                            buf += ch
                            continue
                        raw = buf.decode("utf-8", "replace").rstrip("\r")
                        buf.clear()
                        if raw == "":
                            continue
                        if raw.startswith("event:"):
                            event = raw[6:].strip()
                        elif raw.startswith("data:"):
                            data = raw[5:].strip()
                            if event in ("put", "patch"):
                                try:
                                    self._q.put((event, json.loads(data)))
                                except ValueError:
                                    pass
                            elif event == "auth_revoked":
                                self._id_token = None
                                break
                            elif event == "cancel":
                                break
            except ConnectionError:
                pass                                       # offline — back off
            except Exception as ex:
                self.dbg(f"remotecmd sse: {ex!r}")
            if stop.wait(backoff):
                return
            backoff = min(backoff * 2, 60)

    # ------------------------------------------------------------------
    # worker: applies remote events, runs pending commands
    # ------------------------------------------------------------------

    def _worker(self, stop):
        while not stop.is_set():
            if not self.settings.get("remote_commands"):
                return                                     # feature turned off
            try:
                item = self._q.get(timeout=WAKE_TICK_S)
            except queue.Empty:
                continue
            if item is None:
                continue
            try:
                event, msg = item
                self._apply_event(event, msg)
            except Exception as ex:
                self.dbg(f"remotecmd worker: {ex!r}")

    # ------------------------------------------------------------------
    # applying SSE events into _nodes, then sweeping for work
    # ------------------------------------------------------------------

    def _apply_event(self, event, msg):
        """Fold one SSE put/patch into the in-memory queue, then sweep.
        Called on the worker thread (and directly by the tests)."""
        msg = msg or {}
        path = msg.get("path", "/")
        data = msg.get("data")
        seg = path.strip("/")
        if seg == "":                                      # whole /commands node
            if event == "patch" and isinstance(data, dict):
                for cid, part in data.items():
                    self._merge_node(cid, part)
            else:
                self._nodes = dict(data) if isinstance(data, dict) else {}
                if event == "put" and not self._purged:
                    self._purged = True
                    self._purge_old()
        else:
            parts = seg.split("/")
            cid = parts[0]
            if len(parts) == 1:                            # a whole single node
                if event == "patch":
                    self._merge_node(cid, data)
                elif data is None:
                    self._nodes.pop(cid, None)
                else:
                    self._nodes[cid] = data
            else:                                          # a single field of one
                self._set_field(cid, parts[1], data)
        self._sweep()

    def _merge_node(self, cid, part):
        if part is None:
            self._nodes.pop(cid, None)
            return
        if not isinstance(part, dict):
            return
        cur = self._nodes.get(cid)
        if isinstance(cur, dict):
            cur.update(part)
        else:
            self._nodes[cid] = dict(part)

    def _set_field(self, cid, field, value):
        node = self._nodes.get(cid)
        if not isinstance(node, dict):
            node = {}
            self._nodes[cid] = node
        node[field] = value

    def _sweep(self):
        """Run every pending, not-yet-handled command, oldest first."""
        pending = [(cid, node) for cid, node in self._nodes.items()
                   if isinstance(node, dict)
                   and cid not in self._handled
                   and node.get("status") == "pending"]
        pending.sort(key=lambda t: _as_ms(t[1].get("createdAt")) or 0)
        for cid, node in pending:
            self._process(cid, node)

    def _process(self, cid, node):
        # Claim it BEFORE running: a reconnect re-delivers the pending
        # snapshot and this command must never run twice.
        self._handled.add(cid)
        created = _as_ms(node.get("createdAt"))
        # Local PC clock vs the server createdAt — the PC is NTP-synced so the
        # ~1s skew is nothing against the 2-minute window.
        if created is not None and _now_ms() - created > STALE_MS:
            self.dbg(f"remotecmd: {cid[:8]} too old ({(_now_ms()-created)//1000}s) "
                     f"— stale")
            self._patch(cid, "stale", "PC saw this too late")
            return
        if not self.settings.get("remote_commands"):
            # flag flipped off between the sweep and here — don't run it
            self.dbg(f"remotecmd: {cid[:8]} skipped — remote commands off")
            return
        text = str(node.get("text") or "")
        self.dbg(f"remotecmd: running {cid[:8]} {text!r}")
        status, result = self._execute(text)
        self._patch(cid, status, result)
        self._toast(result or status)

    # ------------------------------------------------------------------
    # the pipeline — mirrors DictationApp._handle_command, minus the
    # command_mode state it must not touch
    # ------------------------------------------------------------------

    def _execute(self, text):
        """(status, result_text) for one command. status is 'done' or
        'failed'."""
        text = (text or "").strip()
        if not text:
            return "failed", "Empty command"
        try:
            msg = self.voicecmds.try_run(text)         # exact hot words
        except Exception as ex:
            self.dbg(f"remotecmd try_run: {ex!r}")
            msg = None
        if msg is not None:
            return "done", msg
        res = self.brain.interpret(text)               # Gemini NL
        if not isinstance(res, dict):
            return "failed", "The brain didn't answer"
        if res.get("error"):
            return "failed", res["error"]
        try:
            fired, toast = voicecmd.execute_actions(
                res.get("actions"), res.get("say"), dbg=self.dbg)
        except Exception as ex:
            self.dbg(f"remotecmd execute_actions: {ex!r}")
            fired, toast = False, "That went wrong — try again"
        if fired:
            return "done", toast or res.get("say") or "Done"
        return "failed", (toast or res.get("say")
                          or "Didn't catch a command — try again")

    # ------------------------------------------------------------------
    # writing the outcome back
    # ------------------------------------------------------------------

    def _patch(self, cid, status, result):
        payload = {"status": status,
                   "result": (result or "")[:500],
                   "doneAt": {".sv": "timestamp"}}
        try:
            token = self._token()
        except ConnectionError:
            self.dbg(f"remotecmd: offline — couldn't report {cid[:8]}")
            return
        if token is None:
            return
        try:
            r = requests.patch(self._url(cid, token), json=payload, timeout=20)
        except Exception as ex:
            self.dbg(f"remotecmd: patch {cid[:8]} failed: {ex!r}")
            return
        if r.status_code == 401:
            self._id_token = None
            return
        if r.status_code != 200:
            self.dbg(f"remotecmd: patch {cid[:8]} HTTP {r.status_code} "
                     f"{r.text[:120]}")

    def _toast(self, msg):
        """Show the phone-triggered result on the pill. Keep a single ⚡ —
        try_run/execute_actions already prefix their own emoji."""
        if not msg:
            return
        text = msg if msg.lstrip().startswith(("⚡", "🤔")) else "⚡ " + msg
        try:
            self.events.put(("toast", text))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # purge  (mirrors CloudSync._purge_tombstones)
    # ------------------------------------------------------------------

    def _purge_old(self):
        cutoff = _now_ms() - PURGE_AGE_MS
        try:
            token = self._token()
        except ConnectionError:
            return
        if token is None:
            return
        for cid, node in list(self._nodes.items()):
            if not isinstance(node, dict):
                continue
            ts = _as_ms(node.get("doneAt"))
            if ts is None:
                ts = _as_ms(node.get("createdAt"))
            if ts is not None and ts < cutoff:
                try:
                    requests.delete(self._url(cid, token), timeout=15)
                    self.dbg(f"remotecmd: purged old command {cid[:8]}")
                except Exception as ex:
                    self.dbg(f"remotecmd purge {cid[:8]}: {ex!r}")
