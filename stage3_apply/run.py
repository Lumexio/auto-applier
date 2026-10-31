"""Stage 3 orchestrator: Autonomous multi-ATS application engine."""

from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path
from typing import Any, Callable

from answerer import DEFAULT_ANSWERS_CFG, QuestionSolver
from facts import load_facts
from fit import DEFAULT_FIT_CFG, FitScorer, build_fit_gate
from llm_client import build_llm_cfg
from form_questions import EXTRACT_JS
from sites import DEFAULT_PROBATION, site_fingerprint

from .ats_handlers import (
    AgentHandler,
    CaptchaEncountered,
    DuplicateApplication,
    FallbackHandler,
    FitDisputed,
    LoginRequired,
    NeedsHumanInput,
    NotAFit,
    PostingClosed,
    UnsupportedForm,
    pick_handler,
)
from .confirm import wait_for_confirmation
from .ratelimit import RateLimiter, extract_domain

from agent import DEFAULT_AGENT_CFG

VALID_MODES = ("dry_run", "assisted", "autonomous")


def resolve_mode(profile: dict[str, Any], stage3_cfg: dict[str, Any]) -> str:
    """Pick the effective run mode, failing safe.

    ``dry_run`` requested by *either* the config (CLI --dry-run, REST API, Langflow) or the profile
    always wins, so a profile that says "autonomous" can never turn an explicit dry run into a live
    one. A missing or unrecognised mode also resolves to ``dry_run`` instead of submitting.
    """
    profile_mode = profile.get("mode")
    cfg_mode = stage3_cfg.get("mode")
    if "dry_run" in (profile_mode, cfg_mode):
        return "dry_run"
    mode = profile_mode or cfg_mode
    if mode in VALID_MODES:
        return mode
    if mode:
        print(f"[Stage 3] Unknown mode {mode!r}; falling back to dry_run. Valid modes: {', '.join(VALID_MODES)}.")
    return "dry_run"


def _safe_dirname(name: str) -> str:
    """Job ids can be URLs; keep them usable as a single Windows-safe directory name."""
    return re.sub(r"[^\w.-]+", "_", name).strip("._")[:80] or "job"


def _quietly(fn: Callable[[], Any]) -> None:
    try:
        fn()
    except Exception:
        pass


