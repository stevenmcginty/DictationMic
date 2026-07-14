"""Offline unit pass for remotecmd — run:
       venv\\Scripts\\python.exe test_remotecmd.py

No network: `requests` is swapped for a fake that records PATCH/DELETE
calls, and synthetic SSE events are fed straight into the internal
handler (_apply_event) rather than spinning real threads. Stub
voicecmds/brain objects give canned returns so the try_run -> brain ->
execute_actions pipeline can be steered case by case."""

import queue
import sys
import time

import remotecmd


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------

class FakeResp:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._data = data or {}
        self.text = ""

    def json(self):
        return self._data


class FakeRequests:
    """Records writes; refuses any read/stream (tests never need them)."""
    def __init__(self):
        self.patched = []       # [(url, json_body)]
        self.deleted = []       # [url]

    def patch(self, url, json=None, timeout=None):
        self.patched.append((url, json))
        return FakeResp(200, {})

    def delete(self, url, timeout=None):
        self.deleted.append(url)
        return FakeResp(200, {})

    def get(self, *a, **k):
        raise AssertionError("no network get in tests")

    def post(self, *a, **k):
        raise AssertionError("no network post in tests")


class StubVoice:
    """voicecmd.VoiceCommands stand-in. try_run returns `ret` and records
    each call so we can assert it ran (or didn't)."""
    def __init__(self, ret=None):
        self.ret = ret
        self.calls = []

    def try_run(self, text):
        self.calls.append(text)
        return self.ret


class StubBrain:
    def __init__(self, ret=None):
        self.ret = ret if ret is not None else {"actions": [], "say": "ok"}
        self.calls = []

    def interpret(self, text):
        self.calls.append(text)
        return self.ret


def make_rc(settings=None, voice=None, brain=None):
    settings = settings or {"remote_commands": True,
                            "sync_uid": "u1", "sync_refresh_token": "rt"}
    saved = []
    rc = remotecmd.RemoteCommands(
        settings, lambda s: saved.append(dict(s)), queue.Queue(),
        voice or StubVoice(), brain or StubBrain())
    # a valid cached token so _token() never touches the network
    rc._id_token = "tok"
    rc._token_exp = time.time() + 9999
    rc._purged = True                 # skip the one-off network purge sweep
    return rc


def snapshot(cid, text, created, status="pending"):
    return {"path": "/", "data": {cid: {
        "text": text, "createdAt": created,
        "status": status, "origin": "phone"}}}


# --------------------------------------------------------------------------
# cases
# --------------------------------------------------------------------------

def test_executes_once_and_never_twice():
    voice = StubVoice(ret="⚡ Starting Claude Code")
    fake = FakeRequests()
    remotecmd.requests = fake
    rc = make_rc(voice=voice)
    snap = snapshot("c1", "open claude code", remotecmd._now_ms())

    rc._apply_event("put", snap)
    assert voice.calls == ["open claude code"], voice.calls
    assert len(fake.patched) == 1, fake.patched
    url, body = fake.patched[0]
    assert "/users/u1/commands/c1" in url, url
    assert body["status"] == "done", body
    assert body["result"] == "⚡ Starting Claude Code", body
    assert body["doneAt"] == {".sv": "timestamp"}, body

    # an SSE reconnect re-delivers the same (still-pending) snapshot
    rc._apply_event("put", snap)
    assert voice.calls == ["open claude code"], "re-executed on re-delivery!"
    assert len(fake.patched) == 1, "patched twice!"


def test_stale_is_skipped():
    voice = StubVoice(ret="should never run")
    fake = FakeRequests()
    remotecmd.requests = fake
    rc = make_rc(voice=voice)
    ten_min_ago = remotecmd._now_ms() - 10 * 60 * 1000
    rc._apply_event("put", snapshot("c2", "open chrome", ten_min_ago))

    assert voice.calls == [], "executor ran on a stale command!"
    assert len(fake.patched) == 1, fake.patched
    _, body = fake.patched[0]
    assert body["status"] == "stale", body
    assert body["result"] == "PC saw this too late", body


def test_brain_error_fails():
    voice = StubVoice(ret=None)                         # not a hot word
    brain = StubBrain(ret={"error": "Can't reach the brain — no internet?"})
    fake = FakeRequests()
    remotecmd.requests = fake
    rc = make_rc(voice=voice, brain=brain)
    rc._apply_event("put", snapshot("c3", "do something odd",
                                    remotecmd._now_ms()))

    assert voice.calls == ["do something odd"], voice.calls
    assert brain.calls == ["do something odd"], brain.calls
    assert len(fake.patched) == 1, fake.patched
    _, body = fake.patched[0]
    assert body["status"] == "failed", body
    assert body["result"] == "Can't reach the brain — no internet?", body


def test_flag_off_runs_nothing():
    voice = StubVoice(ret="⚡ nope")
    fake = FakeRequests()
    remotecmd.requests = fake
    rc = make_rc(settings={"remote_commands": False,
                           "sync_uid": "u1", "sync_refresh_token": "rt"},
                 voice=voice)
    rc._apply_event("put", snapshot("c4", "open chrome", remotecmd._now_ms()))

    assert voice.calls == [], "ran a command with remote_commands off!"
    assert fake.patched == [], fake.patched     # nothing executed => nothing to report


# --------------------------------------------------------------------------

def main():
    tests = [test_executes_once_and_never_twice,
             test_stale_is_skipped,
             test_brain_error_fails,
             test_flag_off_runs_nothing]
    fails = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as ex:
            fails += 1
            print(f"FAIL {t.__name__}: {ex}")
        except Exception as ex:
            fails += 1
            print(f"ERROR {t.__name__}: {ex!r}")
    print(f"{len(tests) - fails}/{len(tests)} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
