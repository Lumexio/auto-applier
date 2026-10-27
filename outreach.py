"""Recruiter discovery and personalized outreach automation."""

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


_SKIP_NAMES = {"linkedin", "sign in", "me", "home", "skip", "message", "connect", "follow", "view profile", "premium"}
# A drafted note must never carry a link or an address: it comes from a model that read scraped page text.
_UNSAFE_NOTE = re.compile(r"https?://|www\.|[\w.+-]+@[\w-]+\.[\w.]+", re.IGNORECASE)


def _profile_key(url: str) -> str:
    """Comparable identity of a LinkedIn profile URL: the slug after /in/, lowercased."""
    m = re.search(r"/in/([^/?#]+)", url or "", re.IGNORECASE)
    return m.group(1).lower() if m else ""


def _is_excluded(url: str, exclude_urls: Iterable[str]) -> bool:
    key = _profile_key(url)
    return bool(key) and key in {_profile_key(u) for u in exclude_urls if u}


@dataclass
class RecruiterInfo:
    name: str
    title: str
    profile_url: str
    source: str  # "job_page_poster" | "company_search"
    has_message_button: bool = False


def extract_recruiter_from_job_page(session: Any, exclude_urls: Iterable[str] = ()) -> RecruiterInfo | None:
    """Scrapes the recruiter / hiring team card directly on the current job view page.

    ``exclude_urls`` (e.g. the applicant's own profile) are never returned: the first /in/ link on a LinkedIn page
    is usually the logged-in user in the top navigation bar.
    """
    try:
        in_links = []
        if hasattr(session, "page"):
            try:
                session.page.evaluate("window.scrollBy(0, 800)")
            except Exception:
                pass
            if hasattr(session, "wait_for_timeout"):
                session.wait_for_timeout(1000)
            in_links = session.page.locator("a[href*='/in/']").all()

        for a in in_links:
            href = a.get_attribute("href") or ""
            if not href or "/in/" not in href:
                continue
            try:
                if a.locator("xpath=ancestor::header | ancestor::nav").count() > 0:
                    continue  # site chrome, not a hiring-team card
            except Exception:
                pass

            text = a.inner_text().strip()
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            if not lines:
                continue

            name = lines[0]
            name = re.sub(r"\s*[•·]\s*(?:1st|2nd|3rd)\+?", "", name).strip()
            if not name or name.lower() in _SKIP_NAMES:
                continue

            title = lines[1] if len(lines) > 1 else "Talent Acquisition / Hiring Team"
            clean_url = href.split("?")[0].rstrip("/")
            if _is_excluded(clean_url, exclude_urls):
                continue

            has_msg = False
            try:
                parent = a.locator(
                    "xpath=./ancestor::div[contains(@class, 'card') or contains(@class, 'section') or contains(@class, 'container') or position()=3]"
                )
                if parent.count() > 0:
                    has_msg = parent.locator("button:has-text('Message')").count() > 0
            except Exception:
                pass

            return RecruiterInfo(
                name=name,
                title=title,
                profile_url=clean_url,
                source="job_page_poster",
                has_message_button=has_msg,
            )
    except Exception as e:
        print(f"[Outreach] Error extracting recruiter from job page: {e}")

    return None


def search_company_recruiter(session: Any, company: str, exclude_urls: Iterable[str] = ()) -> RecruiterInfo | None:
    """Fallback search on LinkedIn for a recruiter at the given company."""
    if not company or company.lower() in ("unknown", "linkedin portal", "handshake network"):
        return None

    try:
        query = urllib.parse.quote(f"{company} recruiter")
        search_url = f"https://www.linkedin.com/search/results/people/?keywords={query}"
        session.goto(search_url)
        if hasattr(session, "wait_for_timeout"):
            session.wait_for_timeout(2500)

        results = session.page.locator("a[href*='/in/']").all() if hasattr(session, "page") else []
        for a in results:
            href = a.get_attribute("href") or ""
            if not href or "/in/" not in href:
                continue
            text = a.inner_text().strip()
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            if not lines:
                continue
            name = re.sub(r"\s*[•·]\s*(?:1st|2nd|3rd)\+?", "", lines[0]).strip()
            if name and name.lower() not in _SKIP_NAMES:
                title = lines[1] if len(lines) > 1 else "Recruiter"
                clean_url = href.split("?")[0].rstrip("/")
                if _is_excluded(clean_url, exclude_urls):
                    continue
                return RecruiterInfo(
                    name=name,
                    title=title,
                    profile_url=clean_url,
                    source="company_search",
                )
    except Exception as e:
        print(f"[Outreach] Error searching recruiter for {company}: {e}")

    return None