class PlaywrightSession:
    """Wraps a Playwright Page object into the BrowserSession protocol."""

    def __init__(self, page: Any, context: Any = None):
        self.page = page
        self.context = context
        self._main = page                                  # the tab the daemon owns; others are opened by applications
        self._pages_before: list[Any] = []
        self._opened: list[Any] = []                       # tabs the browser reports as newly opened (popups, target=_blank)
        self.settle_ms = 700                               # how long a click's consequences get to land before the page is read again
        self._wire(page)
        if context is not None:
            try:
                context.on("page", lambda p: self._opened.append(p))
            except Exception:
                pass

    def _wire(self, page: Any) -> None:
        """A JS alert/confirm/beforeunload dialog would freeze the page for good, so answer them all."""
        try:
            page.on("dialog", lambda d: _quietly(d.dismiss))
        except Exception:
            pass

    @property
    def url(self) -> str:
        return self.page.url

    def current_url(self) -> str:
        return self.page.url

    def goto(self, url: str, *, timeout_s: int = 30) -> None:
        self.page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
        try:
            self.page.wait_for_timeout(1500)
        except Exception:
            pass

    def wait_for_timeout(self, ms: int) -> None:
        try:
            self.page.wait_for_timeout(ms)
        except Exception:
            pass

    def fill(self, selector: str, value: str) -> None:
        if self.page.is_visible(selector):
            self.page.fill(selector, value)

    def select(self, selector: str, value: str) -> None:
        if self.page.is_visible(selector):
            try:
                self.page.select_option(selector, label=value)
            except Exception:
                try:
                    self.page.select_option(selector, value=value)
                except Exception:
                    pass

    def check(self, selector: str) -> None:
        if self.page.is_visible(selector):
            self.page.check(selector)

    def upload(self, selector: str, path: str) -> None:
        p = Path(path)
        if p.is_file() and self.page.locator(selector).count() > 0:
            self.page.set_input_files(selector, str(p.resolve()))

    def click(self, selector: str) -> None:
        if self.page.locator(selector).count() > 0:
            self._remember_pages()
            self.page.locator(selector).first.click()

    def wait_for(self, selector: str, *, timeout_s: int = 30) -> None:
        try:
            self.page.wait_for_selector(selector, timeout=timeout_s * 1000)
        except Exception:
            pass

    def has(self, selector: str, wait_timeout_s: float = 0) -> bool:
        try:
            if wait_timeout_s > 0:
                self.page.wait_for_selector(selector, timeout=int(wait_timeout_s * 1000))
                return True
            if selector.startswith("text="):
                txt = selector.split("text=", 1)[1]
                return self.page.get_by_text(txt).count() > 0
            return self.page.locator(selector).count() > 0
        except Exception:
            return False

    def text(self, selector: str) -> str:
        try:
            return self.page.locator(selector).first.inner_text()
        except Exception:
            return ""

    def screenshot(self) -> bytes:
        try:
            return self.page.screenshot()
        except Exception:
            return b""

    def requires_login(self) -> bool:
        return self.has("button:has-text('Sign In')") or self.has("form[action*='/login']")

    # ---- frames ---------------------------------------------------------------

    def _frame(self, index: int) -> Any:
        """The page's ``index``-th frame (0 = the page itself); sessions without frames behave as one."""
        frames = getattr(self.page, "frames", None)
        return frames[index] if frames and 0 <= index < len(frames) else self.page

    # ---- reading and answering the form's questions (see form_questions.py) ----

    def extract_questions(self) -> list[dict[str, Any]]:
        frames = getattr(self.page, "frames", None)
        if not frames:
            return self.page.evaluate(EXTRACT_JS, {"base": 1}) or []
        found: list[dict[str, Any]] = []
        for index, frame in enumerate(frames):
            try:
                items = frame.evaluate(EXTRACT_JS, {"base": 1 + index * 1000}) or []
            except Exception:
                continue                                   # a detached or cross-origin frame we cannot read
            for item in items:
                item["frame"] = index
                found.append(item)
        return found

    def fill_answer(self, q: Any, value: Any) -> None:
        """Put ``value`` into the control(s) tagged for question ``q``. Raises if the page does not take it."""
        target = self._frame(getattr(q, "frame", 0))
        sel = f'[data-aa-id="{q.id}"]'
        if q.kind in ("text", "textarea"):
            target.fill(sel, str(value))
        elif q.kind == "select":
            try:
                target.select_option(sel, label=str(value))
            except Exception:
                target.select_option(sel, value=str(value))
        elif q.kind == "radio":
            self._set_checked(target, f'{sel}[data-aa-opt="{q.options.index(value)}"]', True)
        elif q.kind == "checkbox":
            self._set_checked(target, sel, bool(value))
        elif q.kind == "checkbox_group":
            for text in value:
                self._set_checked(target, f'{sel}[data-aa-opt="{q.options.index(text)}"]', True)
        elif q.kind == "combobox":
            self._fill_combobox(target, sel, str(value))
        else:
            raise ValueError(f"unsupported question type {q.kind!r}")

    @staticmethod
    def _set_checked(target: Any, selector: str, want: bool) -> None:
        loc = target.locator(selector).first
        if loc.is_checked() == want:
            return
        try:
            loc.check(force=True) if want else loc.uncheck(force=True)
        except Exception:
            loc.evaluate("el => el.click()")           # styled controls whose real <input> is not clickable
        if loc.is_checked() != want:
            raise RuntimeError(f"could not set {selector} to {want}")

    @staticmethod
    def _fill_combobox(target: Any, selector: str, value: str) -> None:
        loc = target.locator(selector).first
        loc.click()
        loc.fill(value)
        target.wait_for_timeout(300)
        option = target.locator('[role="option"]').filter(has_text=value)
        if option.count() > 0:
            option.first.click()
        else:
            loc.press("Enter")

    # ---- the whole-page view the application agent works from (see page_state.py) ----

    def snapshot_raw(self) -> list[dict[str, Any]]:
        from page_state import SNAPSHOT_JS

        frames = getattr(self.page, "frames", None)
        if not frames:
            data = self.page.evaluate(SNAPSHOT_JS, {"base": 1})
            return [dict(data, frame=0)] if isinstance(data, dict) else []
        out: list[dict[str, Any]] = []
        for index, frame in enumerate(frames):
            try:
                data = frame.evaluate(SNAPSHOT_JS, {"base": 1 + index * 1000})
            except Exception:
                continue
            if isinstance(data, dict):
                out.append(dict(data, frame=index))
        return out

    def click_action(self, action: Any) -> None:
        """Click a button/link the last snapshot tagged, adopting a new tab if the click opened one."""
        self._remember_pages()
        loc = self._frame(getattr(action, "frame", 0)).locator(f'[data-aa-act="{action.id}"]').first
        try:
            loc.click(timeout=6000)
        except Exception:
            loc.evaluate("el => el.click()")           # covered by an overlay, or an unusual custom control
        self.settle(self.settle_ms)                    # a popup announces itself while we wait for the page to settle
        self.adopt_new_page(wait_ms=0)

    def upload_file(self, field: Any, path: str) -> None:
        self._frame(getattr(field, "frame", 0)).locator(f'[data-aa-file="{field.id}"]').first.set_input_files(str(Path(path).resolve()))

    def settle(self, ms: int = 700) -> None:
        """Let a click's consequences land (navigation, XHR re-render) before the page is read again."""
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        self.wait_for_timeout(ms)

    def _remember_pages(self) -> None:
        self._pages_before = list(getattr(self.context, "pages", []) or [])
        self._opened.clear()

    def adopt_new_page(self, wait_ms: int = 0) -> bool:
        """If the last click opened a new tab, work in it from now on. True when the session moved."""
        if self.context is None:
            return False
        if wait_ms:
            self.wait_for_timeout(wait_ms)
        fresh = [p for p in self._opened if not p.is_closed()] or [
            p for p in self.context.pages if p not in self._pages_before and not p.is_closed()
        ]
        if not fresh:
            return False
        self.page = fresh[-1]
        self._wire(self.page)
        try:
            self.page.bring_to_front()
            self.page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        return True

    def restore_main(self) -> None:
        """Close the tabs an application opened and go back to the daemon's own tab."""
        if self.context is not None:
            for p in list(self.context.pages):
                if p is not self._main and not p.is_closed():
                    _quietly(p.close)
        self.page = self._main

    def page_text(self, max_chars: int = 9000) -> str:
        """Visible text of the main content, for judging a posting that only exists on the page."""
        for selector in ("main", "[role='main']", "body"):
            loc = self.page.locator(selector)
            if loc.count() > 0:
                text = loc.first.inner_text()
                if len(text) > 200:
                    return text[:max_chars]
        return ""

    def has_captcha(self) -> bool:
        return self.has("iframe[src*='recaptcha']") or self.has("iframe[src*='turnstile']") or self.has("iframe[src*='hcaptcha']")


