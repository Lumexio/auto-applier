"""Job listing ingestion and filtering."""

from __future__ import annotations

import hashlib
import html as html_lib
import json
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from job_text import (
    clean_title,
    dedupe_consecutive,
    html_to_text,
    parse_posted_age,
    parse_salary,
    text_lines,
)
from store import JobStore


# ponytail: stdlib urllib.request over httpx; zero extra network dependencies
def _fetch_json(url: str, timeout: int = 15) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# Stand-ins used when a card does not say which company posted it. outreach.py recognises these.
UNKNOWN_LINKEDIN_COMPANY = "LinkedIn Portal"
UNKNOWN_HANDSHAKE_COMPANY = "Handshake Network"


def _job(**fields: Any) -> dict[str, Any]:
    """Uniform job record. ``description`` is the canonical text; ``content`` mirrors it for the legacy applier."""
    desc = fields.get("description") or ""
    job: dict[str, Any] = {
        "location": "",
        "posted_at": None,
        "remote": False,
        **fields,
        "description": desc,
        "content": desc,
    }
    if job.get("salary_min") is None:
        job.pop("salary_min", None)
        job.pop("salary_max", None)
    return job


def _salary_fields(text: str) -> dict[str, Any]:
    pay = parse_salary(text)
    return {"salary_min": pay[0], "salary_max": pay[1]} if pay else {}


# ponytail: direct public ATS API ingestion; runtime LLM scraper generation only if target has no structured API
def fetch_greenhouse_jobs(board_token: str) -> list[dict[str, Any]]:
    # content=true makes the list endpoint include each posting's description (without it, none is returned)
    url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true"
    data = _fetch_json(url, timeout=45)
    jobs = []
    for j in data.get("jobs", []):
        location = (j.get("location") or {}).get("name", "")
        jobs.append(
            _job(
                id=str(j["id"]),
                title=j["title"],
                company=j.get("company_name") or board_token,
                apply_url=j["absolute_url"],
                location=location,
                description=html_to_text(j.get("content", "")),
                posted_at=j.get("first_published") or j.get("updated_at"),
                remote="remote" in location.lower(),
                source="greenhouse",
            )
        )
    return jobs


