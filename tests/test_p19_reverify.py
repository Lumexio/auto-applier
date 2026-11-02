"""Looking again at applications that were sent but never confirmed.

An ``unconfirmed`` application is never sent a second time. LinkedIn and Handshake remember what you applied to, so a later look at the posting can
settle it: the page says you applied (the application is recorded as submitted), or it does not (it is counted and left alone), or the look
could not tell (signed out, a CAPTCHA: written down, not held against the application). The look only reads the page.
"""

from __future__ import annotations

import io
import json
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import reverify
from daemon import Daemon
from state import StateStore
from tests.test_p0_safety import PROFILE, app_row, save_job, tmp_cwd
from tests.test_p3_agent import agent_env, skipped

LINKEDIN = "https://www.linkedin.com/jobs/view/1001"
HANDSHAKE = "https://app.joinhandshake.com/jobs/2002"
GREENHOUSE = "https://boards.greenhouse.io/northwind/jobs/3003"
NOW = datetime.now(timezone.utc) + timedelta(days=3)               # the applications below were all attempted "now": by then they are old enough


def seeded(store, run_id, job_id, url, status="unconfirmed", hours_ago=48):
    """A job whose application ended as ``status``, attempted ``hours_ago`` hours before the NOW of these tests."""
    save_job(store, run_id, job_id, url, company="Northwind Labs", title="Backend Engineer")
    with store.transaction(job_id, run_id, "autonomous") as tx:
        tx.mark(status, error="neither a confirmation nor an error appeared")
    when = (NOW - timedelta(hours=hours_ago)).isoformat()
    store.conn.execute("UPDATE applications SET started_at = ? WHERE job_id = ?", (when, job_id))
    store.conn.commit()


class Shown:
    """The part of a browser session a look uses. ``pages`` maps a URL to what the browser ends up showing there."""

    def __init__(self, pages):
        self.pages, self.calls, self._here = pages, [], ""

    def goto(self, url, **kw):
        self.calls.append(("goto", url))
        self._here = url
        if self.pages.get(url, {}).get("boom"):
            raise TimeoutError("page took too long")

    def current_url(self):
        return self.pages.get(self._here, {}).get("final", self._here)

    def text(self, selector):
        self.calls.append(("text", selector))
        return self.pages.get(self._here, {}).get("text", "")

    def has_captcha(self):
        return bool(self.pages.get(self._here, {}).get("captcha"))

    def requires_login(self):
        return bool(self.pages.get(self._here, {}).get("login"))


def opts(**over):
    return {**reverify.DEFAULTS, "delay_seconds": [0, 0], **over}


# --------------------------------------------------------------------------- no browser: what counts as proof

def test_only_the_words_a_site_shows_on_a_posting_you_applied_to_count_as_proof():
    for text, phrase in (
        ("Backend Engineer\nApplied 2 days ago\nSee application status", "See application status"),
        ("Applied just now", "Applied just now"), ("Applied 3 weeks ago", "Applied 3 weeks ago"), ("Applied 1 hour ago", "Applied 1 hour ago"),
        ("Application submitted", "Application submitted"), ("You applied", "You applied"), ("You've applied to this job", "You've applied"),
        ("You’ve applied", "You’ve applied"),
    ):
        assert reverify.applied_phrase(text) == phrase, text
    for text in (
        "", "Applied jobs", "Applied Materials is hiring", "Applicants who applied earlier are considered first",
        "Once your application submitted is reviewed you will hear from us", "Apply now", "Easy Apply", "You can apply until Friday",
        "Applied science background preferred", "applied 2 days ago",
    ):
        assert reverify.applied_phrase(text) is None, text


def test_a_look_says_what_the_page_shows_and_only_reads_it():
    pages = {
        "https://x.test/a": {"text": "Applied 2 days ago"},
        "https://x.test/b": {"text": "Backend Engineer. Easy Apply"},
        "https://x.test/c": {"final": "https://x.test/uas/login?next=c", "text": "Application submitted"},          # a sign-in page proves nothing
        "https://x.test/d": {"captcha": True, "text": "Applied 2 days ago"},
        "https://x.test/e": {"final": "https://x.test/jobs/other", "text": "Applied 2 days ago"},                    # someone else's posting
        "https://x.test/f": {"login": True, "text": "Sign in to apply"},
        "https://x.test/g": {"boom": True},
        "https://www.x.test/h/": {"final": "https://x.test/h", "text": "You applied"},                               # www and a trailing slash are the same posting
        "https://x.test/i": {"login": True, "text": "Applied 1 day ago"},                                            # a page that says so is believed
    }
    session = Shown(pages)
    seen = {u.rstrip("/")[-1]: reverify.look(session, {"apply_url": u}) for u in pages}
    assert {k: v.result for k, v in seen.items()} == {
        "a": "applied", "b": "not_seen", "c": "signed_out", "d": "blocked", "e": "not_seen", "f": "signed_out", "g": "error", "h": "applied", "i": "applied"}
    assert 'shows "Applied 2 days ago"' in seen["a"].note and "x.test/jobs/other" in seen["e"].note and "TimeoutError" in seen["g"].note
    assert {c[0] for c in session.calls} <= {"goto", "text"}, "a look reads the page: it does not click, fill in or submit anything"


