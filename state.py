"""SQLite WAL state store with mutual exclusion locks, runs, jobs, and applications."""

from __future__ import annotations

import contextlib
import hashlib
import os
import socket
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

# An application in one of these states must never be started again automatically:
# the posting was (or may have been) applied to, or a human has to look at it first.
BLOCKING_STATUSES = ("submitted", "unconfirmed", "in_progress", "needs_review", "duplicate")
# These can be retried, but only up to `max_attempts` times.
RETRYABLE_STATUSES = ("failed", "skipped")
DEFAULT_MAX_ATTEMPTS = 3


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ApplicationTransaction:
    def __init__(self, state: StateStore, job_id: str, run_id: int, mode: str):
        self.state = state
        self.job_id = job_id
        self.run_id = run_id
        self.mode = mode

    def mark(
        self,
        status: str,
        ats: str | None = None,
        ats_id: str | None = None,
        error: str | None = None,
        error_code: str | None = None,
    ) -> None:
        now = utc_now()
        sub_at = now if status == "submitted" else None
        fin_at = now if status in ("submitted", "failed", "skipped", "duplicate") else None
        # Every transition *into* in_progress is one attempt; a retry also restarts the
        # staleness clock so an old row cannot look stale the moment it is retried.
        starts_attempt = 1 if status == "in_progress" else 0
        self.state.conn.execute(
            """
            INSERT INTO applications(job_id, run_id, status, mode, ats, ats_id, started_at, submitted_at, finished_at, error_code, error_message, attempts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                run_id = excluded.run_id,
                status = excluded.status,
                ats = coalesce(excluded.ats, applications.ats),
                ats_id = coalesce(excluded.ats_id, applications.ats_id),
                started_at = CASE WHEN excluded.status = 'in_progress' THEN excluded.started_at ELSE applications.started_at END,
                submitted_at = coalesce(excluded.submitted_at, applications.submitted_at),
                finished_at = excluded.finished_at,
                error_code = excluded.error_code,
                error_message = excluded.error_message,
                attempts = applications.attempts + ?
            """,
            (self.job_id, self.run_id, status, self.mode, ats, ats_id, now, sub_at, fin_at, error_code, error, starts_attempt, starts_attempt),
        )
        self.state.conn.commit()

    def checkpoint(self, step_name: str) -> None:
        self.state.conn.execute(
            "UPDATE applications SET checkpoint_step = ? WHERE job_id = ?",
            (step_name, self.job_id),
        )
        self.state.conn.commit()


