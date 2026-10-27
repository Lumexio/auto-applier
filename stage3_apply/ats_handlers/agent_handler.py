"""The handler for every site nobody wrote a script for: the application agent behind the normal handler interface.

``prepare`` lets the agent walk the application until it is ready to submit (stage 3 then applies probation and the
dry-run rule exactly as for any other handler); ``submit`` re-checks readiness, clicks the one submit button, and reads
the outcome from the page itself.
"""

from __future__ import annotations

import time
from typing import Any

from agent import AgentStop, ApplyAgent
from page_state import take_snapshot

from ..confirm import looks_submitted
from .base import (
    BaseHandler,
    CaptchaEncountered,
    DuplicateApplication,
    LoginRequired,
    NeedsHumanInput,
    PostingClosed,
    PreparedForm,
    SubmissionResult,
    UnsupportedForm,
)


class AgentHandler(BaseHandler):
    name = "agent"
    outcome_polls = 16

    def __init__(self, session: Any, *, timeout_s: int = 30) -> None:
        super().__init__(session, timeout_s=timeout_s)
        self._agent: ApplyAgent | None = None
        self._ready_button: Any = None                      # the submit button the agent stopped in front of
        self._errors_before: set[str] = set()

    # The agent decides the steps, so there is no fixed list of them and no site-specific success marker.
    def iter_steps(self, profile: dict[str, Any], job: dict[str, Any]):
        return iter(())

    def success_selector(self) -> str | None:
        return None

    # ---- preparing ---------------------------------------------------------

    def prepare(self, profile: dict[str, Any], job: dict[str, Any], *, checkpoint_callback=None) -> PreparedForm:
        self._job = job
        self._navigate(job["apply_url"])
        if self.before_fill:
            self.before_fill(self.session, job)            # the fit gate reads the posting from the page we just opened
        return self.run_from_current_page(profile, job)

    def run_from_current_page(self, profile: dict[str, Any], job: dict[str, Any]) -> PreparedForm:
        """Walk the application from wherever the session is now. Used directly when another handler hands over."""
        self._job = job
        agent = ApplyAgent(
            self.session, profile, job, solve=self._solve_questions, cfg=self.agent_cfg, llm_cfg=self.llm_cfg, trace_dir=self.trace_dir
        )
        try:
            ready = agent.run()
        except AgentStop as stop:
            raise self._exception_for(stop) from stop
        self._agent, self._ready_button = agent, ready.submit
        shot = self.session.screenshot() if hasattr(self.session, "screenshot") else b""
        return PreparedForm(steps=[], review_screenshot=shot, ats=self.name)

    @staticmethod
    def _exception_for(stop: AgentStop) -> Exception:
        reason = f"agent: {stop.reason}"
        if stop.kind == "closed":
            return PostingClosed(reason)
        if stop.kind == "duplicate":
            return DuplicateApplication(reason)
        if stop.kind == "captcha":
            return CaptchaEncountered(reason)
        if stop.kind == "login":
            return LoginRequired(reason)
        if stop.kind == "needs_input":
            return NeedsHumanInput([{"label": g, "kind": "text", "options": [], "reason": stop.reason} for g in (stop.gaps or [stop.reason])])
        return UnsupportedForm(reason)

    # ---- submitting --------------------------------------------------------

    def submit(self) -> SubmissionResult:
        if self._agent is None:
            raise UnsupportedForm("agent: nothing was prepared, so there is nothing to submit")
        snap = take_snapshot(self.session)
        if snap.captcha:
            raise CaptchaEncountered("agent: CAPTCHA at the submit step")
        gaps = snap.required_gaps()
        if gaps:
            raise UnsupportedForm("agent: not ready to submit; still empty: " + ", ".join(gaps[:4]))
        button = self._agent.find_submit(snap, self._ready_button)
        if button is None:
            raise UnsupportedForm("agent: the submit button is missing or no longer unambiguous")
        baseline = self._current_url()
        self._errors_before = set(snap.errors)
        self.session.click_action(button)
        confirmed, evidence = self._observe_outcome(baseline)
        url = self._current_url()
        return SubmissionResult(ok=True, confirmation_url=url, raw={"status": "submitted", "button": button.label}, confirmed=confirmed, evidence=evidence)

    def _observe_outcome(self, baseline_url: str | None = None) -> tuple[bool | None, str]:
        """A page that says so, a redirect to a confirmation URL, or a NEW error message; otherwise unknown."""
        for _ in range(max(1, self.outcome_polls)):
            snap = take_snapshot(self.session)
            if snap.page_type == "confirmed":
                return True, "the page confirms the application was received"
            url = snap.url or self._current_url()
            if baseline_url and url != baseline_url and looks_submitted(url):
                return True, f"redirected to a confirmation page: {url}"
            fresh = [e for e in snap.errors if e not in self._errors_before]
            if fresh:
                return False, "form rejected the submit: " + "; ".join(fresh[:3])
            if snap.page_type in ("closed", "already_applied", "login", "captcha"):
                return None, f"after submitting, the page became: {snap.page_type}"
            self.session.wait_for_timeout(500)
        return None, "neither a confirmation nor an error appeared"
