"""Regression tests for the P0 safety fixes.

Each test pins down a failure that was reproduced against the original code:
  * --dry-run really submitted when profile.json said "autonomous";
  * dry runs wrote an invalid 'pending' status (and consumed retry attempts);
  * unconfirmed / failed / interrupted applications were retried forever (duplicate applications);
  * re-saving a job (INSERT OR REPLACE) cascaded away its application history;
  * `time` was never imported on the "profile locked -> Chromium" fallback path;
  * one failing cycle killed the daemon;
  * the REST API answered any website (wildcard CORS, no auth);
  * recruiter outreach ran by default.

Everything is fixture-free (temp dirs, a fake Playwright, an in-process HTTP server) so it runs under
both pytest and run_tests.py, and never touches the real state.db, browser profile or jobs.json.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

from stage2_filter.rank import stage2_run
from stage3_apply.ats_handlers import GreenhouseHandler
from stage3_apply.run import _handle_recruiter_outreach, resolve_mode, stage3_run
from state import StateStore

PROJECT_DIR = Path(__file__).resolve().parent.parent
GH_URL = "https://boards.greenhouse.io/acme/jobs/1"
PROFILE = {"name": "Test User", "email": "test@example.com", "attachments": {}}


# --------------------------------------------------------------------------- helpers

@contextlib.contextmanager
def tmp_cwd():
    """Run in a scratch directory: stage 2/3 write jobs.json, applications/ and the browser profile to CWD."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        old = os.getcwd()
        os.chdir(d)
        try:
            yield Path(d)
        finally:
            os.chdir(old)


class _FakeLocator:
    def __init__(self, page, sel):
        self.page, self.sel = page, sel

    def count(self):
        return 1 if self.page.present(self.sel) else 0

    @property
    def first(self):
        return self

    def click(self):
        self.page.actions.append(("click", self.sel))


class _FakePage:
    def __init__(self, present=("#first_name", "button[type=submit]")):
        self.url = GH_URL
        self._present = set(present)
        self.actions: list[tuple] = []

    def present(self, sel):
        return sel in self._present

    def submit_clicks(self):
        return [a for a in self.actions if a[0] == "click" and "submit" in a[1].lower()]

    def goto(self, url, **kw):
        self.url = url

    def wait_for_timeout(self, ms):
        pass

    def is_visible(self, sel):
        return self.present(sel)

    def fill(self, sel, value):
        self.actions.append(("fill", sel))

    def check(self, sel):
        pass

    def select_option(self, *a, **kw):
        pass

    def set_input_files(self, *a, **kw):
        pass

    def locator(self, sel):
        return _FakeLocator(self, sel)

    def get_by_text(self, text):
        return _FakeLocator(self, "text=" + text)

    def wait_for_selector(self, sel, timeout=0):
        if not self.present(sel):
            raise TimeoutError(sel)

    def screenshot(self):
        return b"png"

    def evaluate(self, *a):
        pass

    def content(self):
        return "<html></html>"


class _FakeContext:
    def __init__(self, page):
        self.pages = [page]
        self.closed = False

    def new_page(self):
        return self.pages[0]

    def close(self):
        self.closed = True


class _FakeLauncher:
    def __init__(self, ctx, fail=False):
        self.ctx, self.fail, self.calls = ctx, fail, 0

    def launch_persistent_context(self, **kw):
        self.calls += 1
        if self.fail:
            raise RuntimeError("profile locked")
        return self.ctx


@contextlib.contextmanager
def fake_playwright(page, firefox_fails=False):
    """Install a fake ``playwright.sync_api`` so no real browser is ever started."""
    ctx = _FakeContext(page)
    pw = types.SimpleNamespace(
        firefox=_FakeLauncher(ctx, fail=firefox_fails),
        chromium=_FakeLauncher(ctx),
        webkit=_FakeLauncher(ctx),
    )

    class _SyncPW:
        def __enter__(self):
            return pw

        def __exit__(self, *a):
            return False

    sync_mod = types.ModuleType("playwright.sync_api")
    sync_mod.sync_playwright = lambda: _SyncPW()
    with patch.dict(sys.modules, {"playwright": types.ModuleType("playwright"), "playwright.sync_api": sync_mod}):
        yield pw, ctx