def _handle_recruiter_outreach(
    session: Any,
    job: dict[str, Any],
    profile: dict[str, Any],
    state: Any,
    job_id: str,
    app_dir: Path,
    cfg: dict[str, Any],
    is_dry_run: bool,
) -> None:
    outreach_cfg = cfg.get("outreach", {})
    # Opt-in: outreach drives a real LinkedIn account, so it only runs when explicitly enabled.
    if not outreach_cfg.get("enabled", False) or not state:
        return

    max_per_day = outreach_cfg.get("max_per_day", 5)
    daily_count = state.get_outreach_count_today()
    if daily_count >= max_per_day:
        print(f"[Outreach] Daily recruiter outreach limit reached ({daily_count}/{max_per_day}). Pausing.")
        return

    try:
        from outreach import (
            craft_outreach_message,
            extract_recruiter_from_job_page,
            search_company_recruiter,
            send_recruiter_outreach,
        )

        own_profile = [profile.get("linkedin")]
        recruiter = extract_recruiter_from_job_page(session, exclude_urls=own_profile)
        if not recruiter:
            recruiter = search_company_recruiter(session, job.get("company", ""), exclude_urls=own_profile)

        if recruiter:
            if state.has_recruiter_been_contacted(recruiter.profile_url):
                print(f"[Outreach] Recruiter {recruiter.name} already contacted. Skipping.")
                return

            msg = craft_outreach_message(
                profile,
                job,
                recruiter.name,
                template=outreach_cfg.get("template"),
                llm_config=cfg.get("llm"),
            )
            outreach_shot = app_dir / "recruiter_outreach.png"
            channel, out_status = send_recruiter_outreach(
                session,
                recruiter,
                msg,
                is_dry_run=is_dry_run,
                screenshot_path=outreach_shot,
            )
            state.record_outreach(
                job_id=job_id,
                company=job.get("company", ""),
                recruiter_name=recruiter.name,
                recruiter_url=recruiter.profile_url,
                message_text=msg,
                channel=channel,
                status=out_status,
            )
            print(f"[Outreach] Logged outreach for {recruiter.name} ({channel} -> {out_status})")
    except Exception as e:
        print(f"[Outreach] Note: Recruiter outreach encountered error: {e}")


