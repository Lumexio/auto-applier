"""Look again at applications that were sent but never confirmed.

    python reverify.py status       the unconfirmed applications, how often each was looked at again and what the last look found
    python reverify.py run          look at the ones that are due now (only while the daemon is stopped: both use the bot's browser)

An application is ``unconfirmed`` when the submit button was clicked and then neither a confirmation page nor an error appeared. It is never
sent a second time (that could apply twice). Some sites remember what you applied to, though: LinkedIn shows "Applied 2 days ago" and
"See application status" on a posting you applied to. After each cycle the daemon opens the postings of those sites for the oldest unconfirmed
applications, reads the page, and when it says you applied, records the application as ``submitted``. That is all it does with the page:

* it never clicks anything, fills nothing in and never applies;
* what it accepts as proof is a few fixed phrases the site itself shows (see ``APPLIED``), on the posting's own page, while signed in;
* a page that sent the browser to a sign-in page, shows a CAPTCHA (never solved), or sent it to some other posting proves nothing and is
  written down as such, not held against the application;
* it looks at most ``max_checks`` times, ``recheck_hours`` apart, and then leaves the application for you (it stays listed as unconfirmed).

Settings: the ``reverify:`` section of config.yaml.
"""

from __future__ import annotations

import argparse
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "sites": ["linkedin", "handshake"],  # kinds of site (as in `sites.py`) whose postings remember that you applied; elsewhere a look proves nothing
    "min_age_hours": 6,                  # an application is given this long to show up on the site before the first look
    "recheck_hours": 24,                 # between two looks at the same posting
    "max_checks": 4,                     # looks that found the posting without a sign of an application, after which it is left alone
    "max_per_cycle": 5,                  # postings opened in one cycle
    "delay_seconds": [4, 10],            # a pause between two postings
    "headless": True,
}

# What a posting you applied to shows, and nothing broader: the old "Applied" alone matched menus ("Applied jobs") and company names.
APPLIED = (
    re.compile(r"See application status"),
    re.compile(r"Application submitted"),
    re.compile(r"You(?:'|’)ve applied|You applied"),
    re.compile(r"Applied (?:just now|\d+ (?:minute|hour|day|week|month)s? ago)"),
)
_SIGN_IN_PATH = re.compile(r"(?:^|/)(?:log-?in|sign[-_]?in|authwall|checkpoint|uas/login)(?:$|[/?#.])", re.IGNORECASE)
RESULTS = ("applied", "not_seen", "signed_out", "blocked", "error")


@dataclass
class Look:
    result: str                         # one of RESULTS
    note: str = ""


@dataclass
class Summary:
    looked: int = 0
    confirmed: int = 0
    skipped_sites: list[str] = field(default_factory=list)      # sites given up on for this cycle (signed out, CAPTCHA)


def settings(cfg: dict[str, Any] | None) -> dict[str, Any]:
    return {**DEFAULTS, **((cfg or {}).get("reverify") or {})}


def applied_phrase(text: str) -> str | None:
    """The first sign of an application found in a posting's text, or None."""
    for pattern in APPLIED:
        m = pattern.search(text or "")
        if m:
            return m.group(0)
    return None


def site_of(url: str) -> str:
    from stage3_apply.ats_handlers import pick_handler
    return pick_handler(url).name


def _same_posting(wanted: str, here: str) -> bool:
    a, b = urlparse(wanted), urlparse(here)
    host = lambda u: (u.hostname or "").lower().removeprefix("www.")
    return host(a) == host(b) and a.path.rstrip("/") == b.path.rstrip("/")


def look(session: Any, job: dict[str, Any]) -> Look:
    """Open the posting and say what it shows. Reads only; never raises."""
    url = job["apply_url"]
    try:
        session.goto(url)
        here = session.current_url()
        if _SIGN_IN_PATH.search(urlparse(here).path):
            return Look("signed_out", "the posting sent the browser to a sign-in page")
        if hasattr(session, "has_captcha") and session.has_captcha():
            return Look("blocked", "a CAPTCHA is showing (not solved here)")
        if not _same_posting(url, here):
            return Look("not_seen", f"the browser ended up on {urlparse(here).hostname}{urlparse(here).path}, not on the posting")
        seen = applied_phrase(session.text("body"))
        if seen:
            return Look("applied", f'the posting shows "{seen}"')
        if hasattr(session, "requires_login") and session.requires_login():
            return Look("signed_out", "the posting is showing its signed-out page")
        return Look("not_seen", "the posting does not say you applied")
    except Exception as e:
        return Look("error", f"could not read the posting ({type(e).__name__})")


def due(rows: list[dict[str, Any]], opts: dict[str, Any], now: datetime | None = None) -> list[dict[str, Any]]:
    """The unconfirmed applications that are old enough, not looked at too recently or too often, and on a site that remembers applications."""
    now = now or datetime.now(timezone.utc)
    wanted = {str(s).lower() for s in opts["sites"]}
    ripe = now - timedelta(hours=float(opts["min_age_hours"]))
    stale = now - timedelta(hours=float(opts["recheck_hours"]))
    out = []
    for row in rows:
        if row["misses"] >= int(opts["max_checks"]):
            continue
        if _when(row["started_at"]) > ripe or (row["last_look"] and _when(row["last_look"]) > stale):
            continue
        if site_of(row["apply_url"]).lower() not in wanted:
            continue
        out.append(row)
    return out[: max(0, int(opts["max_per_cycle"]))]