def save_job(store, run_id, job_id="g1", url=GH_URL, company="Acme", title="Backend Engineer"):
    job = {"id": job_id, "title": title, "company": company, "apply_url": url, "source": "test", "score": 0.9}
    store.save_job(job, run_id)
    return job


def cfg_for(tmp, mode):
    return {
        "stage3": {"mode": mode},
        "browser": {"type": "firefox", "user_data_dir": str(tmp / "bp")},
        "outreach": {"enabled": False},
        # Tests of other behaviour use the fixed handlers and submit freely; the agent and probation have their own tests.
        "agent": {"enabled": False, "probation": {"enabled": False}},
    }


def record(store, run_id, job_id, url, *outcomes):
    """Save a job and walk it through one transaction per outcome ('in_progress' = leave it open)."""
    save_job(store, run_id, job_id, url)
    for status, error in outcomes:
        with store.transaction(job_id, run_id, "autonomous") as tx:
            if status != "in_progress":
                tx.mark(status, error=error)


def app_row(store, job_id):
    row = store.conn.execute("SELECT * FROM applications WHERE job_id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------- dry-run authority

def test_resolve_mode_dry_run_is_authoritative_and_fails_safe():
    assert resolve_mode({"mode": "autonomous"}, {"mode": "dry_run"}) == "dry_run"   # CLI --dry-run / API / Langflow
    assert resolve_mode({"mode": "dry_run"}, {"mode": "autonomous"}) == "dry_run"
    assert resolve_mode({"mode": "autonomous"}, {}) == "autonomous"
    assert resolve_mode({}, {"mode": "assisted"}) == "assisted"
    assert resolve_mode({"mode": "assisted"}, {"mode": "autonomous"}) == "assisted"  # unchanged precedence
    assert resolve_mode({}, {}) == "dry_run"                                          # nothing configured -> never submit
    assert resolve_mode({"mode": "autonomus"}, {}) == "dry_run"                       # typo -> never submit
    assert resolve_mode({"mode": "auto_apply_allowlist"}, {}) == "dry_run"            # unimplemented mode -> never submit


def test_dry_run_never_submits_even_if_profile_says_autonomous():
    with tmp_cwd() as tmp:
        store = StateStore(tmp / "s.db")
        rid = store.start_run("stage3", "h", "p")
        job = save_job(store, rid)
        page = _FakePage()
        with fake_playwright(page):
            stage3_run([job], dict(PROFILE, mode="autonomous"), state=store, run_id=rid, config=cfg_for(tmp, "dry_run"))

        assert page.submit_clicks() == [], "dry run clicked the Submit button"
        # A dry run must leave no application state behind (no failed/pending row, no consumed attempt)...
        assert app_row(store, "g1") is None
        assert store.is_already_submitted(GH_URL) is False
        # ...but is visible in the audit log.
        events = [r["event"] for r in store.conn.execute("SELECT event FROM events")]
        assert events == ["dry_run_prepared"]
        store.close()


def test_autonomous_submit_is_recorded_and_blocks_a_second_attempt():
    """Control for the test above (the harness can submit) + unconfirmed outcomes are never retried."""
    with tmp_cwd() as tmp:
        store = StateStore(tmp / "s.db")
        rid = store.start_run("stage3", "h", "p")
        job = save_job(store, rid)
        page = _FakePage()
        with fake_playwright(page), patch("stage3_apply.run.wait_for_confirmation", return_value=False):
            cfg = cfg_for(tmp, "autonomous")
            stage3_run([job], dict(PROFILE, mode="autonomous"), state=store, run_id=rid, config=cfg)
            assert len(page.submit_clicks()) == 1
            row = app_row(store, "g1")
            assert (row["status"], row["attempts"]) == ("unconfirmed", 1)

            stage3_run([job], dict(PROFILE, mode="autonomous"), state=store, run_id=rid, config=cfg)
            assert len(page.submit_clicks()) == 1, "an unconfirmed application was submitted again"
        store.close()


def test_failure_after_submit_click_blocks_retries_but_missing_button_does_not():
    with tmp_cwd() as tmp:
        store = StateStore(tmp / "s.db")
        rid = store.start_run("stage3", "h", "p")
        cfg = cfg_for(tmp, "autonomous")
        profile = dict(PROFILE, mode="autonomous")

        # Crash *after* the submit click: outcome unknown -> unconfirmed (blocks), not failed (retried).
        job_a = save_job(store, rid, "a", "https://boards.greenhouse.io/acme/jobs/10")
        with fake_playwright(_FakePage()), patch.object(GreenhouseHandler, "submit", side_effect=RuntimeError("boom")):
            stage3_run([job_a], profile, state=store, run_id=rid, config=cfg)
        assert app_row(store, "a")["status"] == "unconfirmed"
        assert store.is_already_submitted(job_a["apply_url"]) is True

        # No submit button at all: nothing was sent -> plain failure, retryable.
        job_b = save_job(store, rid, "b", "https://boards.greenhouse.io/acme/jobs/11", title="Other")
        with fake_playwright(_FakePage(present=("#first_name",))):
            stage3_run([job_b], profile, state=store, run_id=rid, config=cfg)
        assert app_row(store, "b")["status"] == "failed"
        assert store.is_already_submitted(job_b["apply_url"]) is False
        store.close()


# --------------------------------------------------------------------------- missing `import time`

def test_profile_locked_fallback_to_chromium_does_not_crash():
    with tmp_cwd() as tmp:
        page = _FakePage()
        # stage 3
        store = StateStore(tmp / "s.db")
        rid = store.start_run("stage3", "h", "p")
        job = save_job(store, rid)
        with fake_playwright(page, firefox_fails=True) as (pw, _ctx), patch("time.sleep"):
            stage3_run([job], dict(PROFILE, mode="dry_run"), state=store, run_id=rid, config=cfg_for(tmp, "dry_run"))
            assert pw.firefox.calls == 2 and pw.chromium.calls == 1
        assert [r["event"] for r in store.conn.execute("SELECT event FROM events")] == ["dry_run_prepared"]
        store.close()

        # scraper
        import scraper
        with fake_playwright(_FakePage(), firefox_fails=True) as (pw, _ctx), patch("time.sleep"):
            assert scraper.fetch_browser_jobs("https://example.com/jobs", user_data_dir=str(tmp / "bp2")) == []
            assert pw.chromium.calls == 1


def test_browser_context_is_closed_when_a_job_blows_up():
    with tmp_cwd() as tmp:
        store = StateStore(tmp / "s.db")
        rid = store.start_run("stage3", "h", "p")
        job = save_job(store, rid)
        with fake_playwright(_FakePage()) as (_pw, ctx), patch("stage3_apply.run.pick_handler", side_effect=RuntimeError("boom")):
            try:
                stage3_run([job], dict(PROFILE, mode="dry_run"), state=store, run_id=rid, config=cfg_for(tmp, "dry_run"))
            except RuntimeError:
                pass
            assert ctx.closed, "browser context leaked"
        store.close()


# --------------------------------------------------------------------------- state: blocking, caps, upsert

def test_blocking_statuses_and_retry_cap():
    with tmp_cwd() as tmp:
        store = StateStore(tmp / "s.db")
        rid = store.start_run("stage3", "h", "p")
        url = lambda k: f"https://x.test/{k}"  # noqa: E731

        # Anything that is, or may be, a real submission blocks immediately.
        for status in ("unconfirmed", "needs_review", "duplicate", "submitted"):
            record(store, rid, status, url(status), (status, None))
            assert store.is_already_submitted(url(status)) is True, status
        record(store, rid, "open", url("open"), ("in_progress", None))
        assert store.is_already_submitted(url("open")) is True

        # Plain failures are retryable until max_attempts attempts have been made.
        record(store, rid, "f", url("f"), ("failed", "x"))
        assert store.is_already_submitted(url("f")) is False
        record(store, rid, "f", url("f"), ("failed", "x"), ("failed", "x"))
        assert app_row(store, "f")["attempts"] == 3
        assert store.is_already_submitted(url("f")) is True
        assert store.is_already_submitted(url("f"), max_attempts=5) is False

        # CAPTCHA/login skips are retryable too; a deliberate human skip (no error) is final.
        record(store, rid, "c", url("c"), ("skipped", "CAPTCHA encountered"))
        assert store.is_already_submitted(url("c")) is False
        record(store, rid, "h", url("h"), ("skipped", None))
        assert store.is_already_submitted(url("h")) is True
        store.close()


def test_resaving_a_job_keeps_its_application_history():
    with tmp_cwd() as tmp:
        store = StateStore(tmp / "s.db")
        rid = store.start_run("stage1", "h", "p")
        job = save_job(store, rid, "J1", "https://x.test/jobs/1")
        record(store, rid, "J1", "https://x.test/jobs/1", ("failed", "boom"))
        before = app_row(store, "J1")

        for _ in range(3):  # stage 2 re-saves every candidate on every cycle
            store.save_job(dict(job, score=0.5, title="Backend Engineer II"), rid)

        after = app_row(store, "J1")
        assert after is not None, "application row was cascaded away by INSERT OR REPLACE"
        assert (after["status"], after["attempts"], after["error_message"]) == (before["status"], before["attempts"], "boom")
        stored = store.conn.execute("SELECT title, score, status FROM jobs WHERE id = 'J1'").fetchone()
        assert (stored["title"], stored["score"], stored["status"]) == ("Backend Engineer II", 0.5, "pending")
        store.close()


def test_job_id_collisions_do_not_merge_different_postings():
    with tmp_cwd() as tmp:
        store = StateStore(tmp / "s.db")
        rid = store.start_run("stage1", "h", "p")
        a = {"id": "3", "title": "A", "company": "Acme", "apply_url": "https://a.test/jobs/a", "source": "t"}
        b = {"id": "3", "title": "B", "company": "Beta", "apply_url": "https://b.test/jobs/b", "source": "t"}  # same counter id
        store.save_job(a, rid)
        with store.transaction("3", rid) as tx:
            tx.mark("unconfirmed")

        store.save_job(b, rid)
        assert b["id"] != "3", "second posting silently took over the first posting's id"
        assert store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
        assert app_row(store, "3")["status"] == "unconfirmed"
        assert store.is_already_submitted(a["apply_url"]) is True
        assert store.is_already_submitted(b["apply_url"]) is False

        # The same posting coming back under a different scraper-assigned id keeps its stored identity.
        a_again = dict(a, id="9")
        store.save_job(a_again, rid)
        assert a_again["id"] == "3"
        assert store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
        store.close()


def test_stale_in_progress_is_parked_and_dead_runs_are_halted():
    with tmp_cwd() as tmp:
        store = StateStore(tmp / "s.db")
        r1 = store.start_run("stage3", "h", "p")
        r2 = store.start_run("stage3", "h", "p")
        record(store, r2, "old", "https://x.test/old", ("in_progress", None))
        record(store, r2, "new", "https://x.test/new", ("in_progress", None))
        long_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        store.conn.execute("UPDATE applications SET started_at = ? WHERE job_id = 'old'", (long_ago,))
        store.conn.commit()

        assert store.recover_stale_in_progress(minutes=10) == 1
        old, new = app_row(store, "old"), app_row(store, "new")
        assert old["status"] == "needs_review" and "outcome unknown" in old["error_message"]
        assert new["status"] == "in_progress"
        assert store.is_already_submitted("https://x.test/old") is True   # parked, not retried

        assert store.reconcile_stale_runs(keep_run_id=r2) == 1
        status = {r["id"]: r["status"] for r in store.conn.execute("SELECT id, status FROM runs")}
        assert status == {r1: "halted", r2: "running"}
        store.close()


def test_old_database_is_migrated_and_existing_attempts_are_counted():
    old_schema = re.sub(r"\n\s*attempts\s+INTEGER NOT NULL DEFAULT 0,", "", (PROJECT_DIR / "schema.sql").read_text(encoding="utf-8"))
    assert "attempts" not in old_schema.split("CREATE TABLE IF NOT EXISTS applications")[1].split(");")[0]
    with tmp_cwd() as tmp:
        db = tmp / "old.db"
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(db)
        conn.executescript(old_schema)
        conn.execute("INSERT INTO runs(started_at, status, stage, config_hash) VALUES (?, 'completed', 'stage1', 'h')", (now,))
        conn.execute(
            "INSERT INTO jobs(id, run_id, title, company, company_norm, apply_url, apply_url_canon, source, score, status, created_at, updated_at)"
            " VALUES ('j', 1, 't', 'c', 'c', 'https://x.test/j', 'https://x.test/j', 's', 0, 'pending', ?, ?)",
            (now, now),
        )
        conn.execute("INSERT INTO applications(job_id, run_id, status, mode, started_at) VALUES ('j', 1, 'failed', 'autonomous', ?)", (now,))
        conn.commit()
        conn.close()

        store = StateStore(db)
        cols = {r["name"] for r in store.conn.execute("PRAGMA table_info(applications)")}
        assert "attempts" in cols
        assert app_row(store, "j")["attempts"] == 1          # the pre-existing attempt is counted
        with store.transaction("j", 1) as tx:                # and new attempts keep counting from there
            tx.mark("failed", error="again")
        assert app_row(store, "j")["attempts"] == 2
        store.close()
        StateStore(db).close()                               # migrating twice is a no-op


def test_stage2_does_not_requeue_blocked_postings():
    with tmp_cwd() as tmp:
        store = StateStore(tmp / "s.db")
        rid = store.start_run("stage2", "h", "p")
        record(store, rid, "u", GH_URL, ("unconfirmed", None))
        raw = [{"id": "x", "company": "Acme", "title": "Senior Python Engineer", "description": "Python", "apply_url": GH_URL}]
        filtered, diag = stage2_run(raw, {"skills": ["Python"], "seniority": "senior"}, state=store, run_id=rid, config={})
        assert filtered == [] and diag.removed_duplicates == 1
        store.close()


# --------------------------------------------------------------------------- daemon resilience

def test_backoff_doubles_and_is_capped_at_one_interval():
    from daemon import backoff_seconds

    assert [backoff_seconds(n, 3600) for n in (1, 2, 3, 4)] == [60, 120, 240, 480]
    assert backoff_seconds(12, 3600) == 3600 and backoff_seconds(10_000, 3600) == 3600
    assert backoff_seconds(1, 30) == 30        # never longer than the configured interval
    assert backoff_seconds(0, 3600) == 60      # defensive: treated as the first failure


def test_daemon_survives_failing_cycles_and_backs_off():
    from daemon import Daemon

    with tmp_cwd() as tmp:
        cfg = tmp / "config.yaml"
        cfg.write_text(f'daemon:\n  interval_seconds: 3600\n  state_db: "{(tmp / "d.db").as_posix()}"\n', encoding="utf-8")
        d = Daemon(config_path=str(cfg), profile_path=str(tmp / "profile.json"))
        calls, waits = [], []

        def flaky_cycle(dry_run=False):
            calls.append(1)
            if len(calls) <= 3:
                raise RuntimeError("boom")      # used to terminate the whole daemon
            d.running = False                   # 4th cycle succeeds, then the user stops the daemon

        d.run_cycle = flaky_cycle
        d._sleep = lambda seconds: waits.append(seconds)
        d.run_forever()

        assert len(calls) == 4
        assert waits == [60, 120, 240, 3600]    # backoff while failing, normal cadence once healthy
        d.state.close()


def test_daemon_sleep_wakes_immediately_on_shutdown():
    from daemon import Daemon

    with tmp_cwd() as tmp:
        cfg = tmp / "config.yaml"
        cfg.write_text(f'daemon:\n  state_db: "{(tmp / "d.db").as_posix()}"\n', encoding="utf-8")
        d = Daemon(config_path=str(cfg), profile_path=str(tmp / "profile.json"))

        def stop():
            d.running = False
            d._stop.set()

        threading.Timer(0.2, stop).start()
        started = time.monotonic()
        d._sleep(60)
        assert time.monotonic() - started < 5
        d.state.close()


# --------------------------------------------------------------------------- REST API

@contextlib.contextmanager
def api_server(token="test-token"):
    import api

    with patch.object(api.AutoApplierAPIHandler, "api_token", token):
        server = ThreadingHTTPServer(("127.0.0.1", 0), api.AutoApplierAPIHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_address[1]
        finally:
            server.shutdown()
            server.server_close()


def http_call(port, path, method="GET", headers=None, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers=headers or {}, data=body)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_api_requires_a_token_and_never_sends_cors_headers():
    with tmp_cwd(), api_server() as port:
        good = {"Authorization": "Bearer test-token"}
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "test-token"}, {"Authorization": "Bearer "}):
            status, resp_headers, _ = http_call(port, "/api/profile", headers=headers)
            assert status == 401, headers
            assert not [h for h in resp_headers if h.lower().startswith("access-control")]

        status, resp_headers, _ = http_call(port, "/api/nope", headers=good)    # authorised, unknown route
        assert status == 404
        assert not [h for h in resp_headers if h.lower().startswith("access-control")]

        # The pre-flight a hostile web page would need is refused: no Allow-Origin of any kind.
        status, resp_headers, _ = http_call(port, "/api/apply", method="OPTIONS", headers={"Origin": "https://evil.example"})
        assert not [h for h in resp_headers if h.lower().startswith("access-control")]

        # DNS-rebinding: right token but a non-loopback Host header.
        status, _, _ = http_call(port, "/api/nope", headers=dict(good, Host="evil.example"))
        assert status == 403


