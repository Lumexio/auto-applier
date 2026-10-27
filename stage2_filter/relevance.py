"""Relevance gate and scoring for stage 2.

The gate is deliberately cheap and explainable (no model needed): it removes roles that are plainly not the
applicant's field or seniority, using sensible defaults that ``profile.json`` can override with
``target_titles``, ``title_exclusions`` and ``exclude_junior_roles``.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

# Title must contain at least one of these (word match) unless the profile sets its own `target_titles`.
DEFAULT_TARGET_TITLE_KEYWORDS = (
    "software", "developer", "dev", "engineer", "programmer", "architect", "backend", "back end",
    "full stack", "fullstack", "frontend", "front end", "devops", "sre", "platform",
)
# Plainly different fields; override with `title_exclusions` in the profile.
DEFAULT_TITLE_EXCLUSIONS = (
    "driver", "cashier", "warehouse", "nurse", "barista", "bartender", "receptionist", "technician",
    "mechanic", "electrician", "plumber", "sales", "recruiter", "account executive", "accountant",
    "paralegal", "teacher", "tutor",
)
JUNIOR_TERMS = (
    "intern", "internship", "junior", "jr", "entry level", "trainee", "apprentice", "graduate",
    "early career", "new grad",
)
SENIOR_LEVELS = {"senior", "sr", "staff", "lead", "principal", "architect"}

# Spellings of the same technology that a posting may use instead of the profile's.
SKILL_ALIASES = {
    "node.js": ("nodejs", "node js"),
    "nest.js": ("nestjs", "nest js"),
    "c/c++": ("c++",),
    "postgresql": ("postgres",),
    "javascript": ("ecmascript",),
}


def _fold(text: str) -> str:
    return re.sub(r"[\s\-_/]+", " ", text.casefold()).strip()


@lru_cache(maxsize=512)
def _phrase_re(term: str) -> re.Pattern[str]:
    return re.compile(r"(?<![a-z0-9])" + re.escape(_fold(term)) + r"(?![a-z0-9])")


def _contains_phrase(text: str, term: str) -> bool:
    return bool(_phrase_re(term).search(_fold(text)))


@lru_cache(maxsize=512)
def _skill_re(skill: str) -> re.Pattern[str]:
    forms = [skill.casefold(), *SKILL_ALIASES.get(skill.casefold(), ())]
    alternation = "|".join(re.escape(f) for f in forms)
    # '+' and '#' are part of names like C++ / C#, so they count as word characters here.
    return re.compile(r"(?<![a-z0-9+#])(?:" + alternation + r")(?![a-z0-9+#])")


def skill_in(text: str, skill: str) -> bool:
    return bool(_skill_re(skill.strip()).search(text.casefold()))


def title_relevance(job: dict[str, Any], profile: dict[str, Any]) -> tuple[bool, str]:
    """(keep, reason-if-dropped) based on the title alone."""
    title = job.get("title", "")

    for term in profile.get("title_exclusions", DEFAULT_TITLE_EXCLUSIONS):
        if _contains_phrase(title, term):
            return False, f"title excluded by '{term}'"

    seniority = str(profile.get("seniority", "")).lower()
    if profile.get("exclude_junior_roles", seniority in SENIOR_LEVELS):
        for term in JUNIOR_TERMS:
            if _contains_phrase(title, term):
                return False, f"below seniority ('{term}')"

    targets = profile.get("target_titles", DEFAULT_TARGET_TITLE_KEYWORDS)
    if targets and not any(_contains_phrase(title, t) for t in targets):
        return False, "title is not one of the target roles"
    return True, ""


def score_with_reason(job: dict[str, Any], profile: dict[str, Any]) -> tuple[float, str]:
    """0..1 fit score plus a human-readable explanation (stored in jobs.reason)."""
    skills = [s.strip() for s in profile.get("skills", []) if s.strip()]
    if not skills:
        return 0.5, "profile lists no skills"

    title = job.get("title", "")
    blob = f"{title} {job.get('description', '')}"
    matched = [s for s in skills if skill_in(blob, s)]

    # Base skill score: up to 0.8
    skill_score = min(0.8, len(matched) / max(5, len(skills) * 0.3))

    # Title seniority boost: up to 0.2
    seniority = str(profile.get("seniority", "senior")).lower()
    lowered = title.lower()
    bonus = 0.2 if seniority in lowered or "lead" in lowered or "staff" in lowered else 0.0

    reason = f"{len(matched)}/{len(skills)} skills"
    if matched:
        reason += f" ({', '.join(matched[:6])}{', ...' if len(matched) > 6 else ''})"
    if bonus:
        reason += "; seniority match"
    return round(min(1.0, skill_score + bonus), 2), reason
