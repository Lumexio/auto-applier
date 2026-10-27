"""The fact sheet: everything the application bot is allowed to say about you.

``facts.yaml`` is written and owned by the *user*. The model may only restate what is in it; a question whose
answer is not there is never guessed, the application is parked for review instead. Values left as ``null`` are
"not stated" (for example whether you need visa sponsorship) -- they are deliberately NOT pre-filled.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_facts(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        return {}
    import yaml

    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{p} must contain a YAML mapping at the top level")
    return data


def flatten(data: Any, prefix: str = "") -> dict[str, Any]:
    """{'a': {'b': 1}, 'c': [1, 2], 'd': None} -> {'a.b': 1, 'c': [1, 2]}  (null / empty leaves are 'not stated' and dropped)."""
    out: dict[str, Any] = {}
    if isinstance(data, dict):
        for k, v in data.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif data is not None and data != "" and data != []:
        out[prefix] = data
    return out


def get_path(data: dict[str, Any], dotted: str) -> Any:
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def profile_view(profile: dict[str, Any]) -> dict[str, Any]:
    """The part of profile.json that may be stated to a recruiter, under the 'profile.' namespace."""
    keys = ("name", "first_name", "last_name", "email", "phone", "location", "linkedin", "github", "portfolio", "seniority", "skills")
    return {"profile": {k: profile[k] for k in keys if profile.get(k)}}


def merged_view(facts: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    view = dict(profile_view(profile))
    view.update(facts)
    return view


def render_lines(view: dict[str, Any], skip_prefixes: tuple[str, ...] = (), max_chars: int = 1800) -> tuple[str, dict[str, Any]]:
    """('path: value' lines for the prompt, the flat map those paths came from). Truncated to ``max_chars``."""
    flat = {p: v for p, v in flatten(view).items() if not p.startswith(skip_prefixes)}
    lines: list[str] = []
    used = 0
    kept: dict[str, Any] = {}
    for path, value in flat.items():
        text = ", ".join(map(str, value)) if isinstance(value, list) else str(value)
        line = f"{path}: {text}"[:420]
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        kept[path] = value
        used += len(line) + 1
    return "\n".join(lines), kept


def _split_location(location: str) -> tuple[str | None, str | None, str | None]:
    parts = [p.strip() for p in (location or "").split("/")[0].split(",") if p.strip()]
    if len(parts) >= 3:
        return parts[0], parts[1], parts[-1]
    if len(parts) == 2:
        return parts[0], None, parts[1]
    return (parts[0], None, None) if parts else (None, None, None)


def facts_template(profile: dict[str, Any]) -> str:
    """A starter facts.yaml. Only things profile.json already states are filled; the rest is null for YOU to decide."""
    j = lambda v: json.dumps(v, ensure_ascii=False)  # noqa: E731  (JSON scalars are valid YAML)
    city, region, country = _split_location(profile.get("location", ""))
    skills = [s for s in profile.get("skills", []) if s]
    years_by_skill = "\n".join(f"    {j(s)}: null" for s in skills) or "    {}"
    return f"""# facts.yaml -- what the application bot may say about you.
#
# Rules the bot follows:
#   * It answers form questions ONLY from this file (plus your profile.json contact details).
#   * `null` means "not stated": a question that needs that fact is never guessed. The application is parked
#     as needs_review and the question is listed by `python answers.py pending`.
#   * You can also answer a question once for all future applications: `python answers.py set "<question>" "<answer>"`.
#   * Nothing here is checked against reality. Fill in only what is true.

work_authorization:
  authorized_countries: {j(profile.get("work_authorization") or [])}   # ISO codes; copied from profile.json -- VERIFY
  requires_sponsorship: null        # true / false: will you need an employer to sponsor a visa?

availability:
  notice_period: null               # e.g. "2 weeks"
  earliest_start_date: null         # e.g. "2 weeks after an offer"

compensation:
  expected_salary: {j(profile.get("min_salary"))}          # number, from profile.json min_salary
  currency: {j(profile.get("currency", "USD"))}

location:
  city: {j(city)}
  region: {j(region)}
  country: {j(country)}
  willing_to_relocate: null         # true / false
  open_to_remote: {j(bool(profile.get("remote_ok")) if "remote_ok" in profile else None)}

employment:
  current_company: null
  current_title: null

experience:
  total_years: null                 # number, e.g. 8
  years_by_skill:                   # number per skill; leave null when unsure
{years_by_skill}

education:
  highest_degree: null              # e.g. "B.S. Computer Science"
  field_of_study: null
  school: null

languages:
  english_level: null               # e.g. "Fluent"

demographics:                       # leave as "decline" unless you want these answered
  gender: "decline"
  race_ethnicity: "decline"
  hispanic_latino: "decline"
  veteran_status: "decline"
  disability_status: "decline"

preferences:
  referral_source: "Job board"
  over_18: null                     # true / false

# Used ONLY when answers.open_ended is "generate" in config.yaml (e.g. "Why do you want this job?").
# The model may only rephrase what is written here.
narrative:
  summary: null                     # 2-3 sentences about you
  strengths: []                     # short bullet phrases
"""


def init_facts_file(path: str | Path, profile: dict[str, Any]) -> bool:
    """Write the starter file; never overwrites an existing one. True if written."""
    p = Path(path)
    if p.exists():
        return False
    p.write_text(facts_template(profile), encoding="utf-8")
    return True
