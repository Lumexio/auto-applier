"""Pre-flight checks before generating scraper code."""

from __future__ import annotations

import re
import urllib.parse
import urllib.request
import urllib.robotparser
from typing import Any


class PreflightError(Exception):
    """Base class for preflight check failures."""


class BlocklistedDomainError(PreflightError):
    pass


class RobotsDisallowedError(PreflightError):
    pass


class AntiBotDetectedError(PreflightError):
    pass


class AuthRequiredError(PreflightError):
    pass


def extract_domain(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def check_blocklist(url: str, blocklist: list[str], allowed_domains: list[str] | None = None) -> None:
    domain = extract_domain(url)
    allowed = set(allowed_domains or [])
    if domain in allowed:
        return
    for blocked in blocklist:
        if blocked.lower() in domain:
            raise BlocklistedDomainError(f"Domain '{domain}' is on ToS blocklist; automation not permitted.")


def check_robots(url: str, user_agent: str = "*") -> bool:
    parsed = urllib.parse.urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = urllib.robotparser.RobotFileParser()
    try:
        req = urllib.request.Request(robots_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            lines = [line.decode("utf-8", errors="ignore") for line in resp.readlines()]
            rp.parse(lines)
            return rp.can_fetch(user_agent, url)
    except Exception:
        # If robots.txt cannot be reached, assume allowed
        return True


def detect_antibot(html: str, headers: dict[str, str] | None = None) -> bool:
    h = html.lower()
    signatures = [
        "cf-challenge",
        "challenges.cloudflare.com",
        "turnstile",
        "hcaptcha",
        "g-recaptcha",
        "datadome",
        "perimeterx",
        "just a moment...",
    ]
    if any(sig in h for sig in signatures):
        return True
    if headers:
        for k, v in headers.items():
            if "cf-mitigated" in k.lower() or "challenge" in v.lower():
                return True
    return False


def needs_js_rendering(html: str) -> bool:
    h = html.lower()
    has_spa_shell = (
        '<div id="root">' in h
        or '<div id="app">' in h
        or '<app-root>' in h
    )
    has_jobs = "job" in h or "posting" in h or "career" in h
    has_scripts = "<script" in h and ("bundle" in h or "chunk" in h or "main." in h)
    return has_spa_shell or (not has_jobs and has_scripts)


def detect_locale(html: str) -> str:
    m = re.search(r"<html[^>]*\blang=[\"']?([a-zA-Z_-]+)", html, re.IGNORECASE)
    return m.group(1).lower() if m else "en"


def preflight(url: str, config: dict[str, Any], html: str = "", headers: dict[str, str] | None = None) -> dict[str, Any]:
    stage1_cfg = config.get("stage1", {})
    blocklist = stage1_cfg.get("blocklist", [])
    allowed = config.get("allowed_domains", [])

    # 1. Blocklist
    check_blocklist(url, blocklist, allowed)

    # 2. Robots.txt
    if stage1_cfg.get("robots_respect", True):
        if not check_robots(url):
            raise RobotsDisallowedError(f"robots.txt disallows crawling '{url}'")

    # 3. Anti-bot & CAPTCHA detection
    if html and detect_antibot(html, headers):
        raise AntiBotDetectedError("Anti-bot protection detected; refusing to bypass.")

    # 4. JS Rendering
    use_browser = needs_js_rendering(html) if html else False
    locale = detect_locale(html) if html else "en"

    return {
        "url": url,
        "domain": extract_domain(url),
        "use_browser": use_browser,
        "locale": locale,
    }