def generate_llm_outreach(
    prompt: str,
    endpoint: str = "http://127.0.0.1:11434/v1",
    model: str = "qwen2.5:7b",
    timeout_s: float = 30.0,
) -> str | None:
    """Invokes local OpenAI-compatible endpoint with stdlib urllib to draft dynamic outreach."""
    try:
        url = f"{endpoint.rstrip('/')}/chat/completions"
        payload = json.dumps({
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a job applicant writing a polite, direct LinkedIn connection note. You MUST respond with ONLY the message text. Strictly under 280 characters.",
                },
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 80,
            "temperature": 0.3,
        }).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            content = " ".join(data["choices"][0]["message"]["content"].split()).strip('"').strip("'")
            if not content or _UNSAFE_NOTE.search(content):
                return None
            return content
    except Exception:
        return None


def craft_outreach_message(
    profile: dict[str, Any],
    job: dict[str, Any],
    recruiter_name: str,
    template: str | None = None,
    llm_config: dict[str, Any] | None = None,
    max_chars: int = 280,
) -> str:
    """Generates a concise recruiter note using local LLM with template fallback (< 300 for LinkedIn notes)."""
    first_name = recruiter_name.split()[0] if recruiter_name else "there"
    job_title = job.get("title", "the open position")
    job_title = job_title.split("(")[0].split("-")[0].strip() or "the open role"
    company = job.get("company", "your team")
    if company.lower() in ("unknown", "linkedin portal", "handshake network"):
        company = "your company"

    skills = profile.get("skills", ["Software Engineering"])
    top_skill = skills[0] if skills else "engineering"
    candidate_first = (
        profile.get("first_name")
        or (profile.get("name", "").split() or [""])[0]
    )

    # 1. Attempt dynamic LLM generation from local endpoint
    llm_cfg = llm_config or {}
    if llm_cfg.get("enabled", True):
        prompt = (
            f"Write a LinkedIn connection note to recruiter {first_name} for the {job_title} role at {company}. "
            f"My primary skill is {top_skill}. Sign off from {candidate_first}."
        )
        llm_text = generate_llm_outreach(
            prompt,
            endpoint=llm_cfg.get("endpoint", "http://127.0.0.1:11434/v1"),
            model=llm_cfg.get("model", "qwen2.5:7b"),
            timeout_s=float(llm_cfg.get("timeout_seconds", 30)),
        )
        if llm_text and len(llm_text) <= max_chars:
            return llm_text

    # 2. Template fallback
    if template:
        try:
            msg = template.format(
                recruiter_name=first_name,
                job_title=job_title,
                company=company,
                skill=top_skill,
                candidate_name=candidate_first,
            )
            if len(msg) <= max_chars:
                return msg
        except Exception:
            pass

    # 3. Deterministic safe default
    msg = (
        f"Hi {first_name}, I applied for the {job_title} role at {company}. "
        f"With my background in {top_skill}, I'm very interested in the team's work "
        f"and would love to connect. Best, {candidate_first}."
    )
    if len(msg) > max_chars:
        msg = (
            f"Hi {first_name}, I applied for the {job_title} role at {company} "
            f"and would love to connect about opportunities. Best, {candidate_first}."
        )
    if len(msg) > max_chars:
        msg = msg[: max_chars - 3] + "..."

    return msg