def _epoch_ms_to_iso(value: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def fetch_lever_jobs(company: str) -> list[dict[str, Any]]:
    url = f"https://api.lever.co/v0/postings/{company}"
    data = _fetch_json(url)
    jobs = []
    for j in data:
        categories = j.get("categories") or {}
        sections = [j.get("descriptionPlain", "")]
        sections += [f"{s.get('text', '')}\n{html_to_text(s.get('content', ''))}" for s in j.get("lists") or []]
        sections.append(j.get("additionalPlain", ""))
        location = categories.get("location", "") or ""
        jobs.append(
            _job(
                id=str(j["id"]),
                title=j["text"],
                company=company,
                apply_url=j["applyUrl"],
                location=location,
                description="\n\n".join(s for s in sections if s).strip(),
                posted_at=_epoch_ms_to_iso(j.get("createdAt")),
                remote=(j.get("workplaceType") == "remote") or "remote" in location.lower(),
                source="lever",
            )
        )
    return jobs


# Link texts that are site chrome, never postings.
NAV_TITLES = {
    "jobs", "preferences", "show all", "all jobs", "saved jobs", "my jobs", "alerts", "more", "home", "careers",
    "apply", "learn more", "view all", "see all jobs", "search", "next", "previous", "sign in", "join now",
    "help", "settings", "open jobs", "job search", "see more",
}

# Badges and decoration that LinkedIn mixes into a card's text nodes.
_BADGE = re.compile(
    r"\$|\b(?:easy apply|top applicant|promoted|actively (?:reviewing|recruiting)|posted|be an early applicant|"
    r"verified job|ago|sign in|dismiss|see more|privacy|terms|agree|applicants?|viewed|reposted)\b",
    re.I,
)
_DECOR_SUFFIX = re.compile(r"\s*\((?:verified job|promoted)\)\s*$", re.I)
_HANDSHAKE_META = re.compile(r"\$|[•·∙]|\bago\b|\bpromoted\b|\bapply today\b", re.I)
_PIPE_SPLIT = re.compile(r"\s+[•·∙]\s+")


def _company_and_location(rest: list[str], unknown: str) -> tuple[str, str]:
    """First remaining line is the company ('Acme' or 'Acme • Austin, TX'); the next one is the location."""
    if not rest:
        return unknown, ""
    parts = [p.strip() for p in _PIPE_SPLIT.split(rest[0]) if p.strip()]
    company = parts[0] if parts else unknown
    location = parts[1] if len(parts) > 1 else (rest[1] if len(rest) > 1 else "")
    return company, location


def parse_jobs_from_html(html: str, base_url: str = "") -> list[dict[str, Any]]:
    """Extract job postings from raw HTML using JSON-LD, platform job cards, and anchor tags."""
    import urllib.parse

    jobs: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    host = urllib.parse.urlparse(base_url).netloc.lower()
    on_known_platform = any(h in host for h in ("linkedin.com", "joinhandshake.com"))

    # 1. JSON-LD structured data check (Schema.org JobPosting)
    json_ld_matches = re.findall(
        r'<script[^>]*type=[\'"]application/ld\+json[\'"][^>]*>(.*?)</script>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    for raw_json in json_ld_matches:
        try:
            data = json.loads(raw_json.strip())
            items = data if isinstance(data, list) else [data]
            for item in items:
                if isinstance(item, dict) and item.get("@type") == "JobPosting":
                    apply_url = item.get("url") or base_url
                    if apply_url and apply_url not in seen_urls:
                        seen_urls.add(apply_url)
                        company_info = item.get("hiringOrganization")
                        company_name = (
                            company_info.get("name", "")
                            if isinstance(company_info, dict)
                            else str(company_info or "")
                        )
                        loc_info = item.get("jobLocation")
                        loc_str = ""
                        if isinstance(loc_info, dict):
                            addr = loc_info.get("address", {})
                            loc_str = (
                                addr.get("addressLocality", "")
                                if isinstance(addr, dict)
                                else str(addr)
                            )
                        extra: dict[str, Any] = {}
                        base_salary = item.get("baseSalary")
                        if isinstance(base_salary, dict) and isinstance(base_salary.get("value"), dict):
                            v = base_salary["value"]
                            factor = {"HOUR": 2080, "DAY": 260, "WEEK": 52, "MONTH": 12, "YEAR": 1}.get(str(v.get("unitText", "")).upper())
                            lo, hi = v.get("minValue", v.get("value")), v.get("maxValue", v.get("value"))
                            if factor and isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
                                extra = {"salary_min": int(lo * factor), "salary_max": int(hi * factor)}
                        jobs.append(
                            _job(
                                id=str(
                                    item.get("identifier", {}).get("value")
                                    if isinstance(item.get("identifier"), dict)
                                    else item.get("identifier", len(jobs) + 1)
                                ),
                                title=item.get("title", "Untitled Role"),
                                company=company_name or "Unknown Company",
                                apply_url=apply_url,
                                location=loc_str,
                                description=html_to_text(item.get("description", "")),
                                posted_at=item.get("datePosted"),
                                remote=item.get("jobLocationType") == "TELECOMMUTE" or "remote" in loc_str.lower(),
                                source="json_ld",
                                **extra,
                            )
                        )
        except Exception:
            pass

    # 2. LinkedIn job cards (handles /jobs/view/\d+ and currentJobId=\d+)
    li_matches = list(
        re.finditer(
            r'<a[^>]*href=[\'"]([^\'"]*(?:/jobs/view/|currentJobId=)[^\'"]*)[\'"][^>]*>(.*?)</a>',
            html,
            re.DOTALL | re.IGNORECASE,
        )
    )
    for m in li_matches:
        raw_href = html_lib.unescape(m.group(1))
        id_match = re.search(r'(?:/jobs/view/|currentJobId=)(\d+)', raw_href)
        if not id_match:
            continue
        job_id = id_match.group(1)
        clean_url = f"https://www.linkedin.com/jobs/view/{job_id}"
        if clean_url in seen_urls:
            continue

        lines = dedupe_consecutive([_DECOR_SUFFIX.sub("", ln) for ln in text_lines(m.group(2))])
        if len([ln for ln in lines if not _BADGE.search(ln)]) < 2:
            # Public LinkedIn markup keeps company and location in the element after the link
            # (not inside it), so also read the text up to the next link.
            tail = html[m.end(): m.end() + 800]
            next_link = re.search(r"<a\b", tail, re.IGNORECASE)
            if next_link:
                tail = tail[: next_link.start()]
            lines = dedupe_consecutive(lines + [_DECOR_SUFFIX.sub("", ln) for ln in text_lines(tail)])

        content_lines = [ln for ln in lines if not _BADGE.search(ln)]
        title = clean_title(content_lines[0]) if content_lines else "LinkedIn Role"
        company, location = _company_and_location(content_lines[1:], UNKNOWN_LINKEDIN_COMPANY)
        if company.casefold() == title.casefold():
            company = UNKNOWN_LINKEDIN_COMPANY
        if title.lower() in NAV_TITLES:
            continue

        card_text = " ".join(lines)
        seen_urls.add(clean_url)
        jobs.append(
            _job(
                id=job_id,
                title=title,
                company=company,
                apply_url=clean_url,
                location=location,
                posted_at=parse_posted_age(card_text),
                remote="remote" in f"{location} {card_text}".lower(),
                source="linkedin",
                **_salary_fields(card_text),
            )
        )

    # 3. Handshake job cards (e.g. /jobs/12345)
    handshake_matches = re.finditer(
        r'<a[^>]*href=[\'"]([^\'"]*(?:joinhandshake\.com)?/jobs/\d+[^\'"]*)[\'"][^>]*>(.*?)</a>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    for m in handshake_matches:
        link = html_lib.unescape(m.group(1))
        if link.startswith("/"):
            link = f"https://app.joinhandshake.com{link}"
        clean_url = link.split("?")[0].rstrip("/")
        if clean_url in seen_urls:
            continue

        lines = dedupe_consecutive(text_lines(m.group(2)))
        # Card text reads: employer, [industry], title, then pay / type / location / age metadata.
        head: list[str] = []
        for ln in lines:
            if _HANDSHAKE_META.search(ln):
                break
            head.append(ln)
        head = head or lines[:1]
        if not head:
            continue
        title = clean_title(head[-1]) if len(head) >= 2 else clean_title(head[0])
        company = head[0] if len(head) >= 2 else UNKNOWN_HANDSHAKE_COMPANY
        if len(title) <= 2 or title.lower() in NAV_TITLES:
            continue

        card_text = " ".join(lines)
        seen_urls.add(clean_url)
        jobs.append(
            _job(
                id=clean_url.split("/")[-1],
                title=title,
                company=company,
                apply_url=clean_url,
                posted_at=parse_posted_age(card_text),
                remote="remote" in card_text.lower(),
                source="handshake",
                **_salary_fields(card_text),
            )
        )

    # 4. Generic career links, only for ordinary company sites. On LinkedIn/Handshake the remaining links
    #    are navigation ("Jobs", "Preferences", "Show all"), never postings.
    if not jobs and not on_known_platform:
        generic_matches = re.finditer(
            r'href=[\'"]([^\'"]*/(?:jobs|careers|positions)/[^/\'"?#][^\'"]*)[\'"][^>]*>(.*?)</a>',
            html,
            re.DOTALL | re.IGNORECASE,
        )
        for m in generic_matches:
            if len(jobs) >= 25:
                break
            link = html_lib.unescape(m.group(1))
            abs_url = urllib.parse.urljoin(base_url, link).split("?")[0].rstrip("/")
            title = clean_title(" ".join(text_lines(m.group(2))))
            if not (3 < len(title) <= 160) or title.lower() in NAV_TITLES:
                continue
            if any(skip in title.lower() for skip in ("cookie", "privacy", "term", "about", "contact", "sign in", "show all", "preferences")):
                continue
            if abs_url in seen_urls:
                continue
            seen_urls.add(abs_url)
            jobs.append(
                _job(
                    # Derived from the URL so ids stay stable between runs and never collide across sites.
                    id=hashlib.sha1(abs_url.encode("utf-8")).hexdigest()[:12],
                    title=title,
                    company=urllib.parse.urlparse(base_url).netloc or "Company",
                    apply_url=abs_url,
                    source="browser_extracted",
                )
            )

    return jobs


def fetch_browser_jobs(
    url: str,
    user_data_dir: str = "./.browser_profile",
    browser_type: str = "firefox",
    max_items: int = 25,
) -> list[dict[str, Any]]:
    """Fetch and scrape jobs from any URL using the pre-authenticated persistent browser session."""
    import urllib.request

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[BrowserScraper] Playwright not available. Falling back to HTTP fetch.")
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
            return parse_jobs_from_html(html, base_url=url)[:max_items]

    profile_path = Path(user_data_dir).resolve() / browser_type.lower()
    profile_path.mkdir(parents=True, exist_ok=True)
    try:
        from import_profile import clean_stale_firefox_lock
        clean_stale_firefox_lock(profile_path)
    except Exception:
        pass

    print(f"[BrowserScraper] Loading {url} via persistent {browser_type.capitalize()} profile...")
    with sync_playwright() as p:
        launcher = getattr(p, browser_type.lower(), p.firefox)
        try:
            context = launcher.launch_persistent_context(
                user_data_dir=str(profile_path),
                headless=True,
            )
        except Exception as launch_err:
            time.sleep(1)
            try:
                from import_profile import clean_stale_firefox_lock
                clean_stale_firefox_lock(profile_path)
            except Exception:
                pass
            try:
                context = launcher.launch_persistent_context(
                    user_data_dir=str(profile_path),
                    headless=True,
                )
            except Exception:
                if browser_type.lower() == "firefox":
                    print(f"[BrowserScraper] Firefox profile locked or busy. Falling back to Chromium...")
                    chrom_dir = profile_path.parent / "chromium"
                    chrom_dir.mkdir(parents=True, exist_ok=True)
                    context = p.chromium.launch_persistent_context(
                        user_data_dir=str(chrom_dir),
                        headless=True,
                    )
                else:
                    raise launch_err
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
            page.wait_for_timeout(2000)
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
                page.wait_for_timeout(2000)
            except Exception:
                pass
            html = page.content()
        except Exception as e:
            print(f"[BrowserScraper] Warning loading {url}: {e}")
            html = page.content() if page else ""
        finally:
            context.close()

    jobs = parse_jobs_from_html(html, base_url=url)
    print(f"[BrowserScraper] Extracted {len(jobs)} postings from {url}.")
    return jobs[:max_items]


# ponytail: pure stdlib filtering; jsonschema validation only if receiving untrusted external profiles
def filter_jobs(
    jobs: list[dict[str, Any]], profile: dict[str, Any], store: JobStore
) -> list[dict[str, Any]]:
    exclusions = profile.get("exclusions", {})
    bad_companies = {c.strip().lower() for c in exclusions.get("companies", [])}
    bad_keywords = [k.strip().lower() for k in exclusions.get("keywords", [])]
    skills = [s.strip().lower() for s in profile.get("skills", [])]

    passed = []
    for job in jobs:
        url = job["apply_url"]
        if store.is_seen(url):
            continue
        if job["company"].strip().lower() in bad_companies:
            continue

        search_blob = f"{job['title']} {job.get('content', '')}".lower()
        if any(bad in search_blob for bad in bad_keywords):
            continue

        # Score by matching skills
        score = sum(1 for skill in skills if skill in search_blob)
        job["score"] = score
        passed.append(job)

    # Sort descending by score
    passed.sort(key=lambda j: j.get("score", 0), reverse=True)
    return passed


def load_profile(path: str | Path = "profile.json") -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
