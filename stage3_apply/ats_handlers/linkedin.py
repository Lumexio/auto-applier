"""LinkedIn ATS Handler supporting Easy Apply multi-step modal automation."""

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
class LinkedInHandler(BaseHandler):
    name = "linkedin"
    url_patterns = ("linkedin.com",)
    html_markers = ("jobs-apply-button", "easy-apply", "linkedin.com/jobs")
    # Shown on a posting you already applied to (unverified wording; if wrong, behaviour is unchanged).
    applied_markers = ("See application status", "Application submitted")

    def __init__(self, session: Any, *, timeout_s: int = 30) -> None:
        super().__init__(session, timeout_s=timeout_s)

    def success_selector(self) -> str | None:
        return (
            "div[role='dialog']:has-text('Application sent'), div[role='dialog']:has-text('application was sent'), "
            ".artdeco-inline-feedback--success"
        )

    def iter_steps(self, profile: dict[str, Any], job: dict[str, Any]):
        apply_selectors = [
            "button:has-text('Easy Apply')",
            "button.jobs-apply-button",
            "button:has-text('Apply')",
            "a.jobs-apply-button",
        ]
        has_btn = False
        for sel in apply_selectors:
            if hasattr(self.session, "has") and self.session.has(sel, wait_timeout_s=5):
                has_btn = True
                break

        if not has_btn:
            raise UnsupportedForm(f"{self.name}: no apply button found on job posting")

        is_easy_apply = hasattr(self.session, "has") and (
            self.session.has("button:has-text('Easy Apply')")
            or self.session.has("button.jobs-apply-button:has-text('Easy Apply')")
        )

        if not is_easy_apply:
            external = ("a.jobs-apply-button", "button.jobs-apply-button", "a:has-text('Apply')", "button:has-text('Apply')")
            if self._delegate_external(profile, job, external):
                yield Step(name="external_agent_ready", fields=[])
                return
            yield Step(name="external_apply_redirect", fields=[])
            return

        if hasattr(self.session, "click"):
            self.session.click("button:has-text('Easy Apply'), button.jobs-apply-button")

        if hasattr(self.session, "wait_for_timeout"):
            self.session.wait_for_timeout(2000)

        step_count = 0
        while step_count < 10:
            step_count += 1
            self._solve_questions()          # screening questions on this step of the Easy Apply dialog

            is_review = hasattr(self.session, "has") and (
                self.session.has("button:has-text('Review')")
                or self.session.has("button[aria-label='Review your application']")
            )
            is_submit = hasattr(self.session, "has") and (
                self.session.has("button:has-text('Submit application')")
                or self.session.has("button[aria-label='Submit application']")
            )

            if is_submit:
                yield Step(name="ready_to_submit", fields=[])
                break

            if is_review:
                self.session.click("button:has-text('Review'), button[aria-label='Review your application']")
                if hasattr(self.session, "wait_for_timeout"):
                    self.session.wait_for_timeout(1500)
                continue

            is_next = hasattr(self.session, "has") and (
                self.session.has("button:has-text('Next')")
                or self.session.has("button[aria-label='Continue to next step']")
            )
            if is_next:
                phone = profile.get("phone", "")
                if phone and hasattr(self.session, "fill"):
                    self.session.fill("input[id*='phoneNumber']", phone)

                self.session.click("button:has-text('Next'), button[aria-label='Continue to next step']")
                if hasattr(self.session, "wait_for_timeout"):
                    self.session.wait_for_timeout(1500)
                continue

            break

        yield Step(name="easy_apply_completed", fields=[])

    def submit(self) -> SubmissionResult:
        if self._delegate is not None:                          # the application continued on the employer's own site
            return self._delegate.submit()
        submit_btn = "button:has-text('Submit application'), button[aria-label='Submit application']"
        if hasattr(self.session, "has") and self.session.has(submit_btn, wait_timeout_s=3):
            baseline = self._current_url()
            self.session.click(submit_btn)
            # Read the outcome while the "Application sent" dialog is still up; dismissing it destroys the evidence.
            confirmed, evidence = self._observe_outcome(baseline)
            if confirmed is not False and hasattr(self.session, "click"):
                self.session.click("button[aria-label='Dismiss'], button:has-text('Done')")
            url = self._current_url()
            return SubmissionResult(ok=True, confirmation_url=url, raw={"status": "submitted"}, confirmed=confirmed, evidence=evidence)

        url = self.session.current_url() if hasattr(self.session, "current_url") else ""
        return SubmissionResult(ok=False, confirmation_url=url, raw={"status": "external_application_required"})
