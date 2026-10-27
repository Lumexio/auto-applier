"""Main daemon loop, signal handling, and pipeline scheduler."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sys
import threading
import time
import traceback
import urllib.parse
from pathlib import Path
from typing import Any

from profile_gen import parse_resume_to_profile
from scraper import fetch_browser_jobs, fetch_greenhouse_jobs, fetch_lever_jobs
from stage2_filter.rank import stage2_run
from stage3_apply.run import stage3_run
from state import StateStore
from import_profile import sync_system_firefox_profile
from llm_client import check_health, detect_context_tokens


def load_yaml_config(path: str = "config.yaml") -> dict[str, Any]:
    cfg_file = Path(path)
    if not cfg_file.is_file():
        return {}
    try:
        import yaml
    except ImportError as e:
        # The old fallback silently ran a hard-coded config (scraping Stripe!) instead of the user's file.
        raise RuntimeError("PyYAML is required to read config.yaml. Install it with: pip install pyyaml") from e
    return yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}


def parse_ats_target(url: str) -> tuple[str, str] | None:
    """('greenhouse' | 'lever', company token) for a public job-board URL, or None.

    Understands both the API forms and the human-facing board URLs, e.g.
      https://boards-api.greenhouse.io/v1/boards/acme/jobs, https://boards.greenhouse.io/acme,
      https://job-boards.greenhouse.io/acme, https://api.lever.co/v0/postings/acme, https://jobs.lever.co/acme
    """
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    parts = [p for p in parsed.path.split("/") if p]

    if host.endswith("greenhouse.io"):
        token = None
        if "boards" in parts and parts.index("boards") + 1 < len(parts):
            token = parts[parts.index("boards") + 1]
        elif parts and parts[0] == "embed":
            token = (urllib.parse.parse_qs(parsed.query).get("for") or [None])[0]
        elif parts and host in ("boards.greenhouse.io", "job-boards.greenhouse.io"):
            token = parts[0]
        return ("greenhouse", token) if token else None

    if host.endswith("lever.co"):
        token = None
        if "postings" in parts and parts.index("postings") + 1 < len(parts):
            token = parts[parts.index("postings") + 1]
        elif parts and host.startswith("jobs."):
            token = parts[0]
        return ("lever", token) if token else None

    return None


def hash_dict(d: dict[str, Any]) -> str:
    blob = json.dumps(d, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def backoff_seconds(failures: int, interval: int, base: int = 60) -> int:
    """Delay before retrying after ``failures`` consecutive failed cycles.

    Doubles from ``base`` (60s, 120s, 240s, ...) and never exceeds one normal cycle interval, so a
    persistent fault is retried at the usual cadence instead of hammering sites or sleeping forever.
    """
    exponent = min(max(failures, 1) - 1, 20)
    return max(1, min(int(interval), base * 2**exponent))


class LoggerTee:
    """Duplicates stdout/stderr to a persistent log file with auto-flush."""
    def __init__(self, filename: str = "daemon.log"):
        self.terminal = sys.stdout
        self.log = open(filename, "a", encoding="utf-8", buffering=1)

    def write(self, message: str) -> None:
        if self.terminal:
            try:
                self.terminal.write(message)
                self.terminal.flush()
            except Exception:
                pass
        try:
            self.log.write(message)
            self.log.flush()
        except Exception:
            pass

    def flush(self) -> None:
        if self.terminal:
            try:
                self.terminal.flush()
            except Exception:
                pass
        try:
            self.log.flush()
        except Exception:
            pass


class Daemon:
    def __init__(self, config_path: str = "config.yaml", profile_path: str = "profile.json"):
        self.config = load_yaml_config(config_path)
        self.profile_path = Path(profile_path)
        db_path = self.config.get("daemon", {}).get("state_db", "state.db")
        self.state = StateStore(db_path)
        self.running = True
        self._stop = threading.Event()

    def probe_llm(self) -> bool:
        """Log whether the model is usable and measure its real context window (Ollama defaults to a tiny 2048 tokens)."""
        llm_cfg = self.config.setdefault("llm", {})
        ok, status = check_health(llm_cfg)
        print(f"[Daemon] LLM {'OK' if ok else 'WARNING'}: {status}")
        if ok:
            configured = int(llm_cfg.get("context_tokens", 2048))
            measured = detect_context_tokens(llm_cfg, probe_tokens=int(configured * 1.5))   # a probe bigger than the budget in use
            llm_cfg["effective_context_tokens"] = min(configured, measured) if measured else configured
            if measured and measured < 4096:
                print(
                    f"[Daemon] The model server's context window is only {measured} tokens. Prompts are sized to fit, but longer "
                    "postings and forms get trimmed; set OLLAMA_CONTEXT_LENGTH=8192 and llm.context_tokens: 8192 for better results."
                )
            print(f"[Daemon] LLM prompts are budgeted at {llm_cfg['effective_context_tokens']} tokens.")
        return ok

    def install_signals(self) -> None:
        def handler(sig, frame):
            if not self.running:
                # Second signal: stop waiting for the current step to finish.
                print("\n[Daemon] Second shutdown signal. Exiting immediately.")
                self.state.release_lock()
                sys.exit(1)
            # First signal: ask the loop to stop. The current application finishes (or is parked as
            # needs_review) instead of being cut off mid-form by an exception.
            print("\n[Daemon] Shutdown signal received. Finishing the current step, then exiting...")
            self.running = False
            self._stop.set()

        signal.signal(signal.SIGINT, handler)
        for name in ("SIGTERM", "SIGBREAK"):  # SIGBREAK: Ctrl+Break / console close on Windows
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), handler)

    def _sleep(self, seconds: float) -> None:
        """Interruptible sleep that keeps the lock heartbeat alive and wakes immediately on shutdown."""
        deadline = time.monotonic() + seconds
        while self.running:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if self._stop.wait(min(10.0, remaining)):
                break
            self.state.heartbeat()

    def run_forever(self, dry_run: bool = False) -> None:
        """Cycle until stopped. A failed cycle is logged and retried with backoff; it never ends the daemon."""
        interval = self.config.get("daemon", {}).get("interval_seconds", 3600)
        failures = 0
        while self.running:
            try:
                self.run_cycle(dry_run=dry_run)
                failures = 0
                wait = interval
            except Exception as e:
                failures += 1
                wait = backoff_seconds(failures, interval)
                print(f"[Daemon] Cycle failed ({failures} in a row): {e!r}")
                traceback.print_exc()
                print(f"[Daemon] Retrying in {wait}s.")
            self._sleep(wait)

    def load_or_generate_profile(self) -> dict[str, Any]:
        if not self.profile_path.is_file():
            print(f"[Daemon] {self.profile_path} not found. Generating from resume source of truth...")
            profile = parse_resume_to_profile()
            self.profile_path.write_text(json.dumps(profile, indent=2), encoding="utf-8")
            return profile
        return json.loads(self.profile_path.read_text(encoding="utf-8"))

    def run_cycle(self, dry_run: bool = False) -> None:
        profile = self.load_or_generate_profile()
        cfg_hash = hash_dict(self.config)
        prof_hash = hash_dict(profile)

        run_id = self.state.start_run("stage1", cfg_hash, prof_hash)
        print(f"\n=======================================================")
        print(f"[Daemon] Starting cycle (Run ID: {run_id}) at {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Candidate: {profile['name']} | Mode: {profile.get('mode', 'autonomous')}")
        print(f"=======================================================")

        try:
            # An application left in_progress by a dead process has an unknown outcome (it may have
            # been submitted). Park it for manual review; never retry it automatically.
            stale_minutes = self.config.get("stage3", {}).get("in_progress_stale_minutes", 10)
            parked = self.state.recover_stale_in_progress(stale_minutes)
            if parked:
                print(f"[Daemon] Parked {parked} interrupted application(s) as needs_review (outcome unknown, verify manually).")

            # Stage 1: Ingestion
            raw_jobs: list[dict[str, Any]] = []
            target_urls = self.config.get("stage1", {}).get("target_urls", [])

            for target in target_urls:
                if not self.running:
                    break
                self.state.heartbeat()
                print(f"[Stage 1] Ingesting postings from: {target}")
                ats = parse_ats_target(target)
                if ats and ats[0] == "greenhouse":
                    try:
                        raw_jobs.extend(fetch_greenhouse_jobs(ats[1]))
                    except Exception as e:
                        print(f"[Stage 1] Failed to fetch {target}: {e}")
                elif ats and ats[0] == "lever":
                    try:
                        raw_jobs.extend(fetch_lever_jobs(ats[1]))
                    except Exception as e:
                        print(f"[Stage 1] Failed to fetch {target}: {e}")
                elif "greenhouse.io" in target or "lever.co" in target:
                    # Never guess a company: the old code fell back to scraping Stripe / Netflix.
                    print(f"[Stage 1] Cannot tell which company board {target} points to. Skipping it.")
                else:
                    browser_cfg = self.config.get("browser", {})
                    b_type = browser_cfg.get("type", "firefox")
                    b_dir = browser_cfg.get("user_data_dir", "./.browser_profile")
                    try:
                        raw_jobs.extend(fetch_browser_jobs(target, user_data_dir=b_dir, browser_type=b_type))
                    except Exception as e:
                        print(f"[Stage 1] Failed to fetch {target}: {e}")

            print(f"[Stage 1] Ingestion complete. Scraped {len(raw_jobs)} total listings.")

            if not self.running:
                self.state.finish_run(run_id, "halted", error="shutdown requested")
                print(f"[Daemon] Cycle {run_id} halted by shutdown request.")
                return

            # Stage 2: Filtering & Ranking
            self.state.heartbeat()
            filtered_jobs, diagnostics = stage2_run(
                raw_jobs, profile, state=self.state, run_id=run_id, config=self.config
            )
            print(
                f"[Stage 2] Filtering complete. {len(filtered_jobs)} qualified jobs (filtered: {diagnostics.removed_exclusions} exclusions, {diagnostics.removed_duplicates} duplicates, {diagnostics.removed_scam} scams)."
            )

            # Stage 3: Autonomous Application
            self.state.heartbeat()
            run_config = self.config
            if dry_run:
                run_config = dict(self.config)
                run_config["stage3"] = dict(run_config.get("stage3", {}))
                run_config["stage3"]["mode"] = "dry_run"  # stage 3 treats dry_run from the config as authoritative
            stage3_run(
                filtered_jobs,
                profile,
                state=self.state,
                run_id=run_id,
                config=run_config,
                should_stop=lambda: not self.running,
                on_tick=self.state.heartbeat,
            )

            self.state.finish_run(run_id, "completed" if self.running else "halted")
            print(f"[Daemon] Cycle {run_id} {'completed successfully' if self.running else 'halted by shutdown request'}.")

        except Exception as e:
            print(f"[Daemon] Cycle {run_id} failed with error: {e}")
            self.state.finish_run(run_id, "failed", error=str(e))
            raise

    def start(self, once: bool = False, dry_run: bool = False) -> None:
        tee = LoggerTee("daemon.log")
        sys.stdout = tee
        sys.stderr = tee

        if not self.state.acquire_lock():
            print("[Daemon] Error: Another daemon instance is actively running. Exiting.")
            sys.exit(1)

        browser_cfg = self.config.get("browser", {})
        if browser_cfg.get("auto_sync_system_cookies") and browser_cfg.get("type", "firefox") == "firefox":
            target_profile = Path(browser_cfg.get("user_data_dir", "./.browser_profile")) / "firefox"
            sync_system_firefox_profile(target_dir=target_profile)

        self.install_signals()
        interval = self.config.get("daemon", {}).get("interval_seconds", 3600)

        self.probe_llm()

        try:
            # We hold the lock, so any run still marked 'running' belongs to a daemon that died.
            halted = self.state.reconcile_stale_runs()
            if halted:
                print(f"[Daemon] Marked {halted} run(s) from a previous crash as halted.")

            if once:
                self.run_cycle(dry_run=dry_run)
                return

            print(f"[Daemon] Daemon started. Cycle interval: {interval} seconds. Press Ctrl+C to stop.")
            self.run_forever(dry_run=dry_run)

        finally:
            self.state.release_lock()
            self.state.close()


def open_login_session(url: str = "", user_data_dir: str = "./.browser_profile", browser_type: str = "firefox") -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed. Run: pip install playwright && playwright install firefox")
        return

    b_type = browser_type.lower()
    profile_path = (Path(user_data_dir).resolve()) / b_type
    profile_path.mkdir(parents=True, exist_ok=True)
    try:
        from import_profile import clean_stale_firefox_lock
        clean_stale_firefox_lock(profile_path)
    except Exception:
        pass
    print(f"\n[Auth] Launching headed {b_type.capitalize()} browser using persistent profile at: {profile_path}")
    print("[Auth] Log into your target websites (Workday, Greenhouse, etc.) in the browser.")
    print("[Auth] All cookies, sessions, and tokens will be permanently saved for autonomous runs.\n")

    with sync_playwright() as p:
        launcher = getattr(p, b_type, p.firefox)
        context = launcher.launch_persistent_context(
            user_data_dir=str(profile_path),
            headless=False,
        )
        page = context.pages[0] if context.pages else context.new_page()
        if url:
            print(f"[Auth] Navigating to: {url}")
            page.goto(url)
        input(">>> When you have finished logging in, press [Enter] here to save session and close browser...")
        context.close()
    print(f"[Auth] {b_type.capitalize()} session saved successfully! Subsequent runs will be pre-authenticated.\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Auto Job Applier Daemon")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--profile", default="profile.json", help="Path to profile.json")
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit")
    parser.add_argument("--dry-run", action="store_true", help="Run in dry-run mode (no actual submissions)")
    parser.add_argument("--browser", choices=["firefox", "chromium", "webkit"], help="Browser engine override (default: from config or firefox)")
    parser.add_argument("--login", nargs="?", const="https://google.com", help="Launch browser with persistent profile to manually log into target sites")
    parser.add_argument("--sync-cookies", action="store_true", help="Sync cookies from system Firefox profile to Auto Applier browser profile")
    args = parser.parse_args()

    cfg = load_yaml_config(args.config)
    browser_cfg = cfg.get("browser", {})
    selected_browser = args.browser or browser_cfg.get("type", "firefox")
    cfg.setdefault("browser", {})["type"] = selected_browser

    if args.sync_cookies:
        browser_data = browser_cfg.get("user_data_dir", "./.browser_profile")
        target_dir = Path(browser_data) / selected_browser
        sync_system_firefox_profile(target_dir=target_dir)
        return

    if args.login:
        browser_data = browser_cfg.get("user_data_dir", "./.browser_profile")
        open_login_session(url=args.login, user_data_dir=browser_data, browser_type=selected_browser)
        return

    daemon = Daemon(config_path=args.config, profile_path=args.profile)
    daemon.config = cfg
    daemon.start(once=args.once, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