# --------------------------------------------------------------------------- which applications are looked at, and when

def test_an_application_is_due_when_it_is_old_enough_not_looked_at_lately_and_on_a_site_that_remembers():
    with tmp_cwd() as tmp:
        store = StateStore(tmp / "s.db")
        rid = store.start_run("stage3", "h", "p")
        seeded(store, rid, "old", LINKEDIN.replace("1001", "1"), hours_ago=72)
        seeded(store, rid, "young", LINKEDIN.replace("1001", "2"), hours_ago=2)
        seeded(store, rid, "hs", HANDSHAKE, hours_ago=30)
        seeded(store, rid, "gh", GREENHOUSE, hours_ago=100)                              # greenhouse does not remember applications
        seeded(store, rid, "recent", LINKEDIN.replace("1001", "3"), hours_ago=60)
        seeded(store, rid, "tired", LINKEDIN.replace("1001", "4"), hours_ago=90)
        seeded(store, rid, "done", LINKEDIN.replace("1001", "5"), status="submitted", hours_ago=90)
        seeded(store, rid, "parked", LINKEDIN.replace("1001", "6"), status="needs_review", hours_ago=90)
        store.record_reverify_check("recent", "not_seen", "x")
        store.conn.execute("UPDATE reverify_checks SET checked_at = ?", ((NOW - timedelta(hours=3)).isoformat(),))
        for _ in range(4):
            store.record_reverify_check("tired", "not_seen", "x")
        store.conn.execute("UPDATE reverify_checks SET checked_at = ? WHERE job_id = 'tired'", ((NOW - timedelta(days=2)).isoformat(),))
        store.conn.commit()

        rows = store.unconfirmed_with_checks()
        assert {r["job_id"] for r in rows} == {"old", "young", "hs", "gh", "recent", "tired"}, "only unconfirmed applications are candidates"
        assert [r["job_id"] for r in reverify.due(rows, opts(), NOW)] == ["old", "hs"], "oldest first; too young, too recent, looked at enough and the wrong site are out"
        assert [r["job_id"] for r in reverify.due(rows, opts(max_per_cycle=1), NOW)] == ["old"]
        assert [r["job_id"] for r in reverify.due(rows, opts(sites=["handshake"]), NOW)] == ["hs"]
        assert [r["job_id"] for r in reverify.due(rows, opts(min_age_hours=1), NOW)] == ["old", "hs", "young"], "a shorter wait lets the young one in"
        assert [r["job_id"] for r in reverify.due(rows, opts(recheck_hours=1), NOW)] == ["old", "recent", "hs"], "a shorter gap lets the one looked at 3 hours ago in"
        assert [r["job_id"] for r in reverify.due(rows, opts(max_checks=5), NOW)] == ["tired", "old", "hs"], "a higher limit lets the one that was looked at 4 times in"
        assert reverify.due(rows, opts(max_per_cycle=0), NOW) == []
        store.close()


def test_looks_that_could_not_see_the_posting_are_not_held_against_it_and_do_not_delay_the_next():
    with tmp_cwd() as tmp:
        store = StateStore(tmp / "s.db")
        rid = store.start_run("stage3", "h", "p")
        seeded(store, rid, "a", LINKEDIN)
        for result in ("signed_out", "blocked"):
            store.record_reverify_check("a", result, "x")
        row = store.unconfirmed_with_checks()[0]
        assert row["misses"] == 0 and row["last_look"] is None
        assert [r["job_id"] for r in reverify.due([row], opts(), NOW)] == ["a"]
        store.record_reverify_check("a", "error", "x")
        assert store.unconfirmed_with_checks()[0]["last_look"] is not None and store.unconfirmed_with_checks()[0]["misses"] == 0
        store.close()


