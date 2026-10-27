"""Greenhouse ATS handler."""

from __future__ import annotations

from typing import Any

from .base import (
    BaseHandler,
    DuplicateApplication,
    FormField,
    LoginRequired,
    Step,
    register,
)


@register
class GreenhouseHandler(BaseHandler):
    name = "greenhouse"
    url_patterns = ("boards.greenhouse.io/", "job-boards.greenhouse.io/", "greenhouse.io/")
    html_markers = ("id=\"application_form\"", "class=\"application--container\"", "Greenhouse")

    def iter_steps(self, profile: dict[str, Any], job: dict[str, Any]):
        if hasattr(self.session, "has") and self.session.has("form[action*='users/sign_in']"):
            raise LoginRequired("greenhouse: sign-in form present")

        first_name = profile.get("first_name", profile["name"].split()[0])
        last_name = profile.get("last_name", " ".join(profile["name"].split()[1:]))

        yield Step(
            name="profile",
            fields=[
                FormField("#first_name", first_name, "text", "First Name", required=True),
                FormField("#last_name", last_name, "text", "Last Name", required=True),
                FormField("#email", profile.get("email"), "text", "Email", required=True),
                FormField("#phone", profile.get("phone"), "text", "Phone"),
                FormField("#location", profile.get("location"), "text", "Location"),
                FormField("#candidate-location", profile.get("location"), "text", "Location (alt)"),
                FormField(
                    "#resume",
                    (profile.get("attachments") or {}).get("resume_path"),
                    "file",
                    "Resume",
                    required=True,
                ),
                FormField(
                    "#cover_letter_text",
                    (profile.get("attachments") or {}).get("cover_template"),
                    "textarea",
                    "Cover Letter",
                ),
            ],
        )

        # Voluntary EEO Disclosures
        yield Step(
            name="eeo",
            fields=[
                FormField("#gender", "Decline to self-identify", "select", "Gender"),
                FormField("#hispanic_ethnicity", "Decline to self-identify", "select", "Ethnicity"),
                FormField("#veteran_status", "I don't wish to answer", "select", "Veteran"),
                FormField("#disability_status", "I don't wish to answer", "select", "Disability"),
            ],
        )

    def success_selector(self) -> str | None:
        return "#application_confirmation, h1:has-text('Application submitted')"

    def submit_selectors(self) -> tuple[str, ...]:
        return (
            "input[type=submit][value*='Submit']",
            "button[type=submit]:has-text('Submit')",
            "#submit_app",
            "button[type=submit]",
        )

    def _precheck_duplicate(self) -> None:
        for marker in ("You have already submitted", "Application already submitted"):
            if hasattr(self.session, "has") and self.session.has(f"text={marker}"):
                raise DuplicateApplication(f"greenhouse: {marker}")
