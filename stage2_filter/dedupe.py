"""De-duplication and normalization for job listings."""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

# Stand-ins scrapers use when a card does not name the employer. Two different employers can both be
# "LinkedIn Portal", so a shared title must not make them duplicates of each other.
UNKNOWN_COMPANIES = {"linkedin portal", "handshake network", "unknown company", "unknown", ""}


def normalize_company(company: str) -> str:
    cleaned = company.lower().strip()
    cleaned = re.sub(r"\b(inc|llc|corp|ltd|gmbh|s\.a\.|co)\b\.?", "", cleaned)
    return re.sub(r"[^\w\s]", "", cleaned).strip()


def normalize_title(title: str) -> str:
    cleaned = title.lower().strip()
    return re.sub(r"\s+", " ", cleaned)


def canonicalize_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    # Strip tracking query params
    filtered_queries = []
    if parsed.query:
        for k, v in urllib.parse.parse_qsl(parsed.query):
            if not k.startswith("utm_") and k not in ("ref", "source", "gh_jid", "lever-origin"):
                filtered_queries.append((k, v))
    query_str = urllib.parse.urlencode(filtered_queries)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", query_str, ""))


def dedupe(jobs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    seen_keys: set[tuple[str, str, str]] = set()
    seen_urls: set[str] = set()

    kept: list[dict[str, Any]] = []
    duplicates_count = 0

    for job in jobs:
        comp_norm = normalize_company(job.get("company", ""))
        title_norm = normalize_title(job.get("title", ""))
        loc_norm = normalize_title(job.get("location") or "")
        url_canon = canonicalize_url(job.get("apply_url", ""))

        # Same role in two cities is two postings. Without a known employer only the URL can identify a duplicate.
        key = (comp_norm, title_norm, loc_norm) if comp_norm not in UNKNOWN_COMPANIES else None
        if (key is not None and key in seen_keys) or url_canon in seen_urls:
            duplicates_count += 1
            continue

        if key is not None:
            seen_keys.add(key)
        seen_urls.add(url_canon)
        job["company_norm"] = comp_norm
        job["apply_url_canon"] = url_canon
        kept.append(job)

    return kept, duplicates_count