def test_api_rejects_unauthenticated_writes_and_oversized_bodies():
    with tmp_cwd() as tmp, api_server() as port:
        (tmp / "profile.json").write_text(json.dumps({"name": "Real Name"}), encoding="utf-8")
        status, _, _ = http_call(port, "/api/profile", method="POST", body=json.dumps({"name": "Evil"}).encode())
        assert status == 401
        assert json.loads((tmp / "profile.json").read_text(encoding="utf-8")) == {"name": "Real Name"}

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest("POST", "/api/apply")
        conn.putheader("Authorization", "Bearer test-token")
        conn.putheader("Content-Length", str(5_000_000))
        conn.endheaders()
        assert conn.getresponse().status == 413
        conn.close()


def test_api_apply_goes_live_only_for_a_literal_false():
    import api

    with tmp_cwd() as tmp, api_server() as port, patch.object(api, "stage3_run") as run:
        (tmp / "profile.json").write_text(json.dumps(dict(PROFILE, mode="autonomous")), encoding="utf-8")
        headers = {"Authorization": "Bearer test-token", "Content-Type": "application/json"}

        def mode_for(payload):
            run.reset_mock()
            status, _, _ = http_call(port, "/api/apply", method="POST", headers=headers, body=json.dumps(payload).encode())
            assert status == 200
            return run.call_args.kwargs["config"]["stage3"]["mode"]

        assert mode_for({"jobs": []}) == "dry_run"
        assert mode_for({"jobs": [], "dry_run": True}) == "dry_run"
        assert mode_for({"jobs": [], "dry_run": "false"}) == "dry_run"
        assert mode_for({"jobs": [], "dry_run": 0}) == "dry_run"
        assert mode_for({"jobs": [], "dry_run": None}) == "dry_run"
        assert mode_for({"jobs": [], "dry_run": False}) == "autonomous"


