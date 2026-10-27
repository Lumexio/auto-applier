"""Submission confirmation and verification monitor."""

from __future__ import annotations

import re
import time
import urllib.parse
from typing import Any

# A confirmation page announces itself with a whole path segment such as /confirmation or /thank-you.
# Matching bare substrings of the URL (the old behaviour) "confirmed" any posting on a board like
# `appliedintuition`, `successfactors-partners` or `thankful` before anything was submitted.
_SUCCESS_SEGMENT = re.compile(
    r"(?:^|[/_.\-])"
    r"(?:thank[-_ ]?you|thanks|confirmation|confirmed|success|submitted|application[-_]?(?:received|submitted|complete(?:d)?))"
    r"(?:$|[/_.\-])",
    re.IGNORECASE,
)

SUCCESS_SELECTORS = (
    "#application_confirmation",
    "[data-automation-id='confirmationPage']",
    ".application-confirmation",
    "h1:has-text('Application Submitted')",
    "h1:has-text('Thank You')",
)


def looks_submitted(url: str) -> bool:
    return bool(_SUCCESS_SEGMENT.search(urllib.parse.urlparse(url).path))


def wait_for_confirmation(session: Any, timeout_seconds: int = 60, baseline_url: str | None = None) -> bool:
    """True once the page shows the application went through.

    ``baseline_url`` is where the browser was just before the submit click. A confirmation-looking URL only
    counts if the browser has moved off it, so a posting whose own address happens to look like a
    confirmation page (e.g. a board called ``success``) is never mistaken for a submitted application.
    """
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        current_url = session.current_url() if hasattr(session, "current_url") else session.url
        if looks_submitted(current_url) and (baseline_url is None or current_url != baseline_url):
            return True

        if hasattr(session, "has"):
            for sel in SUCCESS_SELECTORS:
                try:
                    if session.has(sel):
                        return True
                except Exception:
                    pass

        time.sleep(1)
    return False
