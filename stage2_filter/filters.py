"""Hard filters and scam detection for job listings."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def is_expired(job: dict[str, Any], max_age_days: int = 30) -> bool:
    posted_at = job.get("posted_at")
    if not posted_at:
        return False
    try:
        # Simple ISO date parse
        posted_date = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - posted_date).days
        return age > max_age_days
    except Exception:
        return False


def is_excluded(job: dict[str, Any], profile: dict[str, Any]) -> bool:
    exclusions = profile.get("exclusions", {})
    bad_companies = {c.strip().lower() for c in exclusions.get("companies", [])}
    bad_keywords = [k.strip().lower() for k in exclusions.get("keywords", [])]

    # Check company
    comp = job.get("company", "").strip().lower()
    if comp in bad_companies or any(b in comp for b in bad_companies if len(b) > 3):
        return True

    # Check title and content
    blob = f"{job.get('title', '')} {job.get('description', '')}".lower()
    if any(k in blob for k in bad_keywords):
        return True

    return False


def is_scam(job: dict[str, Any], scam_heuristics: list[str] | None = None) -> bool:
    heuristics = scam_heuristics or [
        "wire transfer",
        "pay for training",
        "send money",
        "purchase equipment upfront",
        "crypto payment only",
    ]
    blob = f"{job.get('title', '')} {job.get('description', '')}".lower()
    return any(h in blob for h in heuristics)


def salary_ok(job: dict[str, Any], profile: dict[str, Any]) -> bool:
    min_profile_salary = profile.get("min_salary")
    if not min_profile_salary:
        return True
    job_salary_max = job.get("salary_max")
    job_salary_min = job.get("salary_min")

    # If job specifies a salary max and it's below our minimum, filter it out
    if job_salary_max and job_salary_max < min_profile_salary:
        return False
    if job_salary_min and job_salary_min < (min_profile_salary * 0.7):
        return False
    return True