def test_api_token_is_generated_once_and_env_overrides_it():
    import api

    with tmp_cwd() as tmp, patch.dict(os.environ, {"AUTO_APPLIER_API_TOKEN": ""}):
        token_file = tmp / ".api_token"
        first = api.load_or_create_token(token_file)
        assert len(first) >= 32 and token_file.read_text(encoding="utf-8") == first
        assert api.load_or_create_token(token_file) == first
        with patch.dict(os.environ, {"AUTO_APPLIER_API_TOKEN": "from-env"}):
            assert api.load_or_create_token(token_file) == "from-env"


# --------------------------------------------------------------------------- outreach is opt-in

def test_recruiter_outreach_is_off_unless_explicitly_enabled():
    state = MagicMock()
    session = MagicMock()
    _handle_recruiter_outreach(session, {"company": "Acme"}, {}, state, "j", Path("."), {}, is_dry_run=False)
    _handle_recruiter_outreach(session, {"company": "Acme"}, {}, state, "j", Path("."), {"outreach": {}}, is_dry_run=False)
    _handle_recruiter_outreach(session, {"company": "Acme"}, {}, state, "j", Path("."), {"outreach": {"enabled": False}}, is_dry_run=False)
    state.get_outreach_count_today.assert_not_called()
    assert session.method_calls == []

    # Explicitly enabled: it proceeds as far as the daily budget check (99 >= 5, so it stops there).
    state.get_outreach_count_today.return_value = 99
    _handle_recruiter_outreach(
        session, {"company": "Acme"}, {}, state, "j", Path("."), {"outreach": {"enabled": True, "max_per_day": 5}}, is_dry_run=False
    )
    state.get_outreach_count_today.assert_called_once()

    # The shipped config ships with it switched off.
    from daemon import load_yaml_config

    assert load_yaml_config(str(PROJECT_DIR / "config.yaml"))["outreach"]["enabled"] is False


