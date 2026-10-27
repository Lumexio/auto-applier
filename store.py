"""SQLite idempotency store for job applications."""

import sqlite3
from datetime import datetime, timezone


class JobStore:
    def __init__(self, db_path: str = "state.db"):
        self.conn = sqlite3.connect(db_path)
        # ponytail: single table ledger; WAL daemon with runs/diagnostics only if multi-worker daemon needed
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS applications (
                url TEXT PRIMARY KEY,
                company TEXT,
                title TEXT,
                status TEXT CHECK (status IN ('pending', 'applied', 'skipped', 'failed')),
                applied_at TEXT
            )
        """)
        self.conn.commit()

    def is_seen(self, url: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM applications WHERE url = ?", (url,))
        return cur.fetchone() is not None

    def record(self, url: str, company: str, title: str, status: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT OR REPLACE INTO applications (url, company, title, status, applied_at) VALUES (?, ?, ?, ?, ?)",
            (url, company, title, status, now),
        )
        self.conn.commit()

    def get_status(self, url: str) -> str | None:
        cur = self.conn.execute("SELECT status FROM applications WHERE url = ?", (url,))
        row = cur.fetchone()
        return row[0] if row else None

    def close(self) -> None:
        self.conn.close()
