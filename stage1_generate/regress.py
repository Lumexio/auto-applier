"""Regression testing engine for generated scrapers."""

from __future__ import annotations

from typing import Any
from .generate import generate_scraper
from .sandbox import SandboxResult, run_in_sandbox


class RegressionError(Exception):
    pass


def run_fixture_regression(scraper_code: str, fixture_html: str, expected_min_jobs: int = 1) -> SandboxResult:
    result = run_in_sandbox(scraper_code, html_fixture=fixture_html)
    if result.errors:
        raise RegressionError(f"Scraper execution failed on fixture: {result.errors}")
    if result.jobs_found < expected_min_jobs:
        raise RegressionError(
            f"Regression failed: Scraper found {result.jobs_found} jobs, expected at least {expected_min_jobs}."
        )
    return result


def ensure_working_scraper(
    html: str,
    fixture_html: str,
    config: dict[str, Any] | None = None,
    max_attempts: int = 3,
) -> str:
    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        code = generate_scraper(html, config=config)
        try:
            run_fixture_regression(code, fixture_html)
            return code
        except RegressionError as e:
            last_err = e

    raise RegressionError(f"Failed to generate a working scraper after {max_attempts} attempts: {last_err}")
