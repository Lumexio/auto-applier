"""Lightweight local REST API for Auto Applier, enabling Langflow and browser web integration.

Security model: the API can read the applicant's PII, rewrite ``profile.json`` and trigger real
applications, so it is not open to the browser at large.
  * every request needs ``Authorization: Bearer <token>`` (token: ``AUTO_APPLIER_API_TOKEN`` or the
    git-ignorable ``.api_token`` file, created on first start);
  * the ``Host`` header must be a loopback name, which defeats DNS-rebinding;
  * no CORS headers are ever sent, so other web pages cannot read responses, and the
    ``Authorization`` header forces a preflight that is refused, so they cannot send writes either.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from profile_gen import parse_resume_to_profile
from scraper import fetch_greenhouse_jobs, fetch_lever_jobs
from stage2_filter.rank import stage2_run
from stage3_apply.run import stage3_run
from state import StateStore

TOKEN_FILE = Path(__file__).resolve().parent / ".api_token"
MAX_BODY_BYTES = 1_000_000
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]"}


def load_or_create_token(token_file: Path = TOKEN_FILE) -> str:
    env = os.environ.get("AUTO_APPLIER_API_TOKEN", "").strip()
    if env:
        return env
    if token_file.is_file():
        existing = token_file.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    token = secrets.token_urlsafe(32)
    token_file.write_text(token, encoding="utf-8")
    return token


def get_profile() -> dict[str, Any]:
    prof_file = Path("profile.json")
    if not prof_file.is_file():
        prof = parse_resume_to_profile()
        prof_file.write_text(json.dumps(prof, indent=2), encoding="utf-8")
        return prof
    return json.loads(prof_file.read_text(encoding="utf-8"))


class AutoApplierAPIHandler(BaseHTTPRequestHandler):
    # Set by run_api_server(); an empty token rejects every request rather than allowing all.
    api_token: str = ""

    def _send_json(self, data: Any, status: int = 200, extra_headers: dict[str, str] | None = None) -> None:
        payload = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):  # IPv6 literal, e.g. "[::1]:8000"
            name = host[: host.find("]") + 1]
        else:
            name = host.split(":", 1)[0]
        return name in LOOPBACK_HOSTS

    def _authorized(self) -> bool:
        """Reject (and answer) the request unless it is from a loopback Host with the right token."""
        if not self._host_ok():
            self._send_json({"error": "Forbidden host"}, status=403)
            return False
        header = self.headers.get("Authorization") or ""
        supplied = header[7:].strip() if header[:7].lower() == "bearer " else ""
        expected = self.api_token
        if not expected or not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
            self._send_json({"error": "Unauthorized"}, status=401, extra_headers={"WWW-Authenticate": "Bearer"})
            return False
        return True

    def do_OPTIONS(self) -> None:
        # Deliberately no Access-Control-Allow-* headers: browsers must not be able to call this API cross-origin.
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_body(self) -> dict[str, Any] | None:
        """Parsed JSON body, ``{}`` when empty/invalid, or None after sending an error (too large)."""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._send_json({"error": "Bad Content-Length"}, status=400)
            return None
        if length > MAX_BODY_BYTES:
            self._send_json({"error": "Request body too large"}, status=413)
            return None
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
        except Exception:
            return {}
        return body if isinstance(body, dict) else {}

    def do_GET(self) -> None:
        if not self._authorized():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/api/profile":
            self._send_json(get_profile())
            return

        if path == "/api/status":
            store = StateStore("state.db")
            try:
                cur = store.conn.cursor()
                cur.execute("SELECT id, started_at, finished_at, status, stage FROM runs ORDER BY id DESC LIMIT 5")
                runs = [dict(r) for r in cur.fetchall()]
                cur.execute("SELECT COUNT(*) FROM jobs")
                total_jobs = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM applications WHERE status = 'submitted'")
                submitted = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM applications WHERE status = 'pending'")
                pending = cur.fetchone()[0]
                self._send_json({
                    "status": "online",
                    "runs": runs,
                    "metrics": {
                        "total_jobs": total_jobs,
                        "submitted": submitted,
                        "pending": pending,
                    }
                })
            finally:
                store.close()
            return

        if path == "/api/jobs":
            jobs_file = Path("jobs.json")
            if jobs_file.is_file():
                self._send_json(json.loads(jobs_file.read_text(encoding="utf-8")))
            else:
                self._send_json({"jobs": []})
            return

        self._send_json({"error": "Not Found", "available_routes": ["/api/profile", "/api/status", "/api/jobs"]}, status=404)

    def do_POST(self) -> None:
        if not self._authorized():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        body = self._read_body()
        if body is None:
            return

        if path == "/api/profile":
            prof_file = Path("profile.json")
            current = get_profile()
            current.update(body)
            prof_file.write_text(json.dumps(current, indent=2), encoding="utf-8")
            self._send_json({"status": "updated", "profile": current})
            return

        if path == "/api/scrape":
            url = body.get("url", "https://boards-api.greenhouse.io/v1/boards/stripe/jobs")
            board = body.get("board", "stripe")
            jobs: list[dict[str, Any]] = []
            if "greenhouse" in url or body.get("source") == "greenhouse":
                jobs = fetch_greenhouse_jobs(board)
            elif "lever" in url or body.get("source") == "lever":
                jobs = fetch_lever_jobs(board)
            self._send_json({"status": "ok", "scraped_count": len(jobs), "jobs": jobs[:50]})
            return

        if path == "/api/filter":
            raw_jobs = body.get("jobs", [])
            profile = body.get("profile") or get_profile()
            store = StateStore("state.db")
            try:
                run_id = store.start_run("stage2", "api_run", "api_profile")
                filtered, diag = stage2_run(raw_jobs, profile, state=store, run_id=run_id)
                store.finish_run(run_id, "completed")
                self._send_json({
                    "status": "ok",
                    "qualified_count": len(filtered),
                    "diagnostics": {
                        "total": diag.total_scraped,
                        "removed_duplicates": diag.removed_duplicates,
                        "removed_exclusions": diag.removed_exclusions,
                        "removed_scam": diag.removed_scam,
                    },
                    "jobs": filtered
                })
            finally:
                store.close()
            return

        if path == "/api/apply":
            jobs = body.get("jobs", [])
            # Only a literal JSON `false` goes live. "false", 0, null or a missing field stay a dry run.
            dry_run = body.get("dry_run") is not False
            profile = get_profile()
            store = StateStore("state.db")
            try:
                run_id = store.start_run("stage3", "api_run", "api_profile")
                cfg = {"stage3": {"mode": "dry_run" if dry_run else "autonomous"}}
                stage3_run(jobs, profile, state=store, run_id=run_id, config=cfg)
                store.finish_run(run_id, "completed")
                self._send_json({"status": "ok", "processed_jobs": len(jobs), "dry_run": dry_run})
            finally:
                store.close()
            return

        self._send_json({"error": "Endpoint not recognized"}, status=404)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - signature fixed by BaseHTTPRequestHandler
        # Request lines can carry job URLs and query strings; keep them out of stderr/daemon logs.
        return


def run_api_server(host: str = "127.0.0.1", port: int = 8000) -> None:
    token = load_or_create_token()
    AutoApplierAPIHandler.api_token = token
    server = ThreadingHTTPServer((host, port), AutoApplierAPIHandler)
    print(f"\n[API] Auto Applier API server running at http://{host}:{port}")
    print(f"[API] Interactive routes: /api/profile, /api/status, /api/scrape, /api/filter, /api/apply")
    print(f"[API] Authentication required: send 'Authorization: Bearer <token>'. Token is in {TOKEN_FILE} (or AUTO_APPLIER_API_TOKEN).")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"[API] WARNING: listening on {host}; this API can trigger real job applications. Keep it on loopback.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[API] Stopping API server...")
        server.server_close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Auto Applier API Service")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on (default: 8000)")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface (default: 127.0.0.1)")
    args = parser.parse_args()
    run_api_server(host=args.host, port=args.port)