# --------------------------------------------------------------------------- what a look changes

def test_a_posting_that_says_you_applied_makes_the_application_submitted_and_nothing_else_changes():
    with tmp_cwd() as tmp:
        store = StateStore(tmp / "s.db")
        rid = store.start_run("stage3", "h", "p")
        seeded(store, rid, "yes", LINKEDIN, hours_ago=60)                                  # the oldest is looked at first
        seeded(store, rid, "no", LINKEDIN.replace("1001", "1002"))
        seeded(store, rid, "parked", LINKEDIN.replace("1001", "1003"), status="needs_review")
        shown = Shown({LINKEDIN: {"text": "Applied 2 days ago"}, LINKEDIN.replace("1001", "1002"): {"text": "Easy Apply"},
                       LINKEDIN.replace("1001", "1003"): {"text": "Applied 2 days ago"}})
        trust_before = [tuple(r) for r in store.conn.execute("SELECT * FROM site_trust")]
        attempts_before = app_row(store, "yes")["attempts"]

        out = io.StringIO()
        with redirect_stdout(out):
            summary = reverify.run(store, {"reverify": opts()}, session=shown, now=NOW)
        assert (summary.looked, summary.confirmed) == (2, 1)

        yes, no, parked = app_row(store, "yes"), app_row(store, "no"), app_row(store, "parked")
        assert yes["status"] == "submitted" and yes["error_code"] is None and "confirmed on a later look" in yes["error_message"]
        assert yes["submitted_at"] and yes["attempts"] == attempts_before, "a look is not an attempt"
        assert no["status"] == "unconfirmed" and parked["status"] == "needs_review", "no sign of it, or not an unconfirmed application: unchanged"
        assert [tuple(r) for r in store.conn.execute("SELECT * FROM site_trust")] == trust_before, "a late confirmation does not make the site trusted"
        assert [(r["job_id"], r["result"]) for r in store.conn.execute("SELECT job_id, result FROM reverify_checks ORDER BY id")] == [("yes", "applied"), ("no", "not_seen")]
        events = [dict(r) for r in store.conn.execute("SELECT job_id, stage, event FROM events WHERE stage = 'reverify'")]
        assert events == [{"job_id": "yes", "stage": "reverify", "event": "confirmed"}]
        assert "[Reverify] Backend Engineer @ Northwind Labs" in out.getvalue() and "Recorded as submitted" in out.getvalue()
        assert not store.confirm_unconfirmed("yes", "again") and not store.confirm_unconfirmed("parked", "x"), "only an unconfirmed application can be settled this way"
        assert store.is_already_submitted(LINKEDIN) and store.is_already_submitted(LINKEDIN.replace("1001", "1002")), "settled or not, never applied to again"
        store.close()


def test_a_posting_with_no_sign_of_the_application_is_counted_and_given_up_on_after_max_checks():
    with tmp_cwd() as tmp:
        store = StateStore(tmp / "s.db")
        rid = store.start_run("stage3", "h", "p")
        seeded(store, rid, "a", LINKEDIN)
        shown = Shown({LINKEDIN: {"text": "Backend Engineer. Easy Apply"}})
        cfg = {"reverify": opts(max_checks=3)}
        looks = {}
        for hours in (0, 1, 25, 26, 50, 75, 100):
            clock = NOW + timedelta(hours=hours)
            out = io.StringIO()
            with patch("state.utc_now", lambda: clock.isoformat()), redirect_stdout(out):          # the checks are stamped with the pretend time
                looks[hours] = (reverify.run(store, cfg, session=shown, now=clock).looked, out.getvalue())
        assert {h: n for h, (n, _) in looks.items()} == {0: 1, 1: 0, 25: 1, 26: 0, 50: 1, 75: 0, 100: 0}, "looked at again only after a day, three times, then left alone"
        assert "will look 2 more time(s)" in looks[0][1] and "will look 1 more time(s)" in looks[25][1] and "not looking again" in looks[50][1]
        assert app_row(store, "a")["status"] == "unconfirmed" and len(shown.calls) == 6
        line = reverify.describe(store.unconfirmed_with_checks(), opts(max_checks=3), NOW + timedelta(hours=100))[0]
        assert "looked 3 times, nothing seen: left to you" in line and "does not say you applied" in line
        store.close()


