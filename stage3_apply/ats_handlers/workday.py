"""Workday ATS autonomous multi-step state machine handler."""

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
class WorkdayHandler(BaseHandler):
    name = "workday"
    url_patterns = (".myworkdayjobs.com/", "myworkday.com/")
    html_markers = ("data-automation-id=\"jobTitle\"", "wd-", "Workday")

    def iter_steps(self, profile: dict[str, Any], job: dict[str, Any]):
        # Step 1: Check Auth
        if self._needs_auth():
            raise LoginRequired("Workday requires candidate account authentication.")

        # Step 2: My Information
        yield Step(
            name="my_information",
            fields=[
                FormField(
                    "input[data-automation-id='legalNameSection_firstName']",
                    profile.get("first_name", profile["name"].split()[0]),
                    "text",
                    "First Name",
                    required=True,
                ),
                FormField(
                    "input[data-automation-id='legalNameSection_lastName']",
                    profile.get("last_name", " ".join(profile["name"].split()[1:])),
                    "text",
                    "Last Name",
                    required=True,
                ),
                FormField(
                    "input[data-automation-id='email']",
                    profile.get("email"),
                    "text",
                    "Email",
                    required=True,
                ),
                FormField(
                    "input[data-automation-id='phone-number']",
                    profile.get("phone"),
                    "text",
                    "Phone",
                ),
                FormField(
                    "input[data-automation-id='addressSection_city']",
                    profile.get("location"),
                    "text",
                    "City",
                ),
                FormField(
                    "input[type='file'][data-automation-id='file-upload-input-ref']",
                    (profile.get("attachments") or {}).get("resume_path"),
                    "file",
                    "Resume",
                    required=True,
                ),
            ],
            next_selector="button[data-automation-id='bottom-navigation-next-button']",
        )

        # Step 3: My Experience
        yield Step(
            name="my_experience",
            fields=[],
            next_selector="button[data-automation-id='bottom-navigation-next-button']",
        )

        # Step 4: Voluntary Disclosures (EEO decline-by-default)
        yield Step(
            name="voluntary_disclosures",
            fields=[
                FormField("select[data-automation-id='gender']", "Decline to State", "select", "Gender"),
                FormField("select[data-automation-id='ethnicity']", "Decline to State", "select", "Ethnicity"),
                FormField("select[data-automation-id='veteranStatus']", "I do not wish to answer", "select", "Veteran"),
                FormField("select[data-automation-id='disabilityStatus']", "I do not wish to answer", "select", "Disability"),
            ],
            next_selector="button[data-automation-id='bottom-navigation-next-button']",
        )

        # Step 5: Review
        yield Step(
            name="review",
            fields=[],
            next_selector="button[data-automation-id='bottom-navigation-next-button']",
        )

    def success_selector(self) -> str | None:
        return "[data-automation-id='confirmationPage'], h1:has-text('Application Submitted')"

    def _needs_auth(self) -> bool:
        if not hasattr(self.session, "has"):
            return False
        return (
            self.session.has("button:has-text('Sign In')")
            or self.session.has("button:has-text('Create Account')")
            or self.session.has("input[data-automation-id='email'][type='password']")
        )

    def _precheck_duplicate(self) -> None:
        for marker in ("You have already applied", "Application already submitted"):
            if hasattr(self.session, "has") and self.session.has(f"text={marker}"):
                raise DuplicateApplication(f"workday: {marker}")
