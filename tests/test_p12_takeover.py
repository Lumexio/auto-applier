"""When a scripted handler runs out of things it knows, the application agent reads the page and carries on (or says what it is).

LinkedIn alone ended 17 applications as "failed: no apply button found", every one of them a page the agent could have read: a
closed posting, a link the script had no selector for, a posting already applied to. These tests pin both halves: the hand-over
happens only for "the script found nothing to do", and what the agent decides (closed, stuck, ready to submit) comes back out.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from stage3_apply.ats_handlers import (
    AgentHandler,
    CaptchaEncountered,
    HandshakeHandler,
    LinkedInHandler,
    LoginRequired,
    PostingClosed,
    UnsupportedForm,
)
from stage3_apply.ats_handlers.base import PreparedForm, SubmissionResult
from tests.conftest import FakeSession
from tests.test_p3_agent import ScriptedModel, agent_env, expect, skipped

URL = "https://www.linkedin.com/jobs/view/42"
JOB = {"id": "42", "title": "Backend Engineer", "company": "Acme", "apply_url": URL}


class SeesThePage(FakeSession):
    """A session the agent could work on (it has the agent's click), showing a page with no apply button on it."""

    def click_action(self, action):
        self.actions.append(("click_action", action))


def handler_on(session, cls=LinkedInHandler, **agent_cfg):
    handler = cls(session)
    handler.agent_cfg = agent_cfg
    return handler


def agent_says(outcome=None, error=None):
    """Replace the agent's walk with a fixed result; returns the list of pages it was asked to start from."""
    started: list[str] = []

    def run(self, profile, job):
        started.append(self.session.current_url())
        if error is not None:
            raise error
        return outcome or PreparedForm(steps=[], ats="agent")

    return started, patch.object(AgentHandler, "run_from_current_page", run)


# =========================================================================== no browser: when the hand-over happens

def test_a_script_with_nothing_left_it_knows_how_to_do_hands_the_page_to_the_agent():
    for cls in (LinkedInHandler, HandshakeHandler):
        session = SeesThePage(url=URL)
        handler = handler_on(session, cls)
        started, walk = agent_says()
        with walk:
            form = handler.prepare({}, JOB)
        assert isinstance(form, PreparedForm) and form.ats == "agent", cls
        assert started == [URL] and isinstance(handler._delegate, AgentHandler), cls
        assert not [a for a in session.actions if a[0] == "click"], "the script clicked nothing before handing over"


def test_submitting_after_a_hand_over_is_the_agents_submit_and_the_script_clicks_nothing():
    for cls in (LinkedInHandler, HandshakeHandler):
        session = SeesThePage(url=URL, present={"button[type=submit]", "button:has-text('Submit application')"})
        handler = handler_on(session, cls)
        started, walk = agent_says()
        sent = SubmissionResult(ok=True, confirmed=True, evidence="the agent's own check")
        with walk, patch.object(AgentHandler, "submit", lambda self: sent):
            handler.prepare({}, JOB)
            assert handler.submit() is sent, cls
        assert not [a for a in session.actions if a[0] == "click"], cls                  # the script's own submit selectors were never tried


def test_a_handler_that_relies_on_the_base_submit_also_submits_through_the_agent_that_took_over():
    """Greenhouse, Lever and Workday use BaseHandler.submit (LinkedIn and Handshake have their own): it must hand over too."""
    from stage3_apply.ats_handlers import BaseHandler

    class Odd(BaseHandler):
        name = "odd"

        def iter_steps(self, profile, job):
            raise UnsupportedForm("odd: no form found")
            yield                                                                        # makes this a generator

        def success_selector(self):
            return None

    assert Odd.submit is BaseHandler.submit
    session = SeesThePage(url=URL, present={"button[type=submit]", "button:has-text('Submit application')"})
    handler = handler_on(session, Odd)
    started, walk = agent_says()
    sent = SubmissionResult(ok=True, confirmed=True, evidence="the agent's own check")
    with walk, patch.object(AgentHandler, "submit", lambda self: sent):
        handler.prepare({}, JOB)
        assert started == [URL] and handler.submit() is sent
    assert not [a for a in session.actions if a[0] == "click"], "the base handler's own submit selectors were never tried"


def test_the_agent_may_take_over_unless_a_config_says_otherwise():
    from agent import DEFAULT_AGENT_CFG

    assert DEFAULT_AGENT_CFG["takeover"] is True and DEFAULT_AGENT_CFG["enabled"] is True
    started, walk = agent_says()
    session = SeesThePage(url=URL)
    handler = LinkedInHandler(session)
    handler.agent_cfg = dict(DEFAULT_AGENT_CFG)                                          # what stage 3 hands every handler when config.yaml says nothing
    with walk:
        form = handler.prepare({}, JOB)
    assert form.ats == "agent" and started == [URL]


def test_the_agent_decides_what_the_page_is_and_its_verdict_comes_out_unchanged():
    started, walk = agent_says(error=PostingClosed("agent: this position is no longer accepting applications"))
    with walk:
        e = expect(PostingClosed, lambda: handler_on(SeesThePage(url=URL)).prepare({}, JOB))
    assert "no longer accepting" in str(e) and started == [URL]

    started, walk = agent_says(error=LoginRequired("agent: the page asks to sign in"))
    with walk:
        expect(LoginRequired, lambda: handler_on(SeesThePage(url=URL)).prepare({}, JOB))


def test_when_the_agent_is_stuck_too_the_failure_names_both_what_the_script_and_the_agent_saw():
    started, walk = agent_says(error=UnsupportedForm("agent: no way forward found on this page"))
    handler = handler_on(SeesThePage(url=URL))
    with walk:
        e = expect(UnsupportedForm, lambda: handler.prepare({}, JOB))
    assert "no apply button found" in str(e) and "agent: no way forward" in str(e)
    assert handler._delegate is None, "a stuck agent must not be the one that submits"


def test_the_agent_is_not_asked_when_it_is_switched_off_or_cannot_see_the_page():
    cases = (
        ({"enabled": False}, SeesThePage(url=URL)),                                      # no agent at all
        ({"takeover": False}, SeesThePage(url=URL)),                                     # the agent works on unknown sites but never takes over
        ({}, FakeSession(url=URL)),                                                      # a session without the agent's eyes
    )
    for cfg, session in cases:
        started, walk = agent_says()
        with walk:
            e = expect(UnsupportedForm, lambda: handler_on(session, **cfg).prepare({}, JOB))
        assert str(e) == "linkedin: no apply button found on job posting" and started == [], cfg


def test_a_captcha_or_a_login_wall_is_never_handed_over_as_if_the_script_had_merely_run_out():
    for session, wanted in ((SeesThePage(url=URL, captcha=True), CaptchaEncountered), (SeesThePage(url=URL, login=True), LoginRequired)):
        started, walk = agent_says()
        with walk:
            expect(wanted, lambda: handler_on(session).prepare({}, JOB))
        assert started == [], wanted


def test_a_page_with_no_apply_button_is_judged_after_one_wait_not_one_wait_per_selector():
    waits: list[tuple[str, float]] = []

    class Nothing(SeesThePage):
        def has(self, sel, wait_timeout_s=0):
            if wait_timeout_s:
                waits.append((sel, wait_timeout_s))
            return False

    for cls in (LinkedInHandler, HandshakeHandler):
        waits.clear()
        expect(UnsupportedForm, lambda: handler_on(Nothing(url=URL), cls, enabled=False).prepare({}, JOB))
        assert len(waits) == 1 and waits[0][0].count(", ") >= 3 and waits[0][1] >= 5, (cls, waits)

    class Appears(SeesThePage):
        def has(self, sel, wait_timeout_s=0):
            return bool(wait_timeout_s) and sel == "first, second"

    assert handler_on(Appears(url=URL))._any_present(["first", "second"]) is True          # one that shows up during the wait is found
    assert handler_on(Nothing(url=URL))._any_present(["first", "second"]) is False
    assert handler_on(SeesThePage(url=URL, present={"second"}))._any_present(["first", "second"]) is True      # one already there needs no wait
    assert handler_on(object())._any_present(["first"]) is False                              # a session that cannot look has nothing


# =========================================================================== real browser, fake LinkedIn-like pages

def test_a_link_the_linkedin_script_has_no_selector_for_is_followed_by_the_agent_and_one_application_arrives():
    with agent_env() as env:
        if skipped(env):
            return
        sc = env.fresh(model=ScriptedModel(), handler_cls=LinkedInHandler)
        sc.run("/li/job/nobutton")
        assert env.ats.applications == [] and isinstance(sc.handler._delegate, AgentHandler)
        res = sc.handler.submit()
        assert res.ok and res.confirmed is True and len(env.ats.applications) == 1


def test_a_closed_linkedin_posting_ends_as_closed_instead_of_failed_and_nothing_is_sent():
    with agent_env() as env:
        if skipped(env):
            return
        sc = env.fresh(model=ScriptedModel(), handler_cls=LinkedInHandler)
        expect(PostingClosed, lambda: sc.run("/li/job/closed"))
        assert env.ats.applications == [] and [p for m, p in env.ats.hits if m == "POST"] == []


def test_a_page_with_nothing_to_click_still_fails_and_says_why_in_both_voices():
    with agent_env() as env:
        if skipped(env):
            return
        sc = env.fresh(model=ScriptedModel(), handler_cls=LinkedInHandler)
        e = expect(UnsupportedForm, lambda: sc.run("/li/job/nothing"))
        assert "no apply button found" in str(e) and "agent:" in str(e) and env.ats.applications == []

        sc = env.fresh(model=ScriptedModel(), handler_cls=LinkedInHandler, agent_cfg={"takeover": False})
        e = expect(UnsupportedForm, lambda: sc.run("/li/job/nobutton"))
        assert str(e) == "linkedin: no apply button found on job posting" and env.ats.hit_paths() == ["/li/job/nobutton"]


# =========================================================================== the sign-in check looks at the page it just opened

def test_the_sign_in_check_looks_at_the_page_just_opened_not_the_one_before():
    class Walled(SeesThePage):
        def requires_login(self):
            return "wall" in self.url                                  # a page that shows a sign-in form

    stale = Walled(url="https://www.linkedin.com/jobs/view/1?wall")      # the tab still shows the previous job: a wall
    e = expect(UnsupportedForm, lambda: handler_on(stale, enabled=False).prepare({}, JOB))
    assert "no apply button" in str(e), "the old page's sign-in wall must not decide this job"
    assert stale.url == URL

    behind_wall = Walled(url=URL)
    walled_job = dict(JOB, apply_url="https://www.linkedin.com/jobs/view/2?wall")
    expect(LoginRequired, lambda: handler_on(behind_wall).prepare({}, walled_job))
    assert behind_wall.url == walled_job["apply_url"], "it opened the posting first, then looked"


# =========================================================================== what a failure leaves behind

def test_a_failed_attempt_keeps_a_picture_and_what_the_page_said_and_never_breaks_the_handling_of_the_failure():
    import tempfile
    from pathlib import Path

    from stage3_apply.run import _save_failure_evidence

    class Showing(SeesThePage):
        def snapshot_raw(self):
            return [{"frame": 0, "url": URL, "title": "Backend Engineer | Acme", "text": "Sorry, this position is no longer accepting applications.",
                     "actions": [], "files": [], "errors": ["Phone is invalid"]}]

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        folder = Path(d)
        _save_failure_evidence(Showing(url=URL), folder, "closed", PostingClosed("agent: it is closed"))
        saved = json.loads((folder / "failure.json").read_text(encoding="utf-8"))
        assert (folder / "failure.png").read_bytes().startswith(b"\x89PNG")
        assert (saved["kind"], saved["url"], saved["page_type"]) == ("closed", URL, "closed")
        assert "agent: it is closed" in saved["error"] and "no longer accepting" in saved["text"] and saved["errors"] == ["Phone is invalid"]
        assert saved["title"] == "Backend Engineer | Acme" and saved["saved_at"]

        class Broken:
            def screenshot(self):
                raise RuntimeError("the browser is gone")

            def current_url(self):
                raise RuntimeError("the browser is gone")

        _save_failure_evidence(Broken(), folder / "nowhere" / "deeper", "failed", RuntimeError("x"))        # must simply return

        odd = folder / "odd"
        odd.mkdir()

        class CannotRead(SeesThePage):
            def snapshot_raw(self):
                raise RuntimeError("the page is gone")

        _save_failure_evidence(CannotRead(url=URL), odd, "failed", RuntimeError("x"))                        # the picture and address are still kept
        kept = json.loads((odd / "failure.json").read_text(encoding="utf-8"))
        assert kept["url"] == URL and kept["kind"] == "failed" and "page_type" not in kept and (odd / "failure.png").is_file()


# =========================================================================== the whole stage 3 cycle, real browser, fake site

def cycle(tmp, store, rid, jobs, **agent):
    """One stage 3 cycle over ``jobs`` (several, unlike run_stage3) with the models unreachable."""
    from llm_client import LLMError
    from stage3_apply.run import stage3_run
    from tests.test_p3_agent import stage3_cfg

    cfg = stage3_cfg(tmp, probation={"enabled": False}, **agent)
    with patch("answerer.chat_json", side_effect=LLMError("down")), patch("agent.chat_json", side_effect=LLMError("down")):
        from tests.test_p3_agent import profile_for

        stage3_run(jobs, dict(profile_for(tmp), mode="autonomous"), state=store, run_id=rid, config=cfg)


def test_a_failure_leaves_evidence_in_the_jobs_folder_and_a_later_success_removes_it():
    from tests.fake_ats import FakeATS
    from tests.test_p0_safety import app_row, save_job, tmp_cwd
    from state import StateStore

    ats = FakeATS()
    base = ats.start()
    try:
        with tmp_cwd() as tmp:
            try:
                from playwright.sync_api import sync_playwright

                with sync_playwright() as p:
                    p.chromium.launch(headless=True).close()
            except Exception:
                print("  (skipped: no Playwright browser available)")
                return
            (tmp / "facts.yaml").write_text(
                'work_authorization:\n  authorized_countries: ["MX", "US"]\n  requires_sponsorship: false\nexperience:\n  years_by_skill:\n    Python: 8\n', encoding="utf-8")
            store = StateStore(tmp / "s.db")
            rid = store.start_run("stage3", "h", "p")
            job = save_job(store, rid, "a1", base + "/loop")
            cycle(tmp, store, rid, [job])
            assert app_row(store, "a1")["status"] == "failed"
            folder = tmp / "applications" / "a1"
            saved = json.loads((folder / "failure.json").read_text(encoding="utf-8"))
            assert saved["kind"] == "failed" and "no way forward" in saved["error"] and saved["url"].endswith("/loop") and saved["page_type"]
            assert (folder / "failure.png").stat().st_size > 100

            retry = dict(job, apply_url=base + "/apply/single")                # the same job (same folder), tried again, and this time it works
            cycle(tmp, store, rid, [retry])
            assert app_row(store, "a1")["status"] == "submitted"
            assert not (folder / "failure.png").exists() and not (folder / "failure.json").exists(), "an old picture must not describe a new run"
            store.close()
    finally:
        ats.stop()


def test_a_signed_out_site_is_given_up_after_three_walls_and_no_job_pays_for_it():
    from tests.fake_ats import FakeATS
    from tests.test_p0_safety import app_row, save_job, tmp_cwd
    from state import StateStore

    ats = FakeATS()
    base = ats.start()
    try:
        with tmp_cwd() as tmp:
            try:
                from playwright.sync_api import sync_playwright

                with sync_playwright() as p:
                    p.chromium.launch(headless=True).close()
            except Exception:
                print("  (skipped: no Playwright browser available)")
                return
            store = StateStore(tmp / "s.db")
            rid = store.start_run("stage3", "h", "p")
            walls = [save_job(store, rid, f"w{i}", base + f"/jobs/login/{i}") for i in range(1, 6)]
            fine = save_job(store, rid, "ok", base + "/apply/single")           # same site, queued behind the walls
            cycle(tmp, store, rid, walls + [fine])
            for i in (1, 2, 3):
                row = app_row(store, f"w{i}")
                assert (row["status"], row["error_message"], row["attempts"]) == ("skipped", "Login required", 0), (i, row)   # attempts refunded
                assert store.is_already_submitted(base + f"/jobs/login/{i}") is False, "a sign-in wall must not use up a job's retries"
            assert [app_row(store, j) for j in ("w4", "w5", "ok")] == [None, None, None], "after three walls the site's jobs wait"
            assert ats.applications == []
            warning = store.conn.execute("SELECT payload FROM events WHERE event = 'signed_out'").fetchall()
            assert len(warning) == 1 and "127.0.0.1" in warning[0][0]
            assert (tmp / "applications" / "w1" / "failure.json").is_file()      # what the wall looked like
            store.close()
    finally:
        ats.stop()


def test_every_stop_that_is_not_a_success_keeps_what_the_page_showed():
    from tests.fake_ats import FakeATS
    from tests.test_p0_safety import save_job, tmp_cwd
    from state import StateStore

    ats = FakeATS()
    base = ats.start()
    try:
        with tmp_cwd() as tmp:
            try:
                from playwright.sync_api import sync_playwright

                with sync_playwright() as p:
                    p.chromium.launch(headless=True).close()
            except Exception:
                print("  (skipped: no Playwright browser available)")
                return
            store = StateStore(tmp / "s.db")
            rid = store.start_run("stage3", "h", "p")
            jobs = [save_job(store, rid, "k1", base + "/jobs/closed"), save_job(store, rid, "k2", base + "/jobs/captcha"),
                    save_job(store, rid, "k3", base + "/loop")]
            cycle(tmp, store, rid, jobs)
            saved = {j: json.loads((tmp / "applications" / j / "failure.json").read_text(encoding="utf-8")) for j in ("k1", "k2", "k3")}
            assert {j: s["kind"] for j, s in saved.items()} == {"k1": "closed", "k2": "captcha", "k3": "failed"}
            assert saved["k1"]["page_type"] == "closed" and "no longer accepting" in saved["k1"]["text"]
            assert all((tmp / "applications" / j / "failure.png").is_file() for j in saved)
            store.close()
    finally:
        ats.stop()


def test_a_submit_that_may_have_gone_through_keeps_its_page_as_unconfirmed_evidence_not_as_a_plain_failure():
    from stage3_apply.ats_handlers import BaseHandler
    from tests.fake_ats import FakeATS
    from tests.test_p0_safety import app_row, save_job, tmp_cwd
    from state import StateStore

    ats = FakeATS()
    base = ats.start()
    try:
        with tmp_cwd() as tmp:
            try:
                from playwright.sync_api import sync_playwright

                with sync_playwright() as p:
                    p.chromium.launch(headless=True).close()
            except Exception:
                print("  (skipped: no Playwright browser available)")
                return
            (tmp / "facts.yaml").write_text(
                'work_authorization:\n  authorized_countries: ["MX", "US"]\n  requires_sponsorship: false\nexperience:\n  years_by_skill:\n    Python: 8\n', encoding="utf-8")
            store = StateStore(tmp / "s.db")
            rid = store.start_run("stage3", "h", "p")
            job = save_job(store, rid, "u1", base + "/apply/single")

            def lost_the_connection(self):
                raise RuntimeError("the connection dropped after the click")

            patches = [patch.object(cls, "submit", lost_the_connection) for cls in (BaseHandler, AgentHandler)]
            for p in patches:
                p.start()
            try:
                cycle(tmp, store, rid, [job])
            finally:
                for p in patches:
                    p.stop()
            assert app_row(store, "u1")["status"] == "unconfirmed"
            saved = json.loads((tmp / "applications" / "u1" / "failure.json").read_text(encoding="utf-8"))
            assert saved["kind"] == "unconfirmed" and "connection dropped" in saved["error"]
            store.close()
    finally:
        ats.stop()


def test_a_working_page_clears_the_count_so_only_walls_in_a_row_end_a_sites_turn():
    from tests.fake_ats import FakeATS
    from tests.test_p0_safety import app_row, save_job, tmp_cwd
    from state import StateStore

    ats = FakeATS()
    base = ats.start()
    try:
        with tmp_cwd() as tmp:
            try:
                from playwright.sync_api import sync_playwright

                with sync_playwright() as p:
                    p.chromium.launch(headless=True).close()
            except Exception:
                print("  (skipped: no Playwright browser available)")
                return
            (tmp / "facts.yaml").write_text(
                'work_authorization:\n  authorized_countries: ["MX", "US"]\n  requires_sponsorship: false\nexperience:\n  years_by_skill:\n    Python: 8\n', encoding="utf-8")
            store = StateStore(tmp / "s.db")
            rid = store.start_run("stage3", "h", "p")
            order = [("w1", "/jobs/login/1"), ("w2", "/jobs/login/2"), ("ok", "/apply/single"), ("w3", "/jobs/login/3"),
                     ("w4", "/jobs/login/4"), ("w5", "/jobs/login/5"), ("w6", "/jobs/login/6")]
            cycle(tmp, store, rid, [save_job(store, rid, job_id, base + path) for job_id, path in order])
            assert app_row(store, "ok")["status"] == "submitted"
            assert [app_row(store, j) is not None for j, _ in order] == [True] * 6 + [False], "three walls in a row, counted again after the page that worked"
            assert store.conn.execute("SELECT COUNT(*) FROM events WHERE event = 'signed_out'").fetchone()[0] == 1
            store.close()
    finally:
        ats.stop()


def test_an_option_is_picked_by_its_exact_text_else_by_the_only_choice_containing_it_and_never_by_guessing():
    from stage3_apply.run import PlaywrightSession

    class Choice:
        def __init__(self, index, log):
            self.index, self.log = index, log

        def click(self, timeout=0):
            self.log.append(self.index)

    class Choices:
        def __init__(self, texts):
            self.texts, self.clicked = texts, []

        def all_inner_texts(self):
            return self.texts

        def nth(self, i):
            return Choice(i, self.clicked)

    pick = PlaywrightSession._pick_option
    several = Choices(["Mexico City", " mexico ", "New Mexico"])
    assert pick(several, "Mexico") is True and several.clicked == [1]                  # the exact one, whatever else contains the word
    assert pick(Choices(["Canada (French)"]), "Canada") is True                          # the only one containing it
    two = Choices(["Mexico City", "New Mexico"])
    assert pick(two, "Mexico") is False and two.clicked == []                            # two candidates: a guess, so nothing is clicked
    assert pick(Choices(["Spain"]), "") is False and pick(Choices([]), "Mexico") is False
    assert pick(Choices(["", "Spain"]), "Mexico") is False

    class Broken(Choices):
        def all_inner_texts(self):
            raise RuntimeError("detached")

    assert pick(Broken([]), "Mexico") is False
