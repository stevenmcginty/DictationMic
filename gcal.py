"""
gcal — Google Calendar for the pill, REST only (no SDK, PyInstaller-safe).

Auth is a standard OAuth "Desktop app" loopback flow: connect() opens the
browser on Google's consent page, catches the redirect on a one-shot
127.0.0.1 server, and swaps the code for a refresh token. The client id /
secret come from Steve's own Google Cloud project (see README) and live in
settings.json alongside the refresh token — same trust level as the
existing sync_refresh_token.

Access tokens are minted from the refresh token on demand (mirror of
cloudsync._token). Only calendar.events scope is requested, plus openid
email so the menu can show which account is connected.
"""

import base64
import json
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlencode, urlparse, parse_qs

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
EVENTS_URL = ("https://www.googleapis.com/calendar/v3/"
              "calendars/primary/events")
SCOPE = "openid email https://www.googleapis.com/auth/calendar.events"

_LANDING = b"""<!doctype html><meta charset="utf-8">
<title>DictationMic</title>
<body style="background:#131512;color:#eceee7;font:15px 'Segoe UI',sans-serif;
display:grid;place-items:center;height:100vh;margin:0">
<div style="text-align:center"><div style="font-size:34px">%s</div>
<h2 style="margin:12px 0 4px">%s</h2>
<p style="color:#8a919c">%s</p></div>"""


def _landing(ok):
    if ok:
        return _LANDING % (b"&#128197;", b"Google Calendar is connected",
                           b"You can close this tab and go back to the pill.")
    return _LANDING % (b"&#9888;&#65039;", b"That didn't work",
                       b"Close this tab and try again from the pill.")


def _id_token_email(id_token):
    """The email baked into an OAuth id_token (JWT) — no extra API call."""
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("email", "")
    except Exception:
        return ""