def test_a_signed_out_site_is_left_alone_for_the_rest_of_the_cycle():
    with tmp_cwd() as tmp:
        store = StateStore(tmp / "s.db")
        rid = store.start_run("stage3", "h", "p")
        for n in (1, 2, 3):
            seeded(store, rid, f"l{n}", LINKEDIN.replace("1001", str(n)), hours_ago=100 - n)
        seeded(store, rid, "hs", HANDSHAKE, hours_ago=50)
        shown = Shown({LINKEDIN.replace("1001", "1"): {"final": "https://www.linkedin.com/uas/login"}, HANDSHAKE: {"text": "Application submitted"}})
        out = io.StringIO()
        with redirect_stdout(out):
            summary = reverify.run(store, {"reverify": opts()}, session=shown, now=NOW)
        assert summary.looked == 2 and summary.confirmed == 1 and summary.skipped_sites == ["linkedin"]
        assert [c[1] for c in shown.calls if c[0] == "goto"] == [LINKEDIN.replace("1001", "1"), HANDSHAKE], "the other LinkedIn postings were not opened"
        assert "sign in again" in out.getvalue() and "daemon.py --login" in out.getvalue()
        assert {r["job_id"]: r["misses"] for r in store.unconfirmed_with_checks()} == {"l1": 0, "l2": 0, "l3": 0}
        store.close()


def test_the_browser_is_opened_only_when_there_is_something_to_look_at():
    with tmp_cwd() as tmp:
        store = StateStore(tmp / "s.db")
        rid = store.start_run("stage3", "h", "p")
        seeded(store, rid, "young", LINKEDIN, hours_ago=1)
        seeded(store, rid, "gh", GREENHOUSE, hours_ago=100)
        with patch.object(reverify, "_in_browser", side_effect=AssertionError("a browser was opened for nothing")):
            assert reverify.run(store, {}, now=NOW).looked == 0
            assert reverify.run(StateStore(tmp / "empty.db"), {}, now=NOW).looked == 0
        seeded(store, rid, "old", LINKEDIN.replace("1001", "9"), hours_ago=100)
        with patch.object(reverify, "_in_browser", return_value=reverify.Summary(looked=1)) as opened:
            assert reverify.run(store, {}, now=NOW).looked == 1
            assert [j["job_id"] for j in opened.call_args.args[3]] == ["old"]
        with patch.object(reverify, "_in_browser", side_effect=AssertionError("switched off")):
            assert reverify.run(store, {"reverify": {"enabled": False}}, now=NOW).looked == 0
        store.close()


# --------------------------------------------------------------------------- the daemon and the command line

def test_the_daemon_looks_again_after_stage_3_but_not_in_a_dry_run_and_a_failure_never_fails_the_cycle():
    with tmp_cwd() as tmp:
        cfg = tmp / "config.yaml"
        cfg.write_text(f'daemon:\n  state_db: "{(tmp / "d.db").as_posix()}"\nstage1:\n  target_urls:\n    - "https://jobs.lever.co/netco"\n', encoding="utf-8")
        (tmp / "profile.json").write_text(json.dumps(dict(PROFILE, mode="dry_run")), encoding="utf-8")
        d = Daemon(config_path=str(cfg), profile_path=str(tmp / "profile.json"))
        order = []
        with patch("daemon.fetch_lever_jobs", return_value=[]), patch("daemon.stage3_run", side_effect=lambda *a, **k: order.append("stage3")), \
                patch("daemon.reverify.run", side_effect=lambda *a, **k: order.append("reverify")) as looked:
            d.run_cycle(dry_run=True)
            assert order == ["stage3"], "a dry run changes nothing"
            d.run_cycle(dry_run=False)
            assert order == ["stage3", "stage3", "reverify"], "after stage 3, not before it"
            stop = looked.call_args.kwargs["should_stop"]
            assert looked.call_args.args[0] is d.state and stop() is False
            d.running = False
            assert stop() is True, "a shutdown request stops the looking between two postings"
            d.running = True
            looked.side_effect = RuntimeError("profile is locked")
            out = io.StringIO()
            with redirect_stdout(out):
                d.run_cycle(dry_run=False)
        assert "Could not look at the unconfirmed applications again (RuntimeError: profile is locked)" in out.getvalue()
        assert [r["status"] for r in d.state.conn.execute("SELECT status FROM runs ORDER BY id")] == ["completed"] * 3, "a failed look does not fail the cycle"
        d.state.close()


