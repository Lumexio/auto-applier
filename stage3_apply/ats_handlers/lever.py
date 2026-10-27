"""Lever ATS handler."""

from __future__ import annotations

from typing import Any

from .base import (
    BaseHandler,
    DuplicateApplication,
    FormField,
    Step,
    register,
)


@register
class LeverHandler(BaseHandler):
    name = "lever"
    url_patterns = ("jobs.lever.co/", "lever.co/")
    html_markers = ("class=\"application-form\"", "lever", "posting-apply")

    def iter_steps(self, profile: dict[str, Any], job: dict[str, Any]):
        yield Step(
            name="profile",
            fields=[
                FormField("input[name='name']", profile["name"], "text", "Full Name", required=True),
                FormField("input[name='email']", profile.get("email"), "text", "Email", required=True),
                FormField("input[name='phone']", profile.get("phone"), "text", "Phone"),
                FormField("input[name='org']", profile.get("current_company", ""), "text", "Current Company"),
                FormField("input[name='urls[LinkedIn]']", profile.get("linkedin", ""), "text", "LinkedIn"),
                FormField("input[name='urls[GitHub]']", profile.get("github", ""), "text", "GitHub"),
                FormField(
                    "input[name='urls[Portfolio]']",
                    profile.get("portfolio", (profile.get("attachments") or {}).get("portfolio_url", "")),
                    "text",
                    "Portfolio",
                ),
                FormField(
                    "input[name='resume']",
                    (profile.get("attachments") or {}).get("resume_path"),
                    "file",
                    "Resume",
                    required=True,
                ),
                FormField(
                    "textarea[name='comments']",
                    (profile.get("attachments") or {}).get("cover_template", ""),
                    "textarea",
                    "Additional Information",
                ),
            ],
        )

    def success_selector(self) -> str | None:
        return ".application-confirmation, h1:has-text('Application submitted')"

    def submit_selectors(self) -> tuple[str, ...]:
        return (
            "button[type=submit]:has-text('Submit application')",
            "button[type=submit]:has-text('Submit')",
            "input[type=submit]",
        )

    def _precheck_duplicate(self) -> None:
        for marker in ("You have already applied", "Application already submitted"):
            if hasattr(self.session, "has") and self.session.has(f"text={marker}"):
                raise DuplicateApplication(f"lever: {marker}")
