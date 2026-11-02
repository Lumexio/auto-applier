"""The HTTP side of the web interface: one page, a few JSON endpoints, the pictures of a job's folder.

It listens on 127.0.0.1 only and treats every request as untrusted anyway, because any web page open in your browser can
make your browser call a local address:

  * the ``Host`` header must be this server's own address (a page that re-points its own domain at 127.0.0.1 -- DNS
    rebinding -- would arrive with its own domain there and is refused);
  * a request that carries an ``Origin`` must come from this server's own page;
  * everything but the page itself needs the token that is built into the page when it is served (a different site cannot
    read the page, so it cannot learn the token); a button press (POST) must also be JSON, which a form on another site cannot send;
  * the page is served with a policy that forbids loading anything from elsewhere.

Postings and answers are data from outside: the page shows them as text, never as markup.
"""

from __future__ import annotations

import json
import mimetypes
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from gui.service import ActionError, Desk

APP_NAME = "auto-applier-desk"
STATIC = Path(__file__).resolve().parent / "static"
MAX_BODY = 64 * 1024
CSP = ("default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class DeskServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False         # on Windows SO_REUSEADDR lets a second program share the port

    def __init__(self, desk: Desk, host: str = "127.0.0.1", port: int = 8765) -> None:
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("The web interface listens on this computer only.")
        self.desk = desk
        self.token = secrets.token_urlsafe(24)
        super().__init__((host, port), Handler)
        self.allowed_hosts = {f"127.0.0.1:{self.server_port}", f"localhost:{self.server_port}", f"[::1]:{self.server_port}"}

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/"

    def serve_in_thread(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        thread.start()
        return thread


class Handler(BaseHTTPRequestHandler):
    server: DeskServer
    server_version = "AutoApplierDesk"
    protocol_version = "HTTP/1.0"                                    # one request per connection: a refused POST never leaves its body behind

    def log_message(self, format: str, *args: Any) -> None:        # noqa: A002 - the base class's signature
        pass                                                         # a refresh every few seconds would drown the console

    # ------------------------------------------------------------------ plumbing

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _refuse(self, status: int, message: str) -> None:
        self._json({"ok": False, "error": message}, status)

    def _own_origin(self) -> bool:
        if self.headers.get("Host", "") not in self.server.allowed_hosts:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin.removeprefix("http://") in self.server.allowed_hosts

    def _has_token(self, query: dict[str, list[str]]) -> bool:
        given = self.headers.get("X-Desk-Token") or (query.get("t") or [""])[0]
        return secrets.compare_digest(given.encode("utf-8"), self.server.token.encode("utf-8"))

    # ------------------------------------------------------------------ GET

    def do_HEAD(self) -> None:                                       # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:                                        # noqa: N802
        if not self._own_origin():
            return self._refuse(403, "Not allowed from here.")
        url = urlparse(self.path)
        query = parse_qs(url.query)
        desk = self.server.desk
        if url.path == "/":
            page = (STATIC / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", self.server.token)
            return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8", {"Content-Security-Policy": CSP, "X-Frame-Options": "DENY"})
        if url.path == "/api/ping":
            return self._json({"app": APP_NAME})                     # lets a second start see that the first is already serving
        if not self._has_token(query):
            return self._refuse(403, "Reload the page.")
        try:
            if url.path == "/api/overview":
                return self._json(desk.overview())
            if url.path == "/api/inbox":
                return self._json(desk.inbox())
            if url.path == "/api/sites":
                return self._json(desk.sites())
            if url.path == "/api/answers":
                return self._json(desk.answers())
            if url.path == "/api/job":
                return self._json(desk.job((query.get("id") or [""])[0]))
            if url.path == "/file":
                path = desk.file((query.get("job") or [""])[0], (query.get("name") or [""])[0])
                if path is None:
                    return self._refuse(404, "No such file.")
                return self._send(200, path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        except ActionError as e:
            return self._refuse(404, str(e))
        except Exception as e:                                       # a bad row must show up as an error, not a dead page
            return self._refuse(500, f"{type(e).__name__}: {e}")
        return self._refuse(404, "Not found.")

    # ------------------------------------------------------------------ POST

    def do_POST(self) -> None:                                       # noqa: N802
        if not self._own_origin():
            return self._refuse(403, "Not allowed from here.")
        url = urlparse(self.path)
        if url.path != "/api/act":
            return self._refuse(404, "Not found.")
        if not self._has_token(parse_qs(url.query)):
            return self._refuse(403, "Reload the page.")
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            return self._refuse(415, "JSON only.")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY:
            return self._refuse(413, "That request is too large.")
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._refuse(400, "That was not valid JSON.")
        if not isinstance(body, dict) or not isinstance(body.get("action"), str):
            return self._refuse(400, "Say which action.")
        try:
            return self._json(self.server.desk.act(body["action"], body))
        except ActionError as e:
            return self._refuse(400, str(e))
        except Exception as e:
            return self._refuse(500, f"{type(e).__name__}: {e}")
