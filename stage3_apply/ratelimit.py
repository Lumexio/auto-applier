"""Sliding window rate limiter backed by SQLite state store."""

from __future__ import annotations

import urllib.parse
from typing import Any


def extract_domain(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return parsed.netloc.lower()


class RateLimiter:
    def __init__(
        self,
        state: Any = None,
        per_day: int = 25,
        per_domain_per_hour: int = 5,
    ) -> None:
        self.state = state
        self.per_day = per_day
        self.per_domain_per_hour = per_domain_per_hour
        # In-memory tracking fallback if state store is absent
        self._mem_events: list[tuple[str, str]] = []

    def blocked_reason(self, url: str) -> str | None:
        """None when the application may proceed, else 'daily' (global cap) or 'domain' (this site's hourly cap).

        The distinction matters to the caller: a full domain only blocks that site, a full day blocks everything.
        """
        domain = extract_domain(url)
        if self.state:
            if self.state.count_daily_global_submits() >= self.per_day:
                return "daily"
            if self.state.count_hourly_domain_attempts(domain) >= self.per_domain_per_hour:
                return "domain"
            return None

        # In-memory fallback
        if len(self._mem_events) >= self.per_day:
            return "daily"
        if sum(1 for d, _ in self._mem_events if d == domain) >= self.per_domain_per_hour:
            return "domain"
        return None

    def allow(self, url: str) -> bool:
        return self.blocked_reason(url) is None

    def record(self, url: str, job_id: str = "") -> None:
        domain = extract_domain(url)
        if self.state:
            self.state.record_rate_event(domain, job_id, "submit")
        else:
            self._mem_events.append((domain, job_id))