# --------------------------------------------------------------------------- history backfill tool

def test_backfill_restores_erased_history_from_the_log_and_is_idempotent():
    import backfill_history

    log = "\n".join([
        "[Stage 3] Processing: Backend Engineer @ Acme via linkedin",
        "[Stage 3] Application result: unconfirmed",
        "[Stage 3] Processing: Backend Engineer @ Acme via linkedin",
        "[Stage 3] Application result: unconfirmed",
        "[Stage 3] Processing: Platform Engineer @ Globex @ EU via greenhouse",   # ' @ ' inside the title
        "[Stage 3] Application result: submitted",
        "[Stage 3] Processing: Data Engineer @ Initech via linkedin",
        "[Stage 3] Error during application: linkedin: no apply button found on job posting",  # no result -> not backfilled
        "[Stage 3] Processing: Kept Job @ Hooli via lever",
        "[Stage 3] Application result: unconfirmed",
    ])
    with tmp_cwd() as tmp:
        db = tmp / "s.db"
        store = StateStore(db)
        rid = store.start_run("stage2", "h", "p")
        for jid, title, company in [("a", "Backend Engineer", "Acme"), ("g", "Platform Engineer @ Globex", "EU"),
                                    ("i", "Data Engineer", "Initech"), ("h", "Kept Job", "Hooli")]:
            save_job(store, rid, jid, f"https://x.test/{jid}", company=company, title=title)
        record(store, rid, "h", "https://x.test/h", ("unconfirmed", None))     # this one kept its history
        store.close()
        (tmp / "daemon.log").write_text(log, encoding="utf-8")

        ro = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        plan = backfill_history.plan_backfill(ro, backfill_history.parse_log(log))
        ro.close()
        assert {(p["job_id"], p["status"], p["attempts"]) for p in plan} == {("a", "unconfirmed", 2), ("g", "submitted", 1)}

        assert backfill_history.main(["--log", str(tmp / "daemon.log"), "--db", str(db)]) == 0       # dry run writes nothing
        store = StateStore(db)
        assert app_row(store, "a") is None
        store.close()

        assert backfill_history.main(["--log", str(tmp / "daemon.log"), "--db", str(db), "--apply"]) == 0
        store = StateStore(db)
        assert (app_row(store, "a")["status"], app_row(store, "a")["attempts"]) == ("unconfirmed", 2)
        assert app_row(store, "g")["status"] == "submitted"
        assert app_row(store, "i") is None                                    # an error without a result is not evidence
        assert store.is_already_submitted("https://x.test/a") is True         # now blocks automatic retries
        store.close()

        # Running it again finds nothing left to do.
        ro = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        assert backfill_history.plan_backfill(ro, backfill_history.parse_log(log)) == []
        ro.close()


def test_backfill_refuses_to_write_while_the_daemon_holds_the_lock():
    import backfill_history

    with tmp_cwd() as tmp:
        db = tmp / "s.db"
        store = StateStore(db)
        rid = store.start_run("stage2", "h", "p")
        save_job(store, rid, "a", "https://x.test/a", company="Acme", title="Backend Engineer")
        assert store.acquire_lock(pid=999_999, host="some-other-host") is True    # a live daemon elsewhere
        store.close()
        (tmp / "daemon.log").write_text("[Stage 3] Processing: Backend Engineer @ Acme via linkedin\n[Stage 3] Application result: unconfirmed\n", encoding="utf-8")

        assert backfill_history.main(["--log", str(tmp / "daemon.log"), "--db", str(db), "--apply"]) == 2
        store = StateStore(db)
        assert app_row(store, "a") is None
        store.close()
