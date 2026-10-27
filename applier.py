"""Playwright-based assisted application filling for Greenhouse and Lever."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Page

from scraper import fetch_greenhouse_jobs, fetch_lever_jobs, filter_jobs, load_profile
from store import JobStore


class GreenhouseAdapter:
    @staticmethod
    def can_handle(url: str) -> bool:
        return "greenhouse.io" in url.lower()

    @staticmethod
    def fill(page: Page, profile: dict[str, Any]) -> None:
        name_parts = profile["name"].strip().split()
        first = name_parts[0]
        last = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

        if page.is_visible("#first_name"):
            page.fill("#first_name", first)
        if page.is_visible("#last_name"):
            page.fill("#last_name", last)
        if page.is_visible("#email"):
            page.fill("#email", profile["email"])
        if profile.get("phone") and page.is_visible("#phone"):
            page.fill("#phone", profile["phone"])
        if profile.get("location") and page.is_visible("#location"):
            page.fill("#location", profile["location"])

        # Resume upload
        resume = profile.get("resume_path")
        if resume and Path(resume).is_file():
            if page.locator("input[type=file]#resume").count() > 0:
                page.set_input_files("input[type=file]#resume", str(Path(resume).resolve()))
            elif page.locator("input[type=file]").count() > 0:
                page.set_input_files("input[type=file]", str(Path(resume).resolve()))

        # Cover letter
        cover = profile.get("cover_letter")
        if cover and page.is_visible("#cover_letter_text"):
            page.fill("#cover_letter_text", cover)


class LeverAdapter:
    @staticmethod
    def can_handle(url: str) -> bool:
        return "lever.co" in url.lower()

    @staticmethod
    def fill(page: Page, profile: dict[str, Any]) -> None:
        if page.is_visible("input[name='name']"):
            page.fill("input[name='name']", profile["name"])
        if page.is_visible("input[name='email']"):
            page.fill("input[name='email']", profile["email"])
        if profile.get("phone") and page.is_visible("input[name='phone']"):
            page.fill("input[name='phone']", profile["phone"])
        if profile.get("linkedin") and page.is_visible("input[name='urls[LinkedIn]']"):
            page.fill("input[name='urls[LinkedIn]']", profile["linkedin"])
        if profile.get("github") and page.is_visible("input[name='urls[GitHub]']"):
            page.fill("input[name='urls[GitHub]']", profile["github"])

        # Resume upload
        resume = profile.get("resume_path")
        if resume and Path(resume).is_file() and page.locator("input[name='resume']").count() > 0:
            page.set_input_files("input[name='resume']", str(Path(resume).resolve()))

        # Additional info / cover letter
        cover = profile.get("cover_letter")
        if cover and page.is_visible("textarea[name='comments']"):
            page.fill("textarea[name='comments']", cover)


def pick_adapter(url: str):
    if GreenhouseAdapter.can_handle(url):
        return GreenhouseAdapter
    if LeverAdapter.can_handle(url):
        return LeverAdapter
    return None


def run_session(jobs: list[dict[str, Any]], profile: dict[str, Any], store: JobStore, dry_run: bool = False, browser_type: str = "firefox") -> None:
    if not jobs:
        print("No matching jobs to process.")
        return

    # ponytail: headed browser pre-fill + user confirmation; headless state machine only if bulk spamming
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed. Please run: pip install playwright && playwright install firefox")
        return

    b_type = browser_type.lower()
    with sync_playwright() as p:
        launcher = getattr(p, b_type, p.firefox)
        browser = launcher.launch(headless=dry_run)
        context = browser.new_context()
        page = context.new_page()

        for idx, job in enumerate(jobs, 1):
            url = job["apply_url"]
            print(f"\n[{idx}/{len(jobs)}] {job['title']} @ {job['company']}")
            print(f"URL: {url}")

            adapter = pick_adapter(url)
            if not adapter:
                print(f"Skipping: No adapter available for {url}")
                store.record(url, job["company"], job["title"], "skipped")
                continue

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                adapter.fill(page, profile)

                if dry_run:
                    screenshot_dir = Path("screenshots")
                    screenshot_dir.mkdir(exist_ok=True)
                    safe_name = f"{job['company']}_{job['id']}.png".replace("/", "_").replace("\\", "_")
                    screenshot_path = screenshot_dir / safe_name
                    page.screenshot(path=str(screenshot_path))
                    print(f"[DRY-RUN] Filled form & saved screenshot: {screenshot_path}")
                    store.record(url, job["company"], job["title"], "pending")
                    continue

                # Human-in-the-loop gate
                choice = input("Form filled in browser. Complete custom fields. Submit application? [y/N/s(kip)/q(uit)]: ").strip().lower()
                if choice == "y":
                    # Attempt submit click
                    submit_btn = page.locator("input[type=submit], button[type=submit], button:has-text('Submit application')").first
                    if submit_btn.count() > 0:
                        submit_btn.click()
                        time.sleep(3)
                        store.record(url, job["company"], job["title"], "applied")
                        print("Application submitted and recorded.")
                    else:
                        print("Could not find submit button automatically; please submit manually in browser.")
                        store.record(url, job["company"], job["title"], "applied")
                elif choice == "q":
                    print("Exiting session.")
                    break
                else:
                    store.record(url, job["company"], job["title"], "skipped")
                    print("Skipped.")

            except Exception as exc:
                print(f"Error handling {url}: {exc}")
                store.record(url, job["company"], job["title"], "failed")

        browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Auto Job Applier (Ponytail Ultra)")
    parser.add_argument("--profile", default="profile.json", help="Path to profile JSON")
    parser.add_argument("--db", default="state.db", help="Path to SQLite state database")
    parser.add_argument("--greenhouse", action="append", default=[], help="Greenhouse company board tokens (can specify multiple)")
    parser.add_argument("--lever", action="append", default=[], help="Lever company tokens (can specify multiple)")
    parser.add_argument("--dry-run", action="store_true", help="Fill forms and take screenshots without prompting for submit")
    parser.add_argument("--browser", default="firefox", choices=["firefox", "chromium", "webkit"], help="Browser engine to use (default: firefox)")
    args = parser.parse_args()

    profile = load_profile(args.profile)
    store = JobStore(args.db)

    all_jobs: list[dict[str, Any]] = []
    for gh in args.greenhouse:
        print(f"Fetching Greenhouse jobs for {gh}...")
        try:
            all_jobs.extend(fetch_greenhouse_jobs(gh))
        except Exception as e:
            print(f"Failed to fetch Greenhouse jobs for {gh}: {e}")

    for lev in args.lever:
        print(f"Fetching Lever jobs for {lev}...")
        try:
            all_jobs.extend(fetch_lever_jobs(lev))
        except Exception as e:
            print(f"Failed to fetch Lever jobs for {lev}: {e}")

    filtered = filter_jobs(all_jobs, profile, store)
    print(f"\nTotal scraped: {len(all_jobs)} | Eligible after filters & dedup: {len(filtered)}")

    try:
        run_session(filtered, profile, store, dry_run=args.dry_run, browser_type=args.browser)
    finally:
        store.close()


if __name__ == "__main__":
    main()