def _count_clean_run(state: Any, page_url: str, job_id: str, probation: dict[str, Any]) -> None:
    site_key = site_fingerprint(page_url)
    if state.record_clean_run(site_key, job_id, probation["clean_runs_to_trust"]):
        released = state.release_probation(site_key)
        print(f"[Probation] '{site_key}' is now trusted after {probation['clean_runs_to_trust']} clean runs; {released} parked job(s) released.")


def _hold_for_probation(
    state: Any, tx: Any, handler: Any, job: dict[str, Any], job_id: str, app_dir: Path, site_key: str, page_url: str, probation: dict[str, Any]
) -> None:
    """Park a fully prepared application: it is ready to submit, but this kind of site has not earned that yet."""
    newly_trusted = state.record_clean_run(site_key, job_id, probation["clean_runs_to_trust"])
    clean = state.conn.execute("SELECT COUNT(*) FROM site_clean_runs WHERE site_key = ?", (site_key,)).fetchone()[0]
    note = (
        f"probation: '{site_key}' has not been approved yet ({clean}/{probation['clean_runs_to_trust']} clean runs). "
        f"Review applications/{app_dir.name}/ then: python sites.py approve {site_key}"
    )
    print(f"[Probation] {job['title']} @ {job['company']} is filled in and ready, but not submitted. {note}")
    try:
        (app_dir / "probation.json").write_text(
            json.dumps(
                {"site": site_key, "job": job["title"], "company": job["company"], "url": page_url, "handler": handler.name,
                 "clean_runs": clean, "needed": probation["clean_runs_to_trust"], "review": ["form_snapshot.png", "answers.json", "fit.json"],
                 "to_trust": f"python sites.py approve {site_key}"},
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass
    if tx:
        tx.mark("needs_review", ats=handler.name, error=note[:300], error_code="probation")
    if newly_trusted:
        released = state.release_probation(site_key)
        print(f"[Probation] '{site_key}' is now trusted; {released} parked job(s) released and will be queued again.")


def _resolve_outcome(session: Any, handler: Any, job: dict[str, Any], res: Any, baseline_url: str, confirm_timeout: int) -> tuple[str, str]:
    """Decide what the submit click achieved: ('submitted' | 'unconfirmed' | 'rejected', evidence).

    1. the handler's own observation, taken before it dismissed any dialog;
    2. otherwise a confirmation page / marker (the URL must have moved off the pre-submit page);
    3. otherwise reload the posting: sites that remember applications now show it as applied.
    """
    if res.confirmed is True:
        return "submitted", res.evidence
    if res.confirmed is False:
        return "rejected", res.evidence
    if wait_for_confirmation(session, timeout_seconds=confirm_timeout, baseline_url=baseline_url):
        return "submitted", "confirmation page detected"
    try:
        if handler.verify_applied(job):
            return "submitted", "reloading the posting shows it as applied"
    except Exception:
        pass
    return "unconfirmed", res.evidence


def _load_facts_or_empty(cfg: dict[str, Any]) -> dict[str, Any]:
    path = cfg.get("paths", {}).get("facts", "facts.yaml")
    try:
        facts = load_facts(path)
    except Exception as e:                       # malformed YAML must not take the daemon down
        print(f"[Stage 3] Could not read {path}: {e}. Continuing without a fact sheet.")
        return {}
    if not facts:
        print(f"[Stage 3] No {path}: only your contact details can be filled in. Create it with: python answers.py facts-init")
    return facts


def _clean_lock(user_data_dir: Path) -> None:
    try:
        from import_profile import clean_stale_firefox_lock
        clean_stale_firefox_lock(user_data_dir)
    except Exception:
        pass


def _launch_context(p: Any, browser_type: str, base_data_dir: Path, headless: bool) -> Any:
    """Open the persistent browser context, retrying once and falling back to Chromium for Firefox."""
    user_data_dir = base_data_dir / browser_type
    user_data_dir.mkdir(parents=True, exist_ok=True)
    _clean_lock(user_data_dir)

    # ponytail: persistent context preserves pre-logged-in session cookies/tokens across runs
    launcher = getattr(p, browser_type, p.firefox)
    try:
        return launcher.launch_persistent_context(user_data_dir=str(user_data_dir), headless=headless)
    except Exception as launch_err:
        time.sleep(1)
        _clean_lock(user_data_dir)
        try:
            return launcher.launch_persistent_context(user_data_dir=str(user_data_dir), headless=headless)
        except Exception:
            if browser_type == "firefox":
                print("[Stage 3] Firefox profile locked or busy. Falling back to Chromium...")
                chrom_dir = base_data_dir / "chromium"
                chrom_dir.mkdir(parents=True, exist_ok=True)
                return p.chromium.launch_persistent_context(user_data_dir=str(chrom_dir), headless=headless)
            raise launch_err


def _pause_between_applications(rate_cfg: dict[str, Any], should_stop: Callable[[], bool] | None) -> None:
    """Random human-scale gap between applications (config: stage3.rate_limit.min/max_delay_seconds)."""
    low = float(rate_cfg.get("min_delay_seconds", 0) or 0)
    high = float(rate_cfg.get("max_delay_seconds", low) or 0)
    delay = random.uniform(min(low, high), max(low, high)) if max(low, high) > 0 else 0.0
    deadline = time.monotonic() + delay
    while time.monotonic() < deadline:
        if should_stop and should_stop():
            return
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))


