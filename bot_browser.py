"""The bot's own browser (the persistent profile with your sign-ins), for work that comes after stage 3 in a cycle.

Looking at postings again (reverify.py) and sending the messages you approved (outreach_queue.py) both need it, and only when there is something to
do, so each asks for a session here instead of keeping a browser open.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


def run_with_session(cfg: dict[str, Any], headless: bool, work: Callable[[Any], Any], tag: str) -> Any:
    """Open the bot's browser, call ``work(session)`` and close it again. None when Playwright is not installed."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(f"[{tag}] Playwright is not installed; skipping.")
        return None
    from stage3_apply.run import PlaywrightSession, _launch_context

    browser_cfg = cfg.get("browser", {})
    base = Path(browser_cfg.get("user_data_dir", "./.browser_profile")).resolve()
    with sync_playwright() as p:
        context = _launch_context(p, str(browser_cfg.get("type", "firefox")).lower(), base, headless)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            return work(PlaywrightSession(page, context))
        finally:
            try:
                context.close()
            except Exception:
                pass
