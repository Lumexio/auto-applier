"""Make every ingestion source look the same to the stage 2 filters."""

from __future__ import annotations

from typing import Any

from job_text import clean_title, html_to_text, looks_like_markup


def normalize_job(job: dict[str, Any]) -> dict[str, Any]:
    """Clean a raw posting in place and return it.

    * ``description`` is the one canonical text field. Scrapers historically emitted ``content`` (often raw
      or entity-escaped HTML), so stages 2 and 3 never saw the text; it is folded in here for any source.
    * Titles and company names are unescaped (``&amp;``) and stripped of card metadata.
    """
    job["title"] = clean_title(job.get("title"))
    company = " ".join(str(job.get("company") or "").split())
    job["company"] = company

    description = job.get("description") or job.get("content") or ""
    if looks_like_markup(description):
        description = html_to_text(description)
    job["description"] = description.strip()

    if "remote" not in job:
        job["remote"] = "remote" in f"{job.get('location', '')} {job['title']}".lower()
    return job