def send_recruiter_outreach(
    session: Any,
    recruiter: RecruiterInfo,
    message_text: str,
    is_dry_run: bool = False,
    screenshot_path: Path | None = None,
) -> tuple[str, str]:
    """
    Sends personalized outreach via Message or Connect + Note.
    Returns: (channel, status)
    status: 'sent' | 'drafted' | 'already_connected' | 'limit_reached' | 'inmail_locked' | 'failed'
    """
    try:
        page = session.page
        if recruiter.profile_url not in page.url:
            session.goto(recruiter.profile_url)
            if hasattr(session, "wait_for_timeout"):
                session.wait_for_timeout(2000)

        # 1. Try Direct Message / InMail if button is available and unlocked
        msg_btn = page.locator("main button:has-text('Message')").first
        if msg_btn.count() > 0 and msg_btn.is_visible():
            msg_btn.click()
            if hasattr(session, "wait_for_timeout"):
                session.wait_for_timeout(1500)

            if (
                page.locator("button:has-text('Try Premium'), div:has-text('InMail')").count() > 0
                and page.locator("div[role='textbox']").count() == 0
            ):
                print(f"[Outreach] InMail locked for {recruiter.name}. Falling back to Connect request with note.")
                if page.locator("button[aria-label='Dismiss']").count() > 0:
                    page.locator("button[aria-label='Dismiss']").first.click()
            else:
                textbox = page.locator(
                    "div[role='textbox'], .msg-form__contenteditable, textarea[name='message']"
                ).first
                if textbox.count() > 0 and textbox.is_visible():
                    textbox.fill(message_text)
                    if hasattr(session, "wait_for_timeout"):
                        session.wait_for_timeout(1000)

                    if screenshot_path:
                        screenshot_path.write_bytes(session.screenshot())

                    if is_dry_run:
                        print(f"[Outreach] [DRY RUN] Direct message drafted for {recruiter.name} ({len(message_text)} chars).")
                        return ("message", "drafted")

                    send_btn = page.locator("button:has-text('Send'), button[type='submit']:has-text('Send')").first
                    if send_btn.count() > 0:
                        send_btn.click()
                        if hasattr(session, "wait_for_timeout"):
                            session.wait_for_timeout(1500)
                        print(f"[Outreach] Direct message sent to {recruiter.name}!")
                        return ("message", "sent")

        # 2. Path B: Connect Request with Note
        if page.locator("main button:has-text('Pending')").count() > 0:
            print(f"[Outreach] Invitation to {recruiter.name} is already pending. Skipping.")
            return ("connection_note", "skipped")

        connect_btn = page.locator("main button:has-text('Connect')").first
        if connect_btn.count() == 0 or not connect_btn.is_visible():
            more_btn = page.locator("main button:has-text('More')").first
            if more_btn.count() > 0 and more_btn.is_visible():
                more_btn.click()
                if hasattr(session, "wait_for_timeout"):
                    session.wait_for_timeout(1000)
                connect_btn = page.locator(
                    "div[role='menu'] div:has-text('Connect'), div[role='menu'] button:has-text('Connect')"
                ).first

        if connect_btn.count() > 0 and connect_btn.is_visible():
            connect_btn.click()
            if hasattr(session, "wait_for_timeout"):
                session.wait_for_timeout(1500)

            if page.locator("text=/weekly invitation limit/i").count() > 0:
                print(f"[Outreach] Weekly invitation limit reached on LinkedIn.")
                if page.locator("button:has-text('Got it'), button[aria-label='Dismiss']").count() > 0:
                    page.locator("button:has-text('Got it'), button[aria-label='Dismiss']").first.click()
                return ("connection_note", "limit_reached")

            add_note_btn = page.locator("button:has-text('Add a note')").first
            if add_note_btn.count() > 0 and add_note_btn.is_visible():
                add_note_btn.click()
                if hasattr(session, "wait_for_timeout"):
                    session.wait_for_timeout(1000)

                note_area = page.locator("textarea[name='message']").first
                if note_area.count() > 0:
                    note_area.fill(message_text)
                    if hasattr(session, "wait_for_timeout"):
                        session.wait_for_timeout(500)

                if screenshot_path:
                    screenshot_path.write_bytes(session.screenshot())

                if is_dry_run:
                    print(f"[Outreach] [DRY RUN] Connection note drafted for {recruiter.name} ({len(message_text)} chars).")
                    if page.locator("button[aria-label='Dismiss']").count() > 0:
                        page.locator("button[aria-label='Dismiss']").first.click()
                    return ("connection_note", "drafted")

                send_note_btn = page.locator("button:has-text('Send')").first
                if send_note_btn.count() > 0:
                    send_note_btn.click()
                    if hasattr(session, "wait_for_timeout"):
                        session.wait_for_timeout(1500)
                    print(f"[Outreach] Connection note sent to {recruiter.name}!")
                    return ("connection_note", "sent")
            else:
                if page.locator("main button:has-text('Pending')").count() > 0:
                    # LinkedIn sent the invitation as soon as Connect was clicked, with no chance to add a note.
                    print(f"[Outreach] Clicking Connect sent an invitation to {recruiter.name} without a note.")
                    return ("connection_note", "failed")
                if page.locator("button[aria-label='Dismiss']").count() > 0:
                    page.locator("button[aria-label='Dismiss']").first.click()

        return ("inmail", "inmail_locked")

    except Exception as e:
        print(f"[Outreach] Error during outreach to {recruiter.name}: {e}")
        return ("message", "failed")