def test_status_lists_each_application_and_what_will_happen_to_it():
    with tmp_cwd() as tmp:
        db = tmp / "s.db"
        store = StateStore(db)
        rid = store.start_run("stage3", "h", "p")
        seeded(store, rid, "li", LINKEDIN, hours_ago=1)
        seeded(store, rid, "gh", GREENHOUSE, hours_ago=1)
        store.close()
        (tmp / "config.yaml").write_text(f'daemon:\n  state_db: "{db.as_posix()}"\n', encoding="utf-8")
        out = io.StringIO()
        with redirect_stdout(out):
            assert reverify.main(["--config", str(tmp / "config.yaml"), "status"]) == 0
        text = out.getvalue()
        assert text.startswith("2 application(s) sent but never confirmed:")
        assert "[linkedin]" in text and "[greenhouse]" in text
        assert "not re-checked" in text and "does not remember applications" in text

        (tmp / "config.yaml").write_text(f'daemon:\n  state_db: "{(tmp / "empty.db").as_posix()}"\n', encoding="utf-8")
        out = io.StringIO()
        with redirect_stdout(out):
            reverify.main(["--config", str(tmp / "config.yaml"), "status"])
        assert out.getvalue().strip() == "No application is waiting for a confirmation."


def test_running_it_by_hand_refuses_while_the_daemon_is_running():
    with tmp_cwd() as tmp:
        db = tmp / "s.db"
        (tmp / "config.yaml").write_text(f'daemon:\n  state_db: "{db.as_posix()}"\n', encoding="utf-8")
        out = io.StringIO()
        with patch.object(StateStore, "acquire_lock", return_value=False), patch.object(reverify, "run", side_effect=AssertionError("ran")), redirect_stdout(out):
            assert reverify.main(["--config", str(tmp / "config.yaml"), "run"]) == 1
        assert "daemon is running" in out.getvalue()
        released = MagicMock()
        with patch.object(StateStore, "release_lock", released), patch.object(reverify, "run", return_value=reverify.Summary(looked=0)), redirect_stdout(io.StringIO()):
            assert reverify.main(["--config", str(tmp / "config.yaml"), "run"]) == 0
        released.assert_called_once()


# --------------------------------------------------------------------------- real browser, fake site

def test_real_pages_a_posting_that_says_so_is_believed_and_look_alikes_are_not():
    with agent_env() as env:
        if skipped(env):
            return
        sc = env.fresh()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            store = StateStore(Path(d) / "s.db")
            rid = store.start_run("stage3", "h", "p")
            paths = ["/li/job/applied", "/li/job/lookalike", "/li/job/nothing", "/li/job/elsewhere", "/li/job/walled"]
            for n, path in enumerate(paths):
                seeded(store, rid, path, env.base + path, hours_ago=100 - n)
            seeded(store, rid, "/jobs/captcha", env.base + "/jobs/captcha", hours_ago=10)
            with patch.object(reverify, "site_of", lambda url: "linkedin"), redirect_stdout(io.StringIO()):
                reverify.run(store, {"reverify": opts(max_per_cycle=10, sites=["linkedin"])}, session=sc.session, now=NOW)
            got = {r["job_id"]: r["result"] for r in store.conn.execute("SELECT job_id, result FROM reverify_checks")}
            assert got == {"/li/job/applied": "applied", "/li/job/lookalike": "not_seen", "/li/job/nothing": "not_seen",
                           "/li/job/elsewhere": "not_seen", "/li/job/walled": "signed_out"}, got       # the walled one ends the site for this cycle: the captcha page is not opened
            assert {p: app_row(store, p)["status"] for p in paths} == {
                "/li/job/applied": "submitted", "/li/job/lookalike": "unconfirmed", "/li/job/nothing": "unconfirmed",
                "/li/job/elsewhere": "unconfirmed", "/li/job/walled": "unconfirmed"}
            assert all(method == "GET" for method, _ in env.ats.hits), "it only reads: not one form was sent"
            assert env.ats.applications == []
            store.close()

            store = StateStore(Path(d) / "c.db")
            rid = store.start_run("stage3", "h", "p")
            seeded(store, rid, "cap", env.base + "/jobs/captcha", hours_ago=10)
            with patch.object(reverify, "site_of", lambda url: "linkedin"), redirect_stdout(io.StringIO()):
                reverify.run(store, {"reverify": opts(sites=["linkedin"])}, session=sc.session, now=NOW)
            assert [r["result"] for r in store.conn.execute("SELECT result FROM reverify_checks")] == ["blocked"]
            assert app_row(store, "cap")["status"] == "unconfirmed"
            store.close()
