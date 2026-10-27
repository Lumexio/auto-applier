"""Handshake ATS Handler supporting Quick apply and external job portal redirection."""

from __future__ import annotations

from typing import Any

from .base import (
    BaseHandler,
    PreparedForm,
    Step,
    SubmissionResult,
    UnsupportedForm,
    register,
)


@register
class HandshakeHandler(BaseHandler):
    name = "handshake"
    url_patterns = ("joinhandshake.com",)
    html_markers = ("joinhandshake.com", "handshake")

    def success_selector(self) -> str | None:
        # Phrases only. The old `div:has-text('Applied')` matched any page containing the word (menus, "Applied jobs").
        return "div[role='dialog']:has-text('Application submitted'), div[role='dialog']:has-text('application has been submitted'), h1:has-text('Application submitted')"

    def iter_steps(self, profile: dict[str, Any], job: dict[str, Any]):
        apply_selectors = [
            "button:has-text('Quick apply')",
            "button:has-text('Apply on employer website')",
            "button:has-text('Apply')",
            "a:has-text('Apply on employer website')",
        ]
        has_btn = False
        for sel in apply_selectors:
            if hasattr(self.session, "has") and self.session.has(sel, wait_timeout_s=5):
                has_btn = True
                break

        if not has_btn:
            raise UnsupportedForm(f"{self.name}: no apply button found on Handshake job")

        is_quick_apply = hasattr(self.session, "has") and self.session.has("button:has-text('Quick apply')")
        if is_quick_apply:
            if hasattr(self.session, "click"):
                self.session.click("button:has-text('Quick apply')")
            if hasattr(self.session, "wait_for_timeout"):
                self.session.wait_for_timeout(1500)
            yield Step(name="handshake_quick_apply", fields=[])
        else:
            external = ("button:has-text('Apply on employer website')", "a:has-text('Apply on employer website')", "a:has-text('Apply')", "button:has-text('Apply')")
            if self._delegate_external(profile, job, external):
                yield Step(name="handshake_external_agent_ready", fields=[])
            else:
                yield Step(name="handshake_external_redirect", fields=[])

    def submit(self) -> SubmissionResult:
        if self._delegate is not None:                          # the application continued on the employer's own site
            return self._delegate.submit()
        quick_submit = "button:has-text('Submit application'), button:has-text('Submit')"
        if hasattr(self.session, "has") and self.session.has(quick_submit, wait_timeout_s=3):
            baseline = self._current_url()
            self.session.click(quick_submit)
            confirmed, evidence = self._observe_outcome(baseline)
            url = self._current_url()
            return SubmissionResult(ok=True, confirmation_url=url, raw={"status": "submitted"}, confirmed=confirmed, evidence=evidence)

        url = self.session.current_url() if hasattr(self.session, "current_url") else ""
        return SubmissionResult(ok=False, confirmation_url=url, raw={"status": "external_application_required"})
