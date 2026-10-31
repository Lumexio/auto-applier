"""Confirmation evidence, question hooks and the fit gate, through the handlers and the whole stage 3 flow.

Each test pins a failure from the real run: every LinkedIn application ended 'unconfirmed' because the handler
dismissed the "Application sent" dialog before anything looked at it; required custom questions were never
answered; a poor fit was applied to anyway.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from llm_client import LLMError
from stage3_apply.ats_handlers import (
    DuplicateApplication,
    GreenhouseHandler,
    LinkedInHandler,
    NeedsHumanInput,
    NotAFit,
)
from stage3_apply.run import stage3_run
from state import StateStore
from tests.conftest import FakeSession
from tests.test_p0_safety import GH_URL, PROFILE, _FakeLocator, _FakePage, app_row, cfg_for, fake_playwright, save_job, tmp_cwd

LI_SUBMIT = "button:has-text('Submit application'), button[aria-label='Submit application']"
LI_DISMISS = "button[aria-label='Dismiss'], button:has-text('Done')"
SPONSOR_Q = {"id": 1, "kind": "text", "label": "Do you hold an active security clearance? *", "options": [], "required": True, "value": "", "input_type": "text"}
EMAIL_Q = {"id": 1, "kind": "text", "label": "Email", "options": [], "required": True, "value": "", "input_type": "text"}


class Scripted:
    """A session whose markers appear after a number of polls (a poll = one wait_for_timeout)."""

    def __init__(self, url, appear=None, url_after=None):
        self.url, self.appear, self.url_after = url, appear or {}, url_after or {}
        self.polls, self.log = 0, []

    def has(self, sel, wait_timeout_s=0):
        self.log.append(("has", sel))
        return self.polls >= self.appear.get(sel, 10**9)

    def text(self, sel):
        return "Please answer this required question"

    def wait_for_timeout(self, ms):
        self.polls += 1
        self.url = self.url_after.get(self.polls, self.url)

    def current_url(self):
        return self.url

    def click(self, sel):
        self.log.append(("click", sel))

    def goto(self, url, *, timeout_s=30):
        self.log.append(("goto", url))
        self.url = url

    def has_captcha(self):
        return False

    def requires_login(self):
        return False

    def screenshot(self):
        return b""


# --------------------------------------------------------------------------- handlers: evidence

def test_outcome_observation_reports_success_rejection_redirect_or_nothing():
    base = "https://boards.greenhouse.io/acme/jobs/1"
    h = GreenhouseHandler(Scripted(base, appear={GreenhouseHandler(None).success_selector(): 3}))
    h.outcome_polls = 8
    ok, why = h._observe_outcome(base)
    assert ok is True and "success marker" in why and h.session.polls == 3          # stopped as soon as the marker appeared

    err = GreenhouseHandler(Scripted(base, appear={GreenhouseHandler(None).error_selectors()[0]: 1}))
    err.outcome_polls = 8
    ok, why = err._observe_outcome(base)
    assert ok is False and "form rejected" in why and "required question" in why

    redirect = GreenhouseHandler(Scripted(base, url_after={2: base + "/confirmation"}))
    redirect.outcome_polls = 8
    assert redirect._observe_outcome(base)[0] is True

    silent = GreenhouseHandler(Scripted(base))
    silent.outcome_polls = 5
    assert silent._observe_outcome(base)[0] is None and silent.session.polls == 5
    assert GreenhouseHandler(Scripted(base)).outcome_polls >= 12                    # real browsers get a real window to show the dialog


def test_linkedin_reads_the_success_dialog_before_dismissing_it_and_leaves_a_rejected_form_open():
    li = LinkedInHandler(Scripted("https://www.linkedin.com/jobs/view/1", appear={LI_SUBMIT: 0, LinkedInHandler(None).success_selector(): 2}))
    li.outcome_polls = 6
    res = li.submit()
    assert res.ok and res.confirmed is True and "success marker" in res.evidence
    clicks = [e for e in li.session.log if e[0] == "click"]
    assert clicks == [("click", LI_SUBMIT), ("click", LI_DISMISS)]
    seen_success = max(i for i, e in enumerate(li.session.log) if e == ("has", li.success_selector()))
    assert seen_success < li.session.log.index(("click", LI_DISMISS)), "the dialog was dismissed before it was read"

    rejected = LinkedInHandler(Scripted("https://www.linkedin.com/jobs/view/1", appear={LI_SUBMIT: 0, LinkedInHandler(None).error_selectors()[2]: 1}))
    rejected.outcome_polls = 6
    res = rejected.submit()
    assert res.ok and res.confirmed is False
    assert ("click", LI_DISMISS) not in rejected.session.log                         # the form stays open so a human can see what it wants

    no_button = LinkedInHandler(Scripted("https://www.linkedin.com/jobs/view/1"))
    assert no_button.submit().ok is False                                            # external application: nothing to submit


def test_a_posting_that_shows_it_was_applied_to_is_a_duplicate_before_and_proof_after():
    url = "https://www.linkedin.com/jobs/view/7"
    applied = LinkedInHandler(Scripted(url, appear={"text=See application status": 0}))
    assert applied.verify_applied({"apply_url": url}) is True and ("goto", url) in applied.session.log
    try:
        applied.prepare(PROFILE, {"id": "7", "apply_url": url})
        raise AssertionError("an already-applied posting must not be filled in again")
    except DuplicateApplication:
        pass
    assert LinkedInHandler(Scripted(url)).verify_applied({"apply_url": url}) is False
    nothing_to_look_for = GreenhouseHandler(Scripted(url, appear={"text=anything": 0}))
    assert nothing_to_look_for.verify_applied({"apply_url": url}) is False
    assert not [e for e in nothing_to_look_for.session.log if e[0] == "goto"]        # a handler with no markers does not even navigate


# --------------------------------------------------------------------------- handlers: hooks

class StubSolver:
    def __init__(self, blocking=False):
        self.calls, self.blocking = 0, blocking

    def solve(self, session, job):
        self.calls += 1
        return SimpleNamespace(blocking=self.blocking, unanswered=[{"label": "Need this?"}] if self.blocking else [])


def test_the_fit_hook_runs_before_anything_is_filled_in():
    session = FakeSession()
    handler = GreenhouseHandler(session)

    def veto(sess, job):
        raise NotAFit("wrong stack")

    handler.before_fill = veto
    try:
        handler.prepare({"name": "T U", "email": "t@x.io"}, {"id": "1", "apply_url": GH_URL})
        raise AssertionError("expected NotAFit")
    except NotAFit:
        pass
    assert [a[0] for a in session.actions] == ["goto"]                               # navigated, then stopped: no field was touched


def test_questions_are_solved_on_every_step_and_a_final_sweep_and_unanswerable_ones_stop_the_application():
    session, solver = FakeSession(), StubSolver()
    handler = GreenhouseHandler(session)
    handler.solver = solver
    handler.prepare({"name": "T U", "email": "t@x.io"}, {"id": "1", "apply_url": GH_URL})
    assert solver.calls == 3                                                         # Greenhouse yields two steps, plus the final sweep

    blocked = StubSolver(blocking=True)
    handler = GreenhouseHandler(FakeSession())
    handler.solver = blocked
    try:
        handler.prepare({"name": "T U", "email": "t@x.io"}, {"id": "1", "apply_url": GH_URL})
        raise AssertionError("expected NeedsHumanInput")
    except NeedsHumanInput as e:
        assert e.questions == [{"label": "Need this?"}] and "Need this?" in str(e)
    assert blocked.calls == 1                                                         # stopped at the first step that had a blocker


# --------------------------------------------------------------------------- stage 3, end to end (fake browser)

class DynLocator(_FakeLocator):
    def inner_text(self):
        return self.page.main_text

    def click(self):
        self.page.actions.append(("click", self.sel))
        if "submit" in self.sel.lower():
            self.page.submitted = True


class DynPage(_FakePage):
    """A form whose questions, post-submit outcome and posting text are configurable."""

    SUCCESS = "#application_confirmation, h1:has-text('Application submitted')"

    def __init__(self, questions=None, after_submit="", main_text=""):
        super().__init__()
        self.questions, self.after_submit, self.main_text, self.submitted, self.values = questions or [], after_submit, main_text, False, {}

    def present(self, sel):
        if sel == "main" and self.main_text:
            return True
        if self.submitted:
            if self.after_submit == "success" and sel == self.SUCCESS:
                return True
            if self.after_submit == "errors" and sel == "[aria-invalid='true']:visible":
                return True
            if self.after_submit == "applied" and sel == "text=You have applied":
                return True
        return super().present(sel)

    def locator(self, sel):
        return DynLocator(self, sel)

    def evaluate(self, *a):
        return self.questions

    def fill(self, sel, value):
        self.actions.append(("fill", sel))
        self.values[sel] = value


LONG_POSTING = "Senior Backend Engineer building payment APIs in Python and PostgreSQL. " * 12


def run_stage3(tmp, page, mode="autonomous", extra_patches=(), job_url=GH_URL):
    store = StateStore(tmp / "s.db")
    rid = store.start_run("stage3", "h", "p")
    job = save_job(store, rid, "g1", job_url)
    cfg = cfg_for(tmp, mode)
    profile = dict(PROFILE, mode=mode)
    with fake_playwright(page), patch("answerer.chat_json", side_effect=LLMError("model down")), patch("stage3_apply.run.wait_for_confirmation", return_value=False) as confirm:
        for p in extra_patches:
            p.start()
        try:
            stage3_run([job], profile, state=store, run_id=rid, config=cfg)
        finally:
            for p in extra_patches:
                p.stop()
    return store, confirm


def events(store):
    return [r["event"] for r in store.conn.execute("SELECT event FROM events ORDER BY id")]


def test_required_questions_nobody_can_answer_park_the_job_and_are_listed_for_the_user():
    with tmp_cwd() as tmp:
        page = DynPage(questions=[SPONSOR_Q])
        store, confirm = run_stage3(tmp, page)
        row = app_row(store, "g1")
        assert (row["status"], row["error_code"]) == ("needs_review", "needs_answers") and "security clearance" in row["error_message"]
        assert not [a for a in page.actions if a[0] == "click"], "nothing may be submitted with a required question unanswered"
        pending = store.list_pending()
        assert [p["question_text"] for p in pending] == ["Do you hold an active security clearance?"] and pending[0]["jobs"] == 1
        assert store.is_already_submitted(GH_URL) is True and not confirm.called
        store.close()


def test_a_handler_confirmed_submit_is_recorded_without_waiting_and_the_answers_are_audited():
    with tmp_cwd() as tmp:
        page = DynPage(questions=[EMAIL_Q], after_submit="success")
        store, confirm = run_stage3(tmp, page)
        row = app_row(store, "g1")
        assert (row["status"], row["error_code"]) == ("submitted", None) and row["submitted_at"]
        assert page.values['[data-aa-id="1"]'] == "test@example.com"                    # the profile rule filled the form's own question
        assert not confirm.called, "the handler already had proof; waiting for a second opinion would only add delay"
        assert store.count_daily_global_submits() == 1
        audit = json.loads((tmp / "applications" / "g1" / "answers.json").read_text(encoding="utf-8"))
        assert audit[0]["answered"]["source"] == "profile" and audit[0]["answered"]["question"] == "Email"
        store.close()


def test_a_form_that_rejects_the_submit_is_parked_and_does_not_count_as_an_application():
    with tmp_cwd() as tmp:
        page = DynPage(questions=[EMAIL_Q], after_submit="errors")
        with patch("stage3_apply.run._handle_recruiter_outreach") as outreach:
            store, confirm = run_stage3(tmp, page)
        row = app_row(store, "g1")
        assert (row["status"], row["error_code"]) == ("needs_review", "rejected_by_form") and "form rejected" in row["error_message"]
        assert store.count_daily_global_submits() == 0 and not outreach.called and not confirm.called
        assert store.is_already_submitted(GH_URL) is True                               # a human looks before anything is retried
        store.close()


def test_a_poor_fit_is_skipped_for_good_and_never_judged_twice():
    with tmp_cwd() as tmp:
        page = DynPage(main_text=LONG_POSTING)
        low = {"fit": 20, "seniority_match": "below", "missing_must_haves": ["Kubernetes"], "deal_breakers": [], "summary": "mostly frontend work"}
        with patch("fit.chat_json", return_value=low) as model:
            store, _ = run_stage3(tmp, page)
            row = app_row(store, "g1")
            assert (row["status"], row["error_code"]) == ("skipped", "not_a_fit") and "20%" in row["error_message"]
            assert not [a for a in page.actions if a[0] == "fill"], "a poor fit must be rejected before the form is touched"
            assert store.is_already_submitted(GH_URL) is True                           # permanent, unlike a CAPTCHA skip
            assert json.loads((tmp / "applications" / "g1" / "fit.json").read_text(encoding="utf-8"))["verdict"] == "skip"

            rid = store.start_run("stage3", "h", "p")
            with fake_playwright(page):
                stage3_run([save_job(store, rid, "g1", GH_URL)], dict(PROFILE, mode="autonomous"), state=store, run_id=rid, config=cfg_for(tmp, "autonomous"))
            assert model.call_count == 1                                                # not even re-assessed
        store.close()


def test_a_fit_that_could_not_be_assessed_is_skipped_for_now_not_for_good():
    """fit.on_llm_error: skip means 'do not apply unchecked', not 'give this job up': a model outage must not cost the posting."""
    with tmp_cwd() as tmp:
        store = StateStore(tmp / "s.db")
        cfg = {**cfg_for(tmp, "autonomous"), "fit": {"on_llm_error": "skip"}}
        page = DynPage(main_text=LONG_POSTING)

        def cycle(**model):
            rid = store.start_run("stage3", "h", "p")
            job = {**save_job(store, rid, "g1", GH_URL), "description": LONG_POSTING}
            with fake_playwright(page), patch("answerer.chat_json", side_effect=LLMError("model down")), patch("fit.chat_json", **model), \
                    patch("stage3_apply.run.wait_for_confirmation", return_value=False):
                stage3_run([job], dict(PROFILE, mode="autonomous"), state=store, run_id=rid, config=cfg)
            return app_row(store, "g1")

        row = cycle(side_effect=LLMError("model down"))
        assert row["status"] == "skipped" and row["error_code"] is None and "on_llm_error" in row["error_message"]
        assert not [a for a in page.actions if a[0] == "fill"], "an unchecked posting must not be filled in"
        assert store.is_already_submitted(GH_URL) is False                              # retryable, like a CAPTCHA skip
        assert row["attempts"] == 0, "an outage is not the job's fault: it must not use up one of its attempts"
        for _ in range(5):                                                                # a long outage: far more cycles than max_attempts
            assert cycle(side_effect=LLMError("model down"))["status"] == "skipped"
        assert store.is_already_submitted(GH_URL) is False                              # still not blocked for good

        low = {"fit": 20, "seniority_match": "below", "missing_must_haves": [], "deal_breakers": [], "summary": "mostly frontend work"}
        row = cycle(return_value=low)                                                    # next cycle the model is back and really judges it
        assert (row["status"], row["error_code"]) == ("skipped", "not_a_fit") and "20%" in row["error_message"]
        assert store.is_already_submitted(GH_URL) is True                               # a real verdict is permanent
        assert row["attempts"] == 1                                                      # and it is a real attempt: only outages are refunded
        store.close()


def test_dry_runs_assess_and_report_but_never_write_application_state():
    with tmp_cwd() as tmp:
        low = {"fit": 10, "seniority_match": "below", "missing_must_haves": [], "deal_breakers": [], "summary": "no"}
        with patch("fit.chat_json", return_value=low):
            store, _ = run_stage3(tmp, DynPage(main_text=LONG_POSTING), mode="dry_run")
        assert app_row(store, "g1") is None and "dry_run_not_a_fit" in events(store)
        assert (tmp / "applications" / "g1" / "fit.json").is_file()
        store.close()

    with tmp_cwd() as tmp:
        store, _ = run_stage3(tmp, DynPage(questions=[SPONSOR_Q]), mode="dry_run")
        assert app_row(store, "g1") is None and "dry_run_needs_answers" in events(store)
        assert [p["question_text"] for p in store.list_pending()] == ["Do you hold an active security clearance?"]   # so you can answer BEFORE going live
        store.close()


def test_an_unconfirmed_submit_is_upgraded_when_reloading_the_posting_shows_it_was_applied():
    with tmp_cwd() as tmp:
        page = DynPage(after_submit="applied")
        marker = patch.object(GreenhouseHandler, "applied_markers", ("You have applied",))
        store, confirm = run_stage3(tmp, page, extra_patches=(marker,))
        row = app_row(store, "g1")
        assert row["status"] == "submitted" and confirm.called                          # the generic wait ran first and found nothing
        store.close()

    with tmp_cwd() as tmp:                                                              # no proof anywhere: stays unconfirmed, with the reason
        store, _ = run_stage3(tmp, DynPage())
        row = app_row(store, "g1")
        assert row["status"] == "unconfirmed" and "neither a confirmation nor an error" in row["error_message"]
        store.close()


def test_assisted_mode_still_asks_first_and_shares_the_same_outcome_pipeline():
    with tmp_cwd() as tmp:
        page = DynPage(after_submit="success")
        with patch("builtins.input", return_value="n"):
            store, _ = run_stage3(tmp, page, mode="assisted")
        assert app_row(store, "g1")["status"] == "skipped" and app_row(store, "g1")["error_code"] is None and not page.submitted
        store.close()

    with tmp_cwd() as tmp:
        page = DynPage(after_submit="success")
        with patch("builtins.input", return_value="y"):
            store, _ = run_stage3(tmp, page, mode="assisted")
        assert app_row(store, "g1")["status"] == "submitted"
        store.close()


def test_linkedin_answers_each_easy_apply_step_before_clicking_next():
    session = FakeSession()
    session._present = {"button:has-text('Easy Apply')", "button:has-text('Next')"}        # a multi-step dialog that never reaches Submit
    handler = LinkedInHandler(session)
    handler.solver = StubSolver(blocking=True)
    try:
        handler.prepare({"name": "T U", "email": "t@x.io", "phone": "123"}, {"id": "1", "apply_url": "https://www.linkedin.com/jobs/view/1"})
        raise AssertionError("an unanswerable step must stop the application")
    except NeedsHumanInput:
        pass
    clicks = [a[1] for a in session.actions if a[0] == "click"]
    assert any("Easy Apply" in c for c in clicks)                                           # the dialog was opened...
    assert not any("Next" in c for c in clicks), clicks                                     # ...but never advanced past an unanswered step

    ok = FakeSession()
    ok._present = {"button:has-text('Easy Apply')", "button:has-text('Submit application')"}
    handler = LinkedInHandler(ok)
    handler.solver = StubSolver()
    handler.prepare({"name": "T U", "email": "t@x.io"}, {"id": "1", "apply_url": "https://www.linkedin.com/jobs/view/1"})
    assert handler.solver.calls >= 2


def test_handshake_success_marker_is_a_phrase_not_the_bare_word_applied():
    from stage3_apply.ats_handlers import HandshakeHandler

    marker = HandshakeHandler(None).success_selector()
    assert "Application submitted" in marker and "has-text('Applied')" not in marker           # 'Applied jobs' in a menu is not a success