def stage3_run(
    jobs: list[dict[str, Any]],
    profile: dict[str, Any],
    state: Any = None,
    run_id: int | None = None,
    config: dict[str, Any] | None = None,
    should_stop: Callable[[], bool] | None = None,
    on_tick: Callable[[], None] | None = None,
) -> None:
    """Apply to ``jobs``.

    ``should_stop`` is polled between jobs so the daemon can shut down cleanly instead of being
    killed mid-application; ``on_tick`` is called once per job (the daemon uses it as a heartbeat).
    """
    if not jobs:
        print("[Stage 3] No jobs to apply.")
        return

    cfg = config or {}
    stage3_cfg = cfg.get("stage3", {})
    mode = resolve_mode(profile, stage3_cfg)
    is_dry_run = mode == "dry_run"
    max_attempts = stage3_cfg.get("max_attempts", 3)
    if is_dry_run:
        print("[Stage 3] DRY RUN: forms are prepared and screenshotted but nothing is submitted or recorded as an application.")

    rate_cfg = stage3_cfg.get("rate_limit", {})
    limiter = RateLimiter(
        state=state,
        per_day=profile.get("max_applications_per_day", rate_cfg.get("global_per_day", 25)),
        per_domain_per_hour=profile.get("max_applications_per_domain_per_hour", rate_cfg.get("per_domain_per_hour", 5)),
    )
    limited_domains: set[str] = set()

    llm_cfg = build_llm_cfg(cfg)                       # the `llm` block plus the optional `models:` roles; each component picks its own model from it
    answers_cfg = {**DEFAULT_ANSWERS_CFG, **cfg.get("answers", {})}
    fit_cfg = {**DEFAULT_FIT_CFG, **cfg.get("fit", {})}
    confirm_timeout = int(stage3_cfg.get("confirmation_timeout_seconds", 20))
    probation = {**DEFAULT_PROBATION, **(cfg.get("agent", {}).get("probation") or {})}
    agent_cfg = {**DEFAULT_AGENT_CFG, **{k: v for k, v in cfg.get("agent", {}).items() if k != "probation"}}
    facts = _load_facts_or_empty(cfg) if (answers_cfg["enabled"] or fit_cfg["enabled"]) else {}
    solver = (
        QuestionSolver(profile, facts, llm_cfg, answers_cfg, bank_get=state.get_answer if state else None)
        if answers_cfg["enabled"] else None
    )
    fit_gate = None
    if fit_cfg["enabled"]:
        fit_gate = build_fit_gate(
            FitScorer(profile, facts, llm_cfg, fit_cfg, state),
            lambda j: Path("applications") / _safe_dirname(str(j.get("id", j["apply_url"]))),
        )

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[Stage 3] Playwright is not installed. Run: pip install playwright && playwright install chromium")
        return

    # In autonomous mode, run headless unless user configures otherwise
    headless = is_dry_run or mode == "autonomous"
    browser_cfg = cfg.get("browser", {})
    browser_type = browser_cfg.get("type", "firefox").lower()
    base_data_dir = Path(browser_cfg.get("user_data_dir", "./.browser_profile")).resolve()

    with sync_playwright() as p:
        context = _launch_context(p, browser_type, base_data_dir, headless)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            session = PlaywrightSession(page, context)

            for job in jobs:
                if should_stop and should_stop():
                    print("[Stage 3] Shutdown requested. Stopping before the next application.")
                    break
                if on_tick:
                    on_tick()

                job_id = str(job.get("id", job["apply_url"]))
                url = job["apply_url"]

                if state and state.is_already_submitted(url, max_attempts=max_attempts):
                    print(f"[Stage 3] Skipping already handled job: {job['title']} @ {job['company']}")
                    continue

                # A dry run submits nothing, so the submission budget does not apply to it.
                blocked = None if is_dry_run else limiter.blocked_reason(url)
                if blocked == "daily":
                    print("[Stage 3] Daily application limit reached. Stopping for this cycle.")
                    break
                if blocked == "domain":
                    domain = extract_domain(url)
                    if domain not in limited_domains:
                        limited_domains.add(domain)
                        print(f"[Stage 3] Hourly limit reached for {domain}. Skipping its remaining jobs this cycle.")
                    continue

                handler_cls = pick_handler(url)
                if handler_cls is AgentHandler and not agent_cfg.get("enabled", True):
                    handler_cls = FallbackHandler               # agent switched off: unknown sites are refused, not guessed at
                handler = handler_cls(session)
                handler.agent_cfg, handler.llm_cfg = agent_cfg, llm_cfg

                app_dir = Path("applications") / _safe_dirname(job_id)
                app_dir.mkdir(parents=True, exist_ok=True)
                if solver:
                    solver.begin_job(app_dir)
                    handler.solver = solver
                handler.before_fill = fit_gate
                handler.trace_dir = app_dir / "trace"

                print(f"\n[Stage 3] Processing: {job['title']} @ {job['company']} via {handler.name}")

                # A dry run must not touch application state: it would consume retry attempts and
                # could block the real run later. It is recorded in the events log instead.
                tx_ctx = state.transaction(job_id, run_id or 1, mode) if (state and not is_dry_run) else None
                tx = None
                submit_attempted = False
                try:
                    if tx_ctx:
                        tx = tx_ctx.__enter__()

                    handler.prepare(
                        profile,
                        job,
                        checkpoint_callback=(lambda step: tx.checkpoint(step)) if tx else None,
                    )

                    # Save screenshot
                    screenshot_bytes = session.screenshot()
                    if screenshot_bytes:
                        (app_dir / "form_snapshot.png").write_bytes(screenshot_bytes)

                    if is_dry_run:
                        print(f"[Stage 3] [DRY RUN] Completed form prep for {job['title']}. Screenshot saved.")
                        if state and probation["enabled"]:
                            _count_clean_run(state, session.current_url(), job_id, probation)
                        _handle_recruiter_outreach(session, job, profile, state, job_id, app_dir, cfg, is_dry_run=True)
                        if state:
                            state.log_event(
                                "INFO",
                                "stage3",
                                "dry_run_prepared",
                                json.dumps({"title": job["title"], "company": job["company"], "ats": handler.name, "url": url}),
                                run_id=run_id,
                                job_id=job_id,
                            )
                        continue

                    if mode == "assisted":
                        # Human approval hook
                        user_ok = input(f"Form pre-filled for {job['title']} @ {job['company']}. Submit? [y/N/s]: ").strip().lower()
                        if user_ok != "y":
                            if tx:
                                tx.mark("skipped", ats=handler.name)
                            continue
                    else:
                        print(f"[Stage 3] Autonomous submit triggered for {job['title']}...")

                    site_key = site_fingerprint(session.current_url())
                    if state and probation["enabled"] and not state.site_is_trusted(site_key, probation["clean_runs_to_trust"]):
                        _hold_for_probation(state, tx, handler, job, job_id, app_dir, site_key, session.current_url(), probation)
                        continue

                    baseline_url = session.current_url()
                    submit_attempted = True
                    res = handler.submit()
                    if not res.ok:
                        # Nothing was clicked (e.g. the posting sends you to the employer's own site).
                        submit_attempted = False
                        print(f"[Stage 3] No automated submit available for {job['title']} ({res.raw.get('status')}). Parked for manual review.")
                        if tx:
                            tx.mark("needs_review", ats=handler.name, error=f"no automated submit: {res.raw.get('status', 'unknown')}")
                        continue

                    outcome, evidence = _resolve_outcome(session, handler, job, res, baseline_url, confirm_timeout)
                    if outcome == "rejected":
                        # The site refused the submit (visible form errors): nothing was sent, a human has to look.
                        submit_attempted = False
                        print(f"[Stage 3] The form rejected the submit for {job['title']}: {evidence}")
                        if tx:
                            tx.mark("needs_review", ats=handler.name, error=evidence[:300], error_code="rejected_by_form")
                        continue

                    print(f"[Stage 3] Application result: {outcome}" + (f" ({evidence})" if evidence else ""))
                    if tx:
                        tx.mark(outcome, ats=handler.name, ats_id=res.ats_id, error=evidence[:300] if outcome == "unconfirmed" else None)
                    limiter.record(url, job_id)
                    if outcome == "submitted" and state:
                        state.record_site_submission(site_key)         # proof that this kind of site works end to end
                    if outcome == "submitted":  # never tell a recruiter "I applied" for an unconfirmed attempt
                        _handle_recruiter_outreach(session, job, profile, state, job_id, app_dir, cfg, is_dry_run=False)

                except CaptchaEncountered:
                    print(f"[Stage 3] CAPTCHA detected for {job['title']} @ {job['company']}. Marking skipped.")
                    if tx:
                        tx.mark("skipped", ats=handler.name, error="CAPTCHA encountered")
                except LoginRequired:
                    print(f"[Stage 3] Login required for {job['title']} @ {job['company']}. Marking skipped.")
                    if tx:
                        tx.mark("skipped", ats=handler.name, error="Login required")
                except DuplicateApplication:
                    print(f"[Stage 3] Duplicate detected for {job['title']} @ {job['company']}.")
                    if tx:
                        tx.mark("duplicate", ats=handler.name)
                except PostingClosed as e:
                    print(f"[Stage 3] {job['title']} @ {job['company']}: {e}")
                    if tx:
                        tx.mark("skipped", ats=handler.name, error=str(e)[:300], error_code="closed")   # permanent: the posting is gone
                except NotAFit as e:
                    print(f"[Stage 3] Skipping {job['title']} @ {job['company']}: {e.summary}")
                    if tx:
                        # error_code makes this skip permanent (a CAPTCHA skip, which has none, stays retryable). A verdict is
                        # permanent; "the model could not be asked" is not a verdict, so that one is tried again next cycle.
                        unassessed = e.details.get("error") in ("llm_error", "review_unavailable")
                        tx.mark("skipped", ats=handler.name, error=e.summary[:300], **({} if unassessed else {"error_code": "not_a_fit"}))
                        if unassessed:
                            tx.refund_attempt()           # not the job's fault: a long outage must not block it for good
                    elif state:
                        state.log_event("INFO", "stage3", "dry_run_not_a_fit", json.dumps(e.details)[:2000], run_id=run_id, job_id=job_id)
                except FitDisputed as e:
                    print(f"[Stage 3] Parked {job['title']} @ {job['company']}: {e.summary}. Decide with: python fit_review.py list")
                    if tx:
                        tx.mark("needs_review", ats=handler.name, error=e.summary[:300], error_code="fit_disagreement")   # blocks retries until you decide
                    elif state:
                        state.log_event("INFO", "stage3", "dry_run_fit_disputed", json.dumps(e.details)[:2000], run_id=run_id, job_id=job_id)
                except NeedsHumanInput as e:
                    print(f"[Stage 3] {job['title']} @ {job['company']}: {e}. Parked; see: python answers.py pending")
                    if state:
                        state.add_pending_questions(job_id, e.questions)
                    if tx:
                        tx.mark("needs_review", ats=handler.name, error=str(e)[:300], error_code="needs_answers")
                    elif state:
                        state.log_event("INFO", "stage3", "dry_run_needs_answers", json.dumps(e.questions)[:2000], run_id=run_id, job_id=job_id)
                except Exception as e:
                    print(f"[Stage 3] Error during application: {e}")
                    if tx:
                        # Once the submit button may have been clicked the outcome is unknown, and an
                        # unknown outcome must block retries (unconfirmed) rather than invite a duplicate.
                        may_have_submitted = submit_attempted and not isinstance(e, UnsupportedForm)
                        tx.mark("unconfirmed" if may_have_submitted else "failed", ats=handler.name, error=str(e))
                finally:
                    if tx_ctx:
                        try:
                            tx_ctx.__exit__(None, None, None)
                        except Exception:
                            pass

                if hasattr(session, "restore_main"):
                    session.restore_main()
                if submit_attempted:
                    _pause_between_applications(rate_cfg, should_stop)
        finally:
            try:
                context.close()
            except Exception:
                pass