def _when(stamp: str | None) -> datetime:
    try:
        when = datetime.fromisoformat(str(stamp))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def run(
    state: Any,
    cfg: dict[str, Any] | None,
    *,
    session: Any = None,
    should_stop: Callable[[], bool] | None = None,
    on_tick: Callable[[], None] | None = None,
    now: datetime | None = None,
    pause: Callable[[float], None] = time.sleep,
) -> Summary:
    """Look at the applications that are due. Opens the bot's browser only when there is something to look at (or uses ``session``)."""
    opts = settings(cfg)
    summary = Summary()
    if not opts["enabled"]:
        return summary
    jobs = due(state.unconfirmed_with_checks(), opts, now)
    if not jobs:
        return summary
    if session is None:
        return _in_browser(state, cfg or {}, opts, jobs, should_stop, on_tick, pause)
    return _look_at(session, state, opts, jobs, should_stop, on_tick, pause)


def _look_at(session: Any, state: Any, opts: dict[str, Any], jobs: list[dict[str, Any]], should_stop, on_tick, pause) -> Summary:
    summary = Summary()
    low, high = (list(opts["delay_seconds"]) + [0, 0])[:2]
    walled: set[str] = set()
    for n, job in enumerate(jobs):
        if should_stop and should_stop():
            break
        site = site_of(job["apply_url"])
        if site in walled:
            continue
        if n and max(low, high) > 0:
            pause(random.uniform(min(low, high), max(low, high)))
        if on_tick:
            on_tick()
        outcome = look(session, job)
        state.record_reverify_check(job["job_id"], outcome.result, outcome.note)
        summary.looked += 1
        name = f"{job['title']} @ {job['company']}"
        if outcome.result == "applied":
            if state.confirm_unconfirmed(job["job_id"], outcome.note):
                summary.confirmed += 1
                print(f"[Reverify] {name}: {outcome.note}. Recorded as submitted.")
                state.log_event("INFO", "reverify", "confirmed", outcome.note, job_id=job["job_id"])
        elif outcome.result in ("signed_out", "blocked"):
            walled.add(site)
            summary.skipped_sites.append(site)
            print(f"[Reverify] {name}: {outcome.note}. Not looking at more {site} postings this cycle"
                  + ("; sign in again in the bot's own browser (python daemon.py --login <site url>)." if outcome.result == "signed_out" else "."))
        else:
            left = int(opts["max_checks"]) - int(job["misses"]) - (1 if outcome.result == "not_seen" else 0)
            tail = f"; will look {left} more time(s)" if outcome.result == "not_seen" and left > 0 else "; not looking again" if outcome.result == "not_seen" else ""
            print(f"[Reverify] {name}: {outcome.note}{tail}.")
    return summary


def _in_browser(state: Any, cfg: dict[str, Any], opts: dict[str, Any], jobs: list[dict[str, Any]], should_stop, on_tick, pause) -> Summary:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[Reverify] Playwright is not installed; not looking at unconfirmed applications.")
        return Summary()
    from stage3_apply.run import PlaywrightSession, _launch_context

    browser_cfg = cfg.get("browser", {})
    base = Path(browser_cfg.get("user_data_dir", "./.browser_profile")).resolve()
    print(f"[Reverify] Looking at {len(jobs)} unconfirmed application(s) again.")
    with sync_playwright() as p:
        context = _launch_context(p, browser_cfg.get("type", "firefox").lower(), base, bool(opts["headless"]))
        try:
            page = context.pages[0] if context.pages else context.new_page()
            summary = _look_at(PlaywrightSession(page, context), state, opts, jobs, should_stop, on_tick, pause)
        finally:
            try:
                context.close()
            except Exception:
                pass
    print(f"[Reverify] Looked at {summary.looked}; {summary.confirmed} now confirmed.")
    return summary


def describe(rows: list[dict[str, Any]], opts: dict[str, Any], now: datetime | None = None) -> list[str]:
    """One line per unconfirmed application: what is known and when it is looked at next."""
    now = now or datetime.now(timezone.utc)
    ready = {r["job_id"] for r in due(rows, {**opts, "max_per_cycle": 10**6}, now)}
    wanted = {str(s).lower() for s in opts["sites"]}
    lines = []
    for r in rows:
        site = site_of(r["apply_url"])
        if site.lower() not in wanted:
            plan = f"not re-checked ({site} does not remember applications)"
        elif r["misses"] >= int(opts["max_checks"]):
            plan = f"looked {r['misses']} times, nothing seen: left to you"
        elif r["job_id"] in ready:
            plan = "due at the end of the next cycle"
        else:
            plan = "not due yet"
        last = f"; last look: {r['last_result']}" if r["last_result"] else ""
        lines.append(f"  {r['title']} @ {r['company']} [{site}] - {plan}{last}")
    return lines


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="config.yaml")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("run")
    args = parser.parse_args(argv)

    cfg: dict[str, Any] = {}
    if Path(args.config).is_file():
        from config_loader import load_config
        cfg = load_config(Path(args.config))
    from state import StateStore
    state = StateStore(cfg.get("daemon", {}).get("state_db", "state.db"))
    opts = settings(cfg)

    if args.cmd == "status":
        rows = state.unconfirmed_with_checks()
        if not rows:
            print("No application is waiting for a confirmation.")
            return 0
        print(f"{len(rows)} application(s) sent but never confirmed" + ("" if opts["enabled"] else " (re-checking is switched off: reverify.enabled)") + ":")
        print("\n".join(describe(rows, opts)))
        return 0

    if not state.acquire_lock():
        print("The daemon is running and uses the same browser: it looks at these itself after each cycle. Stop it first to run this by hand.")
        return 1
    try:
        summary = run(state, cfg)
    finally:
        state.release_lock()
    if not summary.looked:
        print("Nothing is due: " + ("re-checking is switched off." if not opts["enabled"] else "see `python reverify.py status`."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
