"""Pytest fixtures for Auto Applier test suite."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from state import StateStore

try:
    import pytest
except ImportError:
    class DummyPytest:
        @staticmethod
        def fixture(func):
            return func
    pytest = DummyPytest()


class FakeSession:
    def __init__(self, *, url: str = "https://example.com", present: set[str] | None = None, captcha: bool = False, login: bool = False):
        self.url = url
        self._present = set(present or [])
        self._captcha = captcha
        self._login = login
        self.actions: list[tuple[str, Any]] = []

    def goto(self, url: str, *, timeout_s: int = 30) -> None:
        self.url = url
        self.actions.append(("goto", url))

    def fill(self, sel: str, val: str) -> None:
        self.actions.append(("fill", sel, val))

    def select(self, sel: str, val: str) -> None:
        self.actions.append(("select", sel, val))

    def check(self, sel: str) -> None:
        self.actions.append(("check", sel))

    def upload(self, sel: str, path: str) -> None:
        self.actions.append(("upload", sel, path))

    def click(self, sel: str) -> None:
        self.actions.append(("click", sel))

    def wait_for(self, sel: str, *, timeout_s: int = 30) -> None:
        self.actions.append(("wait_for", sel))

    def wait_for_timeout(self, ms: int) -> None:
        self.actions.append(("wait_for_timeout", ms))

    def has(self, sel: str, wait_timeout_s: float = 0) -> bool:
        if sel.startswith("text="):
            txt = sel.split("text=", 1)[1]
            return any(txt in p for p in self._present)
        return sel in self._present

    def text(self, sel: str) -> str:
        return "Custom Label"

    def screenshot(self) -> bytes:
        return b"\x89PNG\r\n\x1a\n"

    def current_url(self) -> str:
        return self.url

    def requires_login(self) -> bool:
        return self._login

    def has_captcha(self) -> bool:
        return self._captcha


@pytest.fixture
def tmp_state_store(tmp_path):
    db_file = tmp_path / "test_state.db"
    store = StateStore(db_file)
    yield store
    store.close()


@pytest.fixture
def fake_session():
    return FakeSession()


@pytest.fixture
def sample_profile():
    return {
        "schema_version": "2.0",
        "name": "Alex Rivera Lopez",
        "first_name": "Alex",
        "last_name": "Rivera Lopez",
        "email": "alex.rivera@example.com",
        "phone": "+15555550123",
        "location": "Austin, Texas, USA",
        "skills": ["Python", "Docker", "API Design", "PostgreSQL", "React"],
        "seniority": "senior",
        "min_salary": 100000,
        "exclusions": {
            "companies": ["EvilCorp", "Initech"],
            "keywords": ["unpaid", "commission only"],
        },
        "attachments": {
            "resume_path": "/tmp/resume.pdf",
            "cover_template": "Hello hiring team!",
            "portfolio_url": "https://alex-rivera.example.com",
        },
    }