class GCal:
    """Owns the gcal_* settings keys. Thread-safe enough for one worker."""

    def __init__(self, settings, save_settings, dbg=lambda m: None):
        self.settings = settings
        self.save_settings = save_settings
        self.dbg = dbg
        self._access = None
        self._access_exp = 0.0

    def connected(self):
        return bool(self.settings.get("gcal_refresh_token"))

    def email(self):
        return self.settings.get("gcal_email") or ""

    # ------------------------------------------------------------------
    # connect (loopback OAuth) — call from a worker thread, blocks until
    # the browser round-trip finishes or times out
    # ------------------------------------------------------------------

    def connect(self, client_id, client_secret, timeout=240):
        """(ok, message). Stores tokens in settings on success."""
        client_id = (client_id or "").strip()
        client_secret = (client_secret or "").strip()
        if not client_id or not client_secret:
            return False, "Fill in both boxes"

        state = secrets.token_urlsafe(16)
        result = {}
        done = threading.Event()

        class Catcher(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                q = parse_qs(urlparse(self.path).query)
                ok = (q.get("state", [""])[0] == state and "code" in q)
                if ok:
                    result["code"] = q["code"][0]
                body = _landing(ok)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass
                done.set()

        try:
            httpd = HTTPServer(("127.0.0.1", 0), Catcher)
        except OSError as ex:
            return False, f"Couldn't open a local port ({ex})"
        port = httpd.server_address[1]
        redirect = f"http://127.0.0.1:{port}"
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        try:
            webbrowser.open(AUTH_URL + "?" + urlencode({
                "client_id": client_id, "redirect_uri": redirect,
                "response_type": "code", "scope": SCOPE,
                "access_type": "offline", "prompt": "consent",
                "state": state,
            }))
            if not done.wait(timeout) or "code" not in result:
                return False, ("Didn't hear back from the browser — "
                               "try Connect again")
        finally:
            threading.Thread(target=httpd.shutdown, daemon=True).start()

        import requests
        try:
            r = requests.post(TOKEN_URL, data={
                "code": result["code"], "client_id": client_id,
                "client_secret": client_secret, "redirect_uri": redirect,
                "grant_type": "authorization_code"}, timeout=20)
        except Exception:
            return False, "Couldn't reach Google — check the internet"
        if r.status_code != 200:
            err = ""
            try:
                err = r.json().get("error_description") or r.json().get("error", "")
            except ValueError:
                pass
            self.dbg(f"gcal connect: {r.status_code} {r.text[:200]}")
            if "invalid_client" in (err or "").lower():
                return False, ("Google didn't accept the Client ID/secret — "
                               "check them against the Cloud console")
            return False, "Google sign-in failed" + (f" ({err})" if err else "")
        data = r.json()
        if not data.get("refresh_token"):
            return False, ("Google didn't return a refresh token — remove "
                           "DictationMic under myaccount.google.com → "
                           "Security → Third-party access, then try again")
        self.settings["gcal_client_id"] = client_id
        self.settings["gcal_client_secret"] = client_secret
        self.settings["gcal_refresh_token"] = data["refresh_token"]
        self.settings["gcal_email"] = _id_token_email(data.get("id_token", ""))
        self.save_settings(self.settings)
        self._access = data.get("access_token")
        self._access_exp = time.time() + int(data.get("expires_in", 3600)) - 300
        return True, "Google Calendar is connected"

    def disconnect(self):
        token = self.settings.get("gcal_refresh_token")
        if token:
            try:
                import requests
                requests.post(REVOKE_URL, data={"token": token}, timeout=10)
            except Exception:
                pass                       # local forget is what matters
        for k in ("gcal_refresh_token", "gcal_email"):
            self.settings[k] = ""
        self.save_settings(self.settings)
        self._access = None

    # ------------------------------------------------------------------
    # tokens & events
    # ------------------------------------------------------------------

    def _token(self):
        """A live access token, or None => needs a fresh connect."""
        if self._access and time.time() < self._access_exp:
            return self._access
        rt = self.settings.get("gcal_refresh_token")
        if not rt:
            return None
        import requests
        r = requests.post(TOKEN_URL, data={
            "grant_type": "refresh_token", "refresh_token": rt,
            "client_id": self.settings.get("gcal_client_id", ""),
            "client_secret": self.settings.get("gcal_client_secret", ""),
        }, timeout=15)
        if r.status_code != 200:
            self.dbg(f"gcal token refresh failed: {r.status_code} {r.text[:120]}")
            if r.status_code in (400, 401):
                return None                # revoked/expired => reconnect
            raise ConnectionError("gcal token refresh failed")
        data = r.json()
        self._access = data["access_token"]
        self._access_exp = time.time() + int(data.get("expires_in", 3600)) - 300
        return self._access

    def create_event(self, summary, start, end, all_day, description=""):
        """-> {"eventId", "link"}. Raises RuntimeError with a user-friendly
        message on any failure (the worker turns it into a toast)."""
        token = self._token()
        if token is None:
            raise RuntimeError("Google Calendar needs a fresh sign-in — "
                               "right-click me → Connect Google Calendar")
        if all_day:
            body = {"start": {"date": start.date().isoformat()},
                    "end": {"date": end.date().isoformat()}}
        else:
            body = {"start": {"dateTime": start.isoformat()},
                    "end": {"dateTime": end.isoformat()}}
        body.update({"summary": summary or "Note",
                     "description": description,
                     "reminders": {"useDefault": True}})
        import requests
        r = requests.post(EVENTS_URL,
                          headers={"Authorization": f"Bearer {token}"},
                          json=body, timeout=20)
        if r.status_code == 401:
            self._access = None            # stale token — one retry
            token = self._token()
            if token is None:
                raise RuntimeError("Google Calendar needs a fresh sign-in")
            r = requests.post(EVENTS_URL,
                              headers={"Authorization": f"Bearer {token}"},
                              json=body, timeout=20)
        if r.status_code not in (200, 201):
            self.dbg(f"gcal create_event: {r.status_code} {r.text[:200]}")
            raise RuntimeError("Google Calendar said no — try again in a bit")
        data = r.json()
        return {"eventId": data.get("id", ""),
                "link": data.get("htmlLink", "")}
