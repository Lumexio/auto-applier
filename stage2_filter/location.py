"""Drop postings you could not take up, before any model spends time on them.

Two questions, both answered from ``facts.yaml`` (country, city, authorized countries, willingness to relocate) with
``profile.json`` as the fallback:

* the posting's *location* ("Remote - Spain", "Austin, TX (Hybrid)", "Home based - Americas"): can it be done from where you live?
* the posting's *text*: does it say outright that you must live somewhere you do not ("must reside in the United States")?

The rule is "keep unless sure". An unrecognised location, a bare "Remote", or a requirement naming a place this code does not know
all keep the posting for the fit model to judge. What is dropped is counted in ``removed_location`` and a few examples are logged,
so a wrong drop is easy to notice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from facts import get_path, merged_view

from .places import WORLD, Place, country_code, fold, places_in

DEFAULT_LOCATION_CFG: dict[str, Any] = {
    "enabled": False,              # off unless the config turns it on: dropping postings is never a silent default
    "remote_authorized": "keep",   # "Remote - US" when you may work there but live elsewhere: "keep" | "drop"
    "check_description": True,     # drop a posting whose text says you must live somewhere you do not
    "always_keep": [],             # words in a location that keep a posting whatever else it says ("lisbon")
}

_REMOTE = re.compile(r"(?<![a-z])(?:remote|remoto|distributed|work from home|wfh|home based|home office|telecommut\w*|teletrabajo|anywhere)(?![a-z])")
_ON_SITE = re.compile(r"(?<![a-z])(?:on site|onsite|in office|in person|hybrid|hibrido|presencial|office based)(?![a-z])")

# "must reside in", "needs to be located in", "has to live in", "required to be based in", "must be a resident of" ...
_OBLIGATION = re.compile(
    r"\b(?:must|need(?:s)? to|required to|is required to|are required to|have to|has to|should|will need to)\s+"
    r"(?:currently\s+|physically\s+|legally\s+)*(?:be\s+)?(?:a\s+|an\s+)?(?:permanent\s+|legal\s+)?"
    r"(?:resident|living|live|reside|residing|located|based|present)\s*(?:in|within|of)\b",
    re.IGNORECASE,
)
# "open only to candidates located in", "restricted to applicants based in", "limited to residents of"
_RESTRICTION = re.compile(
    r"\b(?:open|available|restricted|limited)\s+(?:only\s+)?to\s+(?:[a-z]+\s+){0,3}?(?:candidates|applicants|residents|individuals|employees)\s+"
    r"(?:(?:who|that)\s+(?:are\s+)?)?(?:currently\s+)?(?:located|based|living|residing|resident)?\s*(?:in|within|of)\b",
    re.IGNORECASE,
)
_RESIDENTS_ONLY = re.compile(r"\bresidents?\s+only\b", re.IGNORECASE)         # "US residents only": the place is what comes just before
_NEGATION = re.compile(r"\b(?:not|no|never|without|neither|nor|\w*n't)\b[^.]{0,25}$", re.IGNORECASE)
_SENTENCE_END = re.compile(r"[\n;]|(?<!\b[A-Z])\.\s+(?=[A-Z])")          # "the US. Next" ends a sentence; "U.S. Next" is read as an abbreviation
_WINDOW = 140


@dataclass(frozen=True)
class Candidate:
    country: str                       # ISO code of where you live
    authorized: frozenset[str]         # countries you may work in (always includes ``country``)
    city: str                          # folded; "" when unknown
    relocate: bool                     # facts say willing_to_relocate: true (unstated is not a yes)


def candidate_from(facts: dict[str, Any] | None, profile: dict[str, Any] | None) -> Candidate | None:
    """Where the applicant lives and may work, or None when that cannot be told (the filter then does nothing)."""
    profile = profile or {}
    view = merged_view(facts or {}, profile)
    authorized_raw = get_path(view, "work_authorization.authorized_countries") or profile.get("work_authorization") or []
    authorized = {c for c in (country_code(a) for a in (authorized_raw if isinstance(authorized_raw, list) else [])) if c}
    profile_place = [p.strip() for p in str(profile.get("location") or "").split(",") if p.strip()]
    country = country_code(get_path(view, "location.country")) or (country_code(profile_place[-1]) if len(profile_place) > 1 else None)
    if not country and len(authorized) == 1:
        country = next(iter(authorized))
    if not country:
        return None
    city = fold(get_path(view, "location.city") or (profile_place[0] if len(profile_place) > 1 else ""))
    return Candidate(country, frozenset(authorized | {country}), city, get_path(view, "location.willing_to_relocate") is True)


def _reaches(codes: frozenset[str] | set[str], cand: Candidate) -> bool:
    return WORLD in codes or cand.country in codes


def _names(codes: frozenset[str] | set[str]) -> str:
    return ", ".join(sorted(c for c in codes if c != WORLD)[:4])


def _lives_in(text: str, cand: Candidate) -> bool:
    """Is one of the cities the posting names the one you live in?"""
    if not cand.city:
        return True
    if re.search(rf"(?<![a-z0-9]){re.escape(cand.city)}(?![a-z0-9])", text):
        return True
    generic = {"ciudad", "city", "de", "del", "la", "el", "san", "santa", "nuevo"}
    parts = [w for w in cand.city.split() if len(w) >= 5 and w not in generic and not places_in(w).countries]
    return any(re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", text) for w in parts)


def _alternative(alt: str, job_remote: bool, cand: Candidate, cfg: dict[str, Any]) -> str | None:
    """None: you could take this one (or it says nothing we can tell). Otherwise why you could not."""
    place = places_in(alt)
    folded = fold(alt)
    if cand.city and re.search(rf"(?<![a-z0-9]){re.escape(cand.city)}(?![a-z0-9])", folded):
        return None                                            # it names the city you live in, known to the gazetteer or not
    explicit_on_site = bool(_ON_SITE.search(folded))
    remote = bool(_REMOTE.search(folded)) or (job_remote and not explicit_on_site)
    said = f"'{alt.strip()[:60]}'"
    if not place.codes:
        # "Hybrid" or "In-Office" with no place named: you would have to be there, and nothing says where that is
        return None if (remote or not explicit_on_site or cand.relocate) else f"{said} is on-site or hybrid and names no place you could attend"
    on_site = not remote and (explicit_on_site or place.granular)

    if _reaches(place.codes, cand):
        if on_site and explicit_on_site and cand.country in place.codes and place.cities and not cand.relocate and not _lives_in(folded, cand):
            return f"{said} is on-site or hybrid, not where you live"
        return None
    if place.codes & cand.authorized:                          # a country you may work in but do not live in (the US)
        if on_site:
            return None if cand.relocate else f"{said} is on-site; you live in {cand.country} and have not said you would relocate"
        if cfg["remote_authorized"] == "drop":
            return f"{said} is remote but tied to a country you do not live in"
        return None
    return f"{said} is not open to {cand.country}"


def _by_location(job: dict[str, Any], cand: Candidate, cfg: dict[str, Any]) -> str | None:
    location = str(job.get("location") or "")
    folded = fold(location)
    if not folded:
        return None
    for word in cfg["always_keep"]:
        if fold(word) and re.search(rf"(?<![a-z0-9]){re.escape(fold(word))}(?![a-z0-9])", folded):
            return None
    reasons: list[str] = []
    for alt in (a for a in re.split(r"[;|\n]+", location) if a.strip()):
        why = _alternative(alt, bool(job.get("remote")), cand, cfg)
        if why is None:
            return None                                        # one workable alternative is enough
        reasons.append(why)
    return reasons[0] if reasons else None


def _requirements(text: str) -> list[str]:
    """The stretches of text that follow "must reside in ..." style wording (and are not negated)."""
    out: list[str] = []
    for pattern in (_OBLIGATION, _RESTRICTION):
        for m in pattern.finditer(text):
            if _NEGATION.search(text[max(0, m.start() - 40): m.start()]):
                continue
            tail = text[m.end(): m.end() + _WINDOW]
            end = _SENTENCE_END.search(tail)
            out.append(tail[: end.start()] if end else tail)
    for m in _RESIDENTS_ONLY.finditer(text):
        before = text[max(0, m.start() - 60): m.start()]
        if not _NEGATION.search(before):
            out.append(_SENTENCE_END.split(before)[-1][-40:])
    return out


def _by_description(job: dict[str, Any], cand: Candidate) -> str | None:
    text = str(job.get("description") or "")
    if len(text) < 40:
        return None
    wanted: list[Place] = [p for p in (places_in(window) for window in _requirements(text)) if p.codes]
    if not wanted or any(_reaches(p.codes, cand) for p in wanted):
        return None
    if cand.country in places_in(text).seen:                  # the text names your country (or a city of it) somewhere: let the model read it
        return None
    return f"the posting says you must live in {_names(set().union(*(p.codes for p in wanted)))}; you live in {cand.country}"


def location_verdict(job: dict[str, Any], cand: Candidate | None, cfg: dict[str, Any] | None = None) -> str | None:
    """Why this posting is not one you could take up, or None to keep it. Never raises: a bug here must not hide jobs or stop a cycle."""
    if cand is None:
        return None
    settings = {**DEFAULT_LOCATION_CFG, **(cfg or {})}
    try:
        why = _by_location(job, cand, settings)
        if why is None and settings["check_description"]:
            why = _by_description(job, cand)
        return why
    except Exception:
        return None
