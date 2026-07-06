"""
localserver — serves the notes web UI on 127.0.0.1 for the desktop.

A stdlib ThreadingHTTPServer that serves the static web\\ folder plus a tiny
JSON API backed directly by notestore (the notes\\ folder), so "My notes"
works fully offline. Loopback only, Host-header checked, and every /api call
must carry the per-run token that is embedded in the URL we open the browser
with — other local processes can't poke at the notes.
"""

import json
import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def web_dir():
    # onedir build: --add-data "web;web" lands in _internal (sys._MEIPASS);
    # dev run: next to the source files
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "web")


MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".webmanifest": "application/manifest+json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


class LocalServer:
    """Owns the HTTP server thread. start() is idempotent."""

    def __init__(self, store, status_fn=None, dbg=lambda m: None):
        self.store = store
        self.status_fn = status_fn or (lambda: {"sync": "off", "lastSync": 0})
        self.dbg = dbg
        self.token = secrets.token_urlsafe(16)
        self.port = None
        self._httpd = None
        self._last_scan = 0.0

    def _scan_maybe(self):
        """Pick up files added/renamed outside the store (Explorer, etc.),
        at most once every 2 s."""
        now = time.monotonic()
        if now - self._last_scan >= 2.0:
            self._last_scan = now
            try:
                self.store.scan()
            except Exception as ex:
                self.dbg(f"localserver: scan failed: {ex}")

    def start(self):
        if self._httpd is not None:
            return True
        handler = self._make_handler()
        for port in range(8752, 8762):
            try:
                self._httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
                self.port = port
                break
            except OSError:
                continue
        if self._httpd is None:
            return False
        self._httpd.daemon_threads = True
        threading.Thread(target=self._httpd.serve_forever,
                         name="localserver", daemon=True).start()
        self.dbg(f"localserver: listening on 127.0.0.1:{self.port}")
        return True

    def url(self):
        return f"http://127.0.0.1:{self.port}/#t={self.token}"

    # ------------------------------------------------------------------

    def _make_handler(server_self):
        store = server_self.store

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                server_self.dbg("localserver: " + fmt % args)

            # ---- plumbing ----

            def _host_ok(self):
                host = (self.headers.get("Host") or "").split(":")[0].lower()
                return host in ("127.0.0.1", "localhost")

            def _token_ok(self):
                return (self.headers.get("X-DictMic-Token") == server_self.token)

            def _send(self, code, body, ctype="application/json"):
                data = body if isinstance(body, bytes) else \
                    json.dumps(body).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (ConnectionAbortedError, BrokenPipeError):
                    pass

            def _json_body(self):
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    return json.loads(self.rfile.read(n) or b"{}")
                except (ValueError, OSError):
                    return None

            def _route(self):
                """('static', path) | ('api', [parts]) | None"""
                path = self.path.split("?", 1)[0].split("#", 1)[0]
                if not self._host_ok():
                    self._send(403, {"error": "bad host"})
                    return None
                if path.startswith("/api/"):
                    if not self._token_ok():
                        self._send(403, {"error": "bad token"})
                        return None
                    return ("api", [p for p in path[5:].split("/") if p])
                return ("static", path)

            # ---- static files ----

            def _serve_static(self, path):
                if path == "/":
                    path = "/index.html"
                root = os.path.realpath(web_dir())
                full = os.path.realpath(os.path.join(root, path.lstrip("/")))
                if not full.startswith(root + os.sep) and full != root:
                    self._send(404, {"error": "not found"})
                    return
                ext = os.path.splitext(full)[1].lower()
                try:
                    with open(full, "rb") as f:
                        data = f.read()
                except OSError:
                    self._send(404, {"error": "not found"})
                    return
                self._send(200, data, MIME.get(ext, "application/octet-stream"))

            # ---- verbs ----

            def do_GET(self):
                r = self._route()
                if r is None:
                    return
                kind, arg = r
                if kind == "static":
                    return self._serve_static(arg)
                if arg == ["notes"]:
                    server_self._scan_maybe()
                    return self._send(200, store.all_notes())
                if len(arg) == 2 and arg[0] == "notes":
                    note = store.get(arg[1])
                    return self._send(200, note) if note else \
                        self._send(404, {"error": "no such note"})
                if arg == ["status"]:
                    return self._send(200, server_self.status_fn())
                self._send(404, {"error": "no such endpoint"})

            def do_POST(self):
                r = self._route()
                if r is None:
                    return
                kind, arg = r
                if kind == "api" and arg == ["notes"]:
                    body = self._json_body()
                    if body is None or not isinstance(body.get("body"), str):
                        return self._send(400, {"error": "need body"})
                    note = store.create(body.get("title"), body["body"])
                    return self._send(200, note)
                self._send(404, {"error": "no such endpoint"})

            def do_PUT(self):
                r = self._route()
                if r is None:
                    return
                kind, arg = r
                if kind != "api" or not arg or arg[0] != "notes":
                    return self._send(404, {"error": "no such endpoint"})
                body = self._json_body()
                if body is None:
                    return self._send(400, {"error": "bad json"})
                if len(arg) == 2:                      # PUT /api/notes/{id}
                    if not isinstance(body.get("body"), str):
                        return self._send(400, {"error": "need body"})
                    note = store.update(arg[1], body["body"])
                elif len(arg) == 3 and arg[2] == "title":   # .../{id}/title
                    if not (body.get("title") or "").strip():
                        return self._send(400, {"error": "need title"})
                    note = store.rename(arg[1], body["title"])
                elif len(arg) == 3 and arg[2] == "star":     # .../{id}/star
                    if not isinstance(body.get("starred"), bool):
                        return self._send(400, {"error": "need starred"})
                    note = store.set_star(arg[1], body["starred"])
                else:
                    return self._send(404, {"error": "no such endpoint"})
                return self._send(200, note) if note else \
                    self._send(404, {"error": "no such note"})

            def do_DELETE(self):
                r = self._route()
                if r is None:
                    return
                kind, arg = r
                if kind == "api" and len(arg) == 2 and arg[0] == "notes":
                    ok = store.delete(arg[1])
                    return self._send(200, {"ok": ok})
                self._send(404, {"error": "no such endpoint"})

        return Handler
