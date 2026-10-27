"""LLM scraper code generator with prompt injection defenses and sanitization."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

from .sandbox import ast_policy_ok

INJECTION_PATTERNS = [
    r"ignore\s+(?:all\s+)?previous\s+instructions",
    r"system\s*:\s*you\s+are\s+now",
    r"you\s+are\s+now\s+in\s+debug\s+mode",
    r"output\s+the\s+applicant'?s?\s+credentials",
    r"disregard\s+prior\s+guidelines",
]


def strip_injections(text: str, max_chars: int = 25000) -> str:
    cleaned = text
    for pat in INJECTION_PATTERNS:
        cleaned = re.sub(pat, "[REDACTED_INJECTION_ATTEMPT]", cleaned, flags=re.IGNORECASE)
    return cleaned[:max_chars]


def build_generation_prompt(html_snippet: str, locale: str = "en") -> str:
    sanitized = strip_injections(html_snippet)
    return f"""You are an expert Python web-scraping code generator.
Task: Write a clean Python function `scrape(html: str) -> list[dict]` that parses job postings from the provided HTML.

Requirements:
- Target locale: {locale}
- Return a list of dicts with schema:
  {{
    "id": str,
    "title": str,
    "company": str,
    "apply_url": str,
    "location": str or null,
    "posted_at": str or null
  }}
- Check for JSON-LD `<script type="application/ld+json">` with "@type": "JobPosting" first.
- If no JSON-LD, parse HTML tags using standard regex or HTMLParser.
- Output MUST be valid Python code. No `eval`, no `exec`, no `subprocess`, no `os.system`.

HTML Snippet:
```html
{sanitized}
```
"""


# Fallback deterministic generator if local LLM server is not currently online
def build_fallback_scraper(html: str) -> str:
    return """
import json, re
from html.parser import HTMLParser

def scrape(html: str) -> list[dict]:
    jobs = []
    # 1. Try JSON-LD JobPosting extraction
    ld_matches = re.findall(r'<script[^>]*type=[\"\\\']application/ld\\+json[\"\\\'][^>]*>(.*?)</script>', html, re.DOTALL | re.IGNORECASE)
    for blob in ld_matches:
        try:
            data = json.loads(blob.strip())
            items = data if isinstance(data, list) else [data]
            for item in items:
                if item.get("@type") == "JobPosting":
                    jobs.append({
                        "id": str(item.get("identifier", item.get("url", len(jobs) + 1))),
                        "title": item.get("title", ""),
                        "company": (item.get("hiringOrganization") or {}).get("name", ""),
                        "apply_url": item.get("url", ""),
                        "location": (item.get("jobLocation") or {}).get("address", {}).get("addressLocality", ""),
                        "posted_at": item.get("datePosted", "")
                    })
        except Exception:
            pass

    if jobs:
        return jobs

    # 2. Heuristic DOM search
    cards = re.findall(r'<li[^>]*class=[\"\\\'][^\"\\\']*job-card[^\"\\\']*[\"\\\'][^>]*data-id=[\"\\\'](.*?)[\"\\\'][^>]*>(.*?)</li>', html, re.DOTALL | re.IGNORECASE)
    for jid, body in cards:
        title_m = re.search(r'<a[^>]*class=[\"\\\'][^\"\\\']*job-link[^\"\\\']*[\"\\\'][^>]*>(.*?)</a>', body, re.DOTALL)
        comp_m = re.search(r'<span[^>]*class=[\"\\\'][^\"\\\']*company[^\"\\\']*[\"\\\'][^>]*>(.*?)</span>', body, re.DOTALL)
        apply_m = re.search(r'href=[\"\\\'](http[s]?://[^\"\\\']+)[\"\\\']', body)
        loc_m = re.search(r'<span[^>]*class=[\"\\\'][^\"\\\']*location[^\"\\\']*[\"\\\'][^>]*>(.*?)</span>', body, re.DOTALL)
        if title_m and apply_m:
            jobs.append({
                "id": jid,
                "title": title_m.group(1).strip(),
                "company": comp_m.group(1).strip() if comp_m else "",
                "apply_url": apply_m.group(1).strip(),
                "location": loc_m.group(1).strip() if loc_m else "",
                "posted_at": None
            })
    return jobs
"""


def generate_scraper(html: str, config: dict[str, Any] | None = None) -> str:
    cfg = config or {}
    llm_cfg = cfg.get("llm", {})
    endpoint = llm_cfg.get("endpoint", "http://127.0.0.1:11434/v1")
    model = llm_cfg.get("model", "qwen2.5:7b")

    prompt = build_generation_prompt(html)

    # Attempt query to local OpenAI-compatible endpoint
    try:
        url = f"{endpoint.rstrip('/')}/chat/completions"
        payload = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": "You output only pure executable Python code inside ```python ``` blocks."},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.1,
            "max_tokens": 2048
        }).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            raw_code = data["choices"][0]["message"]["content"]
            # Extract code block
            if "```python" in raw_code:
                raw_code = raw_code.split("```python")[1].split("```")[0]
            elif "```" in raw_code:
                raw_code = raw_code.split("```")[1].split("```")[0]
            if ast_policy_ok(raw_code):
                return raw_code.strip()
    except Exception:
        pass

    # Deterministic fallback
    return build_fallback_scraper(html).strip()
