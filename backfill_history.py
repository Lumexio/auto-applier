"""One-off repair: rebuild application history that the old `INSERT OR REPLACE` bug erased.

Every cycle the old stage 2 re-saved each queued job with INSERT OR REPLACE, which cascaded away that
job's `applications` row. Jobs that had already been attempted therefore looked brand new and were
queued again. daemon.log still records every attempt:

    [Stage 3] Processing: <title> @ <company> via <ats>
    [Stage 3] Application result: unconfirmed | submitted

so this restores a blocking `unconfirmed` (or `submitted`) row for each such job that has lost its
history. Nothing is retried automatically after that.

    python backfill_history.py            # dry run: read-only, prints the plan
    python backfill_history.py --apply    # write it (stop the daemon first)
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from state import BLOCKING_STATUSES, StateStore, utc_now

PROCESSING = re.compile(r"^\[Stage 3\] Processing: (?P<title>.+) @ (?P<company>.+) via (?P<ats>\w+)\s*$")
RESULT = re.compile(r"^\[Stage 3\] Application result: (?P<status>submitted|unconfirmed)\s*$")


def parse_log(text: str) -> dict[tuple[str, str], dict[str, Any]]:
    """(title, company) -> {"results": Counter(status), "ats": str} for every attempt that reached a result."""
    found: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {"results": Counter(), "ats": None})
    current: tuple[str, str, str] | None = None
    for line in text.splitlines():
        m = PROCESSING.match(line)
        if m:
            current = (m["title"].strip(), m["company"].strip(), m["ats"])
            continue
        r = RESULT.match(line)
        if r and current:
            entry = found[(current[0], current[1])]
            entry["results"][r["status"]] += 1
            entry["ats"] = current[2]
            current = None
    return dict(found)


def plan_backfill(conn: sqlite3.Connection, attempts: dict[tuple[str, str], dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows to (re)create. Read-only: works on a connection opened with mode=ro."""
    conn.row_factory = sqlite3.Row
    plan: list[dict[str, Any]] = []
    for (title, company), info in sorted(attempts.items()):
        total = sum(info["results"].values())
        status = "submitted" if info["results"]["submitted"] else "unconfirmed"
        for job in conn.execute("SELECT id, run_id, apply_url FROM jobs WHERE title = ? AND company = ?", (title, company)).fetchall():
            app = conn.execute("SELECT status FROM applications WHERE job_id = ?", (job["id"],)).fetchone()
            if app and app["status"] in BLOCKING_STATUSES:
                continue  # history survived
            plan.append(
                {
                    "job_id": job["id"],
                    "run_id": job["run_id"],
                    "title": title,
                    "company": company,
                    "apply_url": job["apply_url"],
                    "status": status,
                    "attempts": total,
                    "ats": info["ats"],
                    "previous": app["status"] if app else None,
                }
            )
    return plan


def apply_plan(state: StateStore, plan: list[dict[str, Any]]) -> None:
    now = utc_now()
    with state.conn:  # one transaction
        for p in plan:
            state.conn.execute(
                """
                INSERT INTO applications(job_id, run_id, status, mode, ats, started_at, submitted_at, finished_at, error_message, attempts)
                VALUES (?, ?, ?, 'autonomous', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    status = excluded.status,
                    ats = coalesce(excluded.ats, applications.ats),
                    submitted_at = coalesce(excluded.submitted_at, applications.submitted_at),
                    finished_at = excluded.finished_at,
                    error_message = excluded.error_message,
                    attempts = max(applications.attempts, excluded.attempts)
                """,
                (
                    p["job_id"],
                    p["run_id"],
                    p["status"],
                    p["ats"],
                    now,
                    now if p["status"] == "submitted" else None,
                    now,
                    f"backfilled from daemon.log: {p['attempts']} earlier attempt(s) ended '{p['status']}'",
                    p["attempts"],
                ),
            )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", default="daemon.log")
    ap.add_argument("--db", default="state.db")
    ap.add_argument("--apply", action="store_true", help="write the rows (default is a read-only dry run)")
    args = ap.parse_args(argv)

    log_path, db_path = Path(args.log), Path(args.db)
    if not log_path.is_file() or not db_path.is_file():
        print(f"Need both {log_path} and {db_path}.")
        return 1

    attempts = parse_log(log_path.read_text(encoding="utf-8", errors="replace"))
    ro = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        plan = plan_backfill(ro, attempts)
    finally:
        ro.close()

    print(f"daemon.log: {len(attempts)} postings with a recorded result; {len(plan)} have lost their history.")
    for p in plan:
        prev = f" (currently {p['previous']})" if p["previous"] else " (no row)"
        print(f"  {p['status']:11s} x{p['attempts']}  {p['title'][:55]} @ {p['company'][:28]}{prev}")
    if not plan:
        print("Nothing to do.")
        return 0
    if not args.apply:
        print("\nDry run. Re-run with --apply (daemon stopped) to write these rows.")
        return 0

    state = StateStore(db_path)
    try:
        if not state.acquire_lock():
            print("\nRefusing to write: the daemon is running (it holds the lock). Stop it first.")
            return 2
        try:
            apply_plan(state, plan)
        finally:
            state.release_lock()
    finally:
        state.close()
    print(f"\nWrote {len(plan)} row(s). They now block automatic retries.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