class StateStore:
    def __init__(self, db_path: str | Path = "state.db", schema_path: str | Path | None = None):
        self.db_path = Path(db_path)
        schema_file = Path(schema_path) if schema_path else self.db_path.parent / "schema.sql"
        if not schema_file.is_file():
            schema_file = Path(__file__).parent / "schema.sql"

        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        if schema_file.is_file():
            self.conn.executescript(schema_file.read_text(encoding="utf-8"))
            self.conn.commit()
        self._migrate()

    # (table, column, DDL, optional one-off backfill). CREATE TABLE IF NOT EXISTS never alters an existing table,
    # so columns added after a database was first created are added here.
    _COLUMN_MIGRATIONS = (
        # Any application row that already exists was attempted at least once.
        ("applications", "attempts", "INTEGER NOT NULL DEFAULT 0", "UPDATE applications SET attempts = 1"),
        ("run_diagnostics", "removed_relevance", "INTEGER NOT NULL DEFAULT 0", None),
    )

    def _migrate(self) -> None:
        """Bring a database created by an older schema up to date."""
        for table, column, ddl, backfill in self._COLUMN_MIGRATIONS:
            cols = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            if cols and column not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                if backfill:
                    self.conn.execute(backfill)
        self.conn.commit()

    @classmethod
    def from_connection(cls, conn: sqlite3.Connection) -> StateStore:
        inst = cls.__new__(cls)
        inst.db_path = Path(":memory:")
        inst.conn = conn
        inst.conn.row_factory = sqlite3.Row
        return inst

    # ---- Mutual Exclusion Lock ----------------------------------------------

    def acquire_lock(self, pid: int | None = None, host: str | None = None) -> bool:
        pid = pid or os.getpid()
        host = host or socket.gethostname()
        now = utc_now()
        try:
            # Reclaim lock immediately if previous holder on this local host is no longer running
            if host == socket.gethostname():
                existing = self.conn.execute("SELECT holder_pid, holder_host FROM daemon_lock WHERE id = 1").fetchone()
                if existing and existing["holder_host"] == host and existing["holder_pid"] != pid:
                    holder_pid = existing["holder_pid"]
                    is_alive = False
                    try:
                        if os.name == "nt":
                            import ctypes
                            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, holder_pid)
                            if h:
                                ctypes.windll.kernel32.CloseHandle(h)
                                is_alive = True
                        else:
                            os.kill(holder_pid, 0)
                            is_alive = True
                    except Exception:
                        is_alive = False
                    if not is_alive:
                        self.conn.execute("DELETE FROM daemon_lock WHERE id = 1")
                        self.conn.commit()

            # Try insert or take over lock if heartbeat is older than 2 minutes
            cur = self.conn.cursor()
            cur.execute(
                """
                INSERT INTO daemon_lock(id, holder_pid, holder_host, acquired_at, heartbeat_at)
                VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    holder_pid   = excluded.holder_pid,
                    holder_host  = excluded.holder_host,
                    acquired_at  = excluded.acquired_at,
                    heartbeat_at = excluded.heartbeat_at
                WHERE datetime(daemon_lock.heartbeat_at) < datetime('now', '-2 minutes')
                """,
                (pid, host, now, now),
            )
            self.conn.commit()
            return cur.rowcount > 0
        except sqlite3.Error:
            return False

    def heartbeat(self, pid: int | None = None) -> bool:
        pid = pid or os.getpid()
        now = utc_now()
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE daemon_lock SET heartbeat_at = ? WHERE id = 1 AND holder_pid = ?",
            (now, pid),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def release_lock(self, pid: int | None = None) -> None:
        pid = pid or os.getpid()
        self.conn.execute("DELETE FROM daemon_lock WHERE id = 1 AND holder_pid = ?", (pid,))
        self.conn.commit()

    # ---- Runs ---------------------------------------------------------------

    def start_run(self, stage: str, config_hash: str, profile_hash: str | None = None) -> int:
        now = utc_now()
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO runs(started_at, status, stage, config_hash, profile_hash)
            VALUES (?, 'running', ?, ?, ?)
            """,
            (now, stage, config_hash, profile_hash),
        )
        self.conn.commit()
        return cur.lastrowid  # type: ignore

    def finish_run(self, run_id: int, status: str = "completed", error: str | None = None) -> None:
        now = utc_now()
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, status = ?, error_message = ? WHERE id = ?",
            (now, status, error, run_id),
        )
        self.conn.commit()

    def reconcile_stale_runs(self, keep_run_id: int | None = None) -> int:
        """Close runs left 'running' by a crash or kill. Only call while holding the daemon lock."""
        cur = self.conn.execute(
            """
            UPDATE runs
               SET status = 'halted', finished_at = ?, error_message = 'daemon stopped before this run finished'
             WHERE status = 'running' AND id IS NOT ?
            """,
            (utc_now(), keep_run_id),
        )
        self.conn.commit()
        return cur.rowcount

    # ---- Jobs ---------------------------------------------------------------

    def save_job(self, job: dict[str, Any], run_id: int) -> None:
        """Insert or update a posting without ever deleting its application history.

        Identity rules (this mutates ``job["id"]`` so stage 3 sees the same id that was stored):
          * the same posting (company + canonical URL) keeps the id it was first stored under;
          * a *different* posting that merely reuses an id (e.g. the "1".."10" counters the generic
            HTML parser hands out) is given a stable id derived from its URL instead of overwriting
            the first posting.
        """
        now = utc_now()
        company_norm = job.get("company_norm", job["company"].lower().strip())
        canon = job.get("apply_url_canon", job["apply_url"].split("?")[0])

        same = self.conn.execute(
            "SELECT id FROM jobs WHERE company_norm = ? AND apply_url_canon = ?", (company_norm, canon)
        ).fetchone()
        if same:
            job["id"] = same["id"]
        else:
            clash = self.conn.execute("SELECT apply_url_canon FROM jobs WHERE id = ?", (job["id"],)).fetchone()
            if clash and clash["apply_url_canon"] != canon:
                job["id"] = "u-" + hashlib.sha1(canon.encode("utf-8")).hexdigest()[:16]

        self.conn.execute(
            """
            INSERT INTO jobs(
                id, run_id, title, company, company_norm, location, remote, url,
                apply_url, apply_url_canon, posted_at, description, salary_min,
                salary_max, currency, source, score, reason, ats, status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                run_id          = excluded.run_id,
                title           = excluded.title,
                company         = excluded.company,
                company_norm    = excluded.company_norm,
                location        = excluded.location,
                remote          = excluded.remote,
                url             = excluded.url,
                apply_url       = excluded.apply_url,
                apply_url_canon = excluded.apply_url_canon,
                posted_at       = coalesce(excluded.posted_at, jobs.posted_at),
                description     = coalesce(nullif(excluded.description, ''), jobs.description),
                salary_min      = coalesce(excluded.salary_min, jobs.salary_min),
                salary_max      = coalesce(excluded.salary_max, jobs.salary_max),
                currency        = excluded.currency,
                source          = excluded.source,
                score           = excluded.score,
                reason          = excluded.reason,
                ats             = coalesce(excluded.ats, jobs.ats),
                updated_at      = excluded.updated_at
            """,
            (
                job["id"],
                run_id,
                job["title"],
                job["company"],
                company_norm,
                job.get("location"),
                1 if job.get("remote") else 0,
                job.get("url"),
                job["apply_url"],
                canon,
                job.get("posted_at"),
                job.get("description"),
                job.get("salary_min"),
                job.get("salary_max"),
                job.get("currency", "USD"),
                job.get("source", "unknown"),
                float(job.get("score", 0.0)),
                job.get("reason"),
                job.get("ats"),
                job.get("status", "pending"),
                now,
                now,
            ),
        )
        self.conn.commit()

    def get_pending_jobs(self) -> list[dict[str, Any]]:
        cur = self.conn.execute("SELECT * FROM jobs WHERE status = 'pending' ORDER BY score DESC")
        return [dict(row) for row in cur.fetchall()]

    def is_already_submitted(self, apply_url: str, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> bool:
        """True when this posting must not be applied to again automatically.

        Blocks on every outcome that is, or may be, a real submission (submitted, unconfirmed,
        in_progress, needs_review, duplicate) and on a deliberate skip: a human decline, or a "not a fit"
        decision (skipped + error_code). Plain failures and error-skips (CAPTCHA, login) stay retryable
        until ``max_attempts`` attempts were made.
        """
        canon = apply_url.split("?")[0]
        blocking = ",".join("?" for _ in BLOCKING_STATUSES)
        retryable = ",".join("?" for _ in RETRYABLE_STATUSES)
        cur = self.conn.execute(
            f"""
            SELECT 1 FROM jobs j
            JOIN applications a ON a.job_id = j.id
            WHERE (j.apply_url = ? OR j.apply_url_canon = ?)
              AND (
                    a.status IN ({blocking})
                 OR (a.status = 'skipped' AND (a.error_message IS NULL OR a.error_code IS NOT NULL))
                 OR (a.status IN ({retryable}) AND a.attempts >= ?)
              )
            LIMIT 1
            """,
            (apply_url, canon, *BLOCKING_STATUSES, *RETRYABLE_STATUSES, max_attempts),
        )
        return cur.fetchone() is not None

    def recover_stale_in_progress(self, minutes: int = 10) -> int:
        """Resolve applications whose process died mid-flight.

        The outcome of such an attempt is unknown (the form may well have been submitted), so it is
        parked as ``needs_review`` -- which blocks automatic retries -- instead of being retried.
        """
        cur = self.conn.execute(
            """
            UPDATE applications
               SET status = 'needs_review',
                   finished_at = ?,
                   error_message = 'interrupted: outcome unknown, verify manually'
             WHERE status = 'in_progress'
               AND datetime(started_at) < datetime('now', ?)
            """,
            (utc_now(), f"-{int(minutes)} minutes"),
        )
        self.conn.commit()
        return cur.rowcount

    # ---- Applications -------------------------------------------------------

    @contextlib.contextmanager
    def transaction(self, job_id: str, run_id: int, mode: str = "autonomous") -> Generator[ApplicationTransaction, None, None]:
        tx = ApplicationTransaction(self, job_id, run_id, mode)
        tx.mark("in_progress")
        try:
            yield tx
        except Exception as e:
            tx.mark("failed", error=str(e))
            raise

    # ---- Rate Limiting ------------------------------------------------------

    def record_rate_event(self, domain: str, job_id: str, event_type: str = "submit") -> None:
        self.conn.execute(
            "INSERT INTO rate_events(domain, job_id, event_at, event_type) VALUES (?, ?, ?, ?)",
            (domain, job_id, utc_now(), event_type),
        )
        self.conn.commit()

    def count_hourly_domain_attempts(self, domain: str) -> int:
        cur = self.conn.execute(
            """
            SELECT COUNT(*) FROM rate_events
            WHERE domain = ? AND datetime(event_at) > datetime('now', '-1 hour')
            """,
            (domain,),
        )
        return int(cur.fetchone()[0])

    def count_daily_global_submits(self) -> int:
        cur = self.conn.execute(
            """
            SELECT COUNT(*) FROM rate_events
            WHERE event_type = 'submit' AND datetime(event_at) > datetime('now', '-1 day')
            """
        )
        return int(cur.fetchone()[0])

    # ---- Stale In-Progress Recovery -----------------------------------------

    def get_stale_in_progress(self) -> list[dict[str, Any]]:
        cur = self.conn.execute("SELECT * FROM v_stale_in_progress")
        return [dict(row) for row in cur.fetchall()]

    def log_event(self, level: str, stage: str, event: str, payload: str = "", run_id: int | None = None, job_id: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO events(run_id, job_id, level, stage, event, payload, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, job_id, level, stage, event, payload, utc_now()),
        )
        self.conn.commit()

    # ---- Recruiter Outreach Tracking ----------------------------------------

    def record_outreach(
        self,
        job_id: str,
        company: str,
        recruiter_name: str,
        recruiter_url: str,
        message_text: str,
        channel: str,
        status: str,
    ) -> int:
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO recruiter_outreach(job_id, company, recruiter_name, recruiter_url, message_text, channel, status, sent_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (job_id, company, recruiter_name, recruiter_url, message_text, channel, status, utc_now()),
        )
        self.conn.commit()
        return cur.lastrowid  # type: ignore

    def get_outreach_count_today(self) -> int:
        cur = self.conn.execute(
            """
            SELECT COUNT(*) FROM recruiter_outreach
            WHERE status IN ('sent', 'drafted') AND datetime(sent_at) > datetime('now', '-1 day')
            """
        )
        return int(cur.fetchone()[0])

    # A recruiter we already reached, were already connected to, or who has a pending invitation is not contacted again.
    CONTACTED_STATUSES = ("sent", "drafted", "already_connected", "skipped")

    def has_recruiter_been_contacted(self, recruiter_url: str) -> bool:
        if not recruiter_url:
            return False
        marks = ",".join("?" for _ in self.CONTACTED_STATUSES)
        cur = self.conn.execute(
            f"SELECT COUNT(*) FROM recruiter_outreach WHERE recruiter_url = ? AND status IN ({marks})",
            (recruiter_url, *self.CONTACTED_STATUSES),
        )
        return int(cur.fetchone()[0]) > 0

    def get_total_outreaches(self) -> int:
        cur = self.conn.execute("SELECT COUNT(*) FROM recruiter_outreach WHERE status IN ('sent', 'drafted')")
        return int(cur.fetchone()[0])

    # ---- Answer bank & questions waiting for the user -------------------------

    def get_answer(self, question_key: str) -> str | None:
        row = self.conn.execute("SELECT answer FROM answer_bank WHERE question_key = ?", (question_key,)).fetchone()
        return row["answer"] if row else None

    def put_answer(self, question_key: str, question_text: str, answer: str, source: str = "user") -> None:
        self.conn.execute(
            """
            INSERT INTO answer_bank(question_key, question_text, answer, source, updated_at) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(question_key) DO UPDATE SET
                question_text = excluded.question_text, answer = excluded.answer,
                source = excluded.source, updated_at = excluded.updated_at
            """,
            (question_key, question_text, answer, source, utc_now()),
        )
        self.conn.commit()

    def add_pending_questions(self, job_id: str, questions: list[dict[str, Any]]) -> None:
        """Remember which required questions stopped this job (questions: dicts with label/kind/options/reason)."""
        from form_questions import normalize_key  # local import: form_questions has no dependency on state

        now = utc_now()
        for q in questions:
            key = normalize_key(q.get("label", ""))
            if not key:
                continue
            self.conn.execute(
                """
                INSERT INTO pending_questions(job_id, question_key, question_text, kind, options, reason, first_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id, question_key) DO UPDATE SET reason = excluded.reason
                """,
                (job_id, key, q.get("label", ""), q.get("kind"), "|".join(q.get("options") or []), q.get("reason", ""), now),
            )
        self.conn.commit()

    def list_pending(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT question_key, MIN(question_text) AS question_text, MIN(kind) AS kind, MIN(options) AS options,
                   MIN(reason) AS reason, COUNT(DISTINCT job_id) AS jobs
              FROM pending_questions GROUP BY question_key ORDER BY jobs DESC, question_key
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def resolve_pending(self, question_key: str) -> int:
        cur = self.conn.execute("DELETE FROM pending_questions WHERE question_key = ?", (question_key,))
        self.conn.commit()
        return cur.rowcount

    def jobs_ready_to_requeue(self) -> list[str]:
        """Jobs parked for missing answers whose every pending question has since been answered."""
        rows = self.conn.execute(
            """
            SELECT a.job_id FROM applications a
             WHERE a.status = 'needs_review' AND a.error_code = 'needs_answers'
               AND NOT EXISTS (SELECT 1 FROM pending_questions p WHERE p.job_id = a.job_id)
            """
        ).fetchall()
        return [r["job_id"] for r in rows]

    def requeue(self, job_id: str) -> bool:
        """Forget a needs_answers parking so the posting can be queued again (if it is still listed)."""
        cur = self.conn.execute(
            "DELETE FROM applications WHERE job_id = ? AND status = 'needs_review' AND error_code = 'needs_answers'", (job_id,)
        )
        self.conn.commit()
        return cur.rowcount > 0

    # ---- Site trust & probation -----------------------------------------------

    def site_is_trusted(self, site_key: str, clean_needed: int = 3) -> bool:
        """Trusted = approved by you, or one application there was confirmed, or enough clean runs (0 disables that route)."""
        row = self.conn.execute("SELECT approved, confirmed_submissions FROM site_trust WHERE site_key = ?", (site_key,)).fetchone()
        if row and (row["approved"] or row["confirmed_submissions"] > 0):
            return True
        if clean_needed <= 0:
            return False
        clean = self.conn.execute("SELECT COUNT(*) FROM site_clean_runs WHERE site_key = ?", (site_key,)).fetchone()[0]
        return clean >= clean_needed

    def record_clean_run(self, site_key: str, job_id: str, clean_needed: int = 3) -> bool:
        """Note that this site type was filled in cleanly for ``job_id``. True when that made it trusted for the first time."""
        was_trusted = self.site_is_trusted(site_key, clean_needed)
        self.conn.execute("INSERT OR IGNORE INTO site_clean_runs(site_key, job_id, run_at) VALUES (?, ?, ?)", (site_key, job_id, utc_now()))
        self.conn.commit()
        return not was_trusted and self.site_is_trusted(site_key, clean_needed)

    def _set_trust(self, site_key: str, approved: int) -> None:
        self.conn.execute(
            """
            INSERT INTO site_trust(site_key, approved, confirmed_submissions, updated_at) VALUES (?, ?, 0, ?)
            ON CONFLICT(site_key) DO UPDATE SET approved = excluded.approved, updated_at = excluded.updated_at
            """,
            (site_key, approved, utc_now()),
        )

    def approve_site(self, site_key: str) -> None:
        self._set_trust(site_key, 1)
        self.conn.commit()

    def revoke_site(self, site_key: str) -> None:
        """Back to probation: approval, confirmed submissions and the clean-run count are all forgotten."""
        self._set_trust(site_key, 0)
        self.conn.execute("UPDATE site_trust SET confirmed_submissions = 0 WHERE site_key = ?", (site_key,))
        self.conn.execute("DELETE FROM site_clean_runs WHERE site_key = ?", (site_key,))
        self.conn.commit()

    def record_site_submission(self, site_key: str) -> None:
        self.conn.execute(
            """
            INSERT INTO site_trust(site_key, approved, confirmed_submissions, updated_at) VALUES (?, 0, 1, ?)
            ON CONFLICT(site_key) DO UPDATE SET confirmed_submissions = confirmed_submissions + 1, updated_at = excluded.updated_at
            """,
            (site_key, utc_now()),
        )
        self.conn.commit()

    def release_probation(self, site_key: str) -> int:
        """Forget the probation parking of this site's jobs so they can be queued (and now submitted) again."""
        cur = self.conn.execute(
            """
            DELETE FROM applications
             WHERE status = 'needs_review' AND error_code = 'probation'
               AND job_id IN (SELECT job_id FROM site_clean_runs WHERE site_key = ?)
            """,
            (site_key,),
        )
        self.conn.commit()
        return cur.rowcount

    def list_sites(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT k.site_key,
                   COALESCE(t.approved, 0) AS approved,
                   COALESCE(t.confirmed_submissions, 0) AS confirmed_submissions,
                   (SELECT COUNT(*) FROM site_clean_runs c WHERE c.site_key = k.site_key) AS clean_runs,
                   (SELECT COUNT(*) FROM applications a JOIN site_clean_runs c ON c.job_id = a.job_id
                     WHERE c.site_key = k.site_key AND a.status = 'needs_review' AND a.error_code = 'probation') AS parked
              FROM (SELECT site_key FROM site_trust UNION SELECT site_key FROM site_clean_runs) k
              LEFT JOIN site_trust t ON t.site_key = k.site_key
             ORDER BY k.site_key
            """
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- Model fit assessments ------------------------------------------------

    def get_fit(self, job_id: str, cache_key: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM fit_scores WHERE job_id = ? AND cache_key = ?", (job_id, cache_key)).fetchone()
        return dict(row) if row else None

    def put_fit(self, job_id: str, cache_key: str, score: float | None, verdict: str, payload: str, model: str) -> None:
        self.conn.execute(
            """
            INSERT INTO fit_scores(job_id, cache_key, score, verdict, payload, model, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id, cache_key) DO UPDATE SET
                score = excluded.score, verdict = excluded.verdict, payload = excluded.payload,
                model = excluded.model, created_at = excluded.created_at
            """,
            (job_id, cache_key, score, verdict, payload, model, utc_now()),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
