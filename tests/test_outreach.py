"""Unit tests for recruiter discovery and personalized outreach."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from outreach import (
    RecruiterInfo,
    craft_outreach_message,
    extract_recruiter_from_job_page,
    generate_llm_outreach,
    send_recruiter_outreach,
)
from state import StateStore


def test_craft_outreach_message_length_and_fields() -> None:
    profile = {
        "name": "Alex Rivera Lopez",
        "first_name": "Alex",
        "skills": ["React", "TypeScript", "Python"],
    }
    job = {
        "title": "Senior Frontend Developer (React + TypeScript)",
        "company": "CLOUDSUFI",
    }
    msg = craft_outreach_message(profile, job, recruiter_name="Sam Taylor")
    assert "Sam" in msg
    assert "Senior Frontend Developer" in msg
    assert "CLOUDSUFI" in msg
    assert "React" in msg
    assert "Alex" in msg
    assert len(msg) <= 280, f"Message too long: {len(msg)} characters (must be <= 280 for LinkedIn 300 limit)"


def test_craft_outreach_message_truncation_for_very_long_inputs() -> None:
    profile = {
        "name": "Alex Rivera Lopez",
        "skills": ["ExtremelyLongEnterpriseTechnologyStackSpecializationFramework"],
    }
    job = {
        "title": "Super Mega Long Title " * 10,
        "company": "Super Giant Global Worldwide Enterprise Corporation Inc Limited",
    }
    msg = craft_outreach_message(profile, job, recruiter_name="Alexander-Montgomery-Wellington", max_chars=280)
    assert len(msg) <= 280


def test_state_outreach_tracking() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test_outreach_state.db"
        store = StateStore(db_path)

        assert store.get_outreach_count_today() == 0
        assert store.has_recruiter_been_contacted("https://www.linkedin.com/in/recruiter1") is False

        outreach_id = store.record_outreach(
            job_id="job_123",
            company="CLOUDSUFI",
            recruiter_name="Sam Taylor",
            recruiter_url="https://www.linkedin.com/in/recruiter1",
            message_text="Hi Sam, I applied for the Senior Frontend role.",
            channel="connection_note",
            status="drafted",
        )
        assert outreach_id > 0
        assert store.get_outreach_count_today() == 1
        assert store.has_recruiter_been_contacted("https://www.linkedin.com/in/recruiter1") is True
        assert store.get_total_outreaches() == 1

        store.close()


class MockLocator:
    def __init__(self, count_val=1, is_vis=True, text="", href=""):
        self._count = count_val
        self._is_vis = is_vis
        self._text = text
        self._href = href

    def count(self) -> int:
        return self._count

    def is_visible(self) -> bool:
        return self._is_vis

    def inner_text(self) -> str:
        return self._text

    def get_attribute(self, attr: str) -> str:
        if attr == "href":
            return self._href
        return ""

    def all(self) -> list[MockLocator]:
        if self._count > 0:
            return [self]
        return []

    @property
    def first(self) -> MockLocator:
        return self

    def click(self) -> None:
        pass

    def fill(self, text: str) -> None:
        pass

    def locator(self, sel: str) -> MockLocator:
        if "Message" in sel:
            return MockLocator(count_val=1, is_vis=True, text="Message")
        return MockLocator(count_val=0, is_vis=False)


class MockPage:
    def __init__(self):
        self.url = "https://www.linkedin.com/jobs/view/4473691580/"

    def evaluate(self, script: str) -> None:
        pass

    def locator(self, sel: str) -> MockLocator:
        if "a[href*='/in/']" in sel:
            return MockLocator(
                count_val=1,
                is_vis=True,
                text="Sam Taylor • 3rd\nTalent Acquisition Leader",
                href="https://www.linkedin.com/in/sam-taylor-0000001/?ref=jobs",
            )
        if "Message" in sel:
            return MockLocator(count_val=1, is_vis=True, text="Message")
        if "textbox" in sel or "message" in sel:
            return MockLocator(count_val=1, is_vis=True, text="")
        return MockLocator(count_val=0, is_vis=False)


class MockSession:
    def __init__(self):
        self.page = MockPage()

    def wait_for_timeout(self, ms: int) -> None:
        pass

    def goto(self, url: str) -> None:
        self.page.url = url

    def screenshot(self) -> bytes:
        return b"\x89PNG\r\n\x1a\n"


def test_extract_recruiter_from_job_page_mocked() -> None:
    session = MockSession()
    recruiter = extract_recruiter_from_job_page(session)
    assert recruiter is not None
    assert recruiter.name == "Sam Taylor"
    assert recruiter.profile_url == "https://www.linkedin.com/in/sam-taylor-0000001"
    assert recruiter.source == "job_page_poster"


def test_send_recruiter_outreach_dry_run() -> None:
    session = MockSession()
    recruiter = RecruiterInfo(
        name="Sam Taylor",
        title="Talent Acquisition Leader",
        profile_url="https://www.linkedin.com/in/sam-taylor-0000001",
        source="job_page_poster",
    )
    channel, status = send_recruiter_outreach(
        session,
        recruiter,
        message_text="Hi Sam, I recently applied for the role.",
        is_dry_run=True,
    )
    assert status == "drafted"
    assert channel == "message"


def test_generate_llm_outreach_mocked_and_fallback() -> None:
    # 1. Test offline/connection error fallback -> returns None
    result = generate_llm_outreach("prompt", endpoint="http://127.0.0.1:9999/v1", timeout_s=0.1)
    assert result is None

    # 2. Test successful mock response
    mock_resp_data = json.dumps({
        "choices": [
            {
                "message": {
                    "content": "Hi Sam, I applied for Senior Frontend role at CLOUDSUFI and would love to connect. Best, Alex."
                }
            }
        ]
    }).encode("utf-8")

    mock_resp = MagicMock()
    mock_resp.read.return_value = mock_resp_data
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        out = generate_llm_outreach("write a note", endpoint="http://127.0.0.1:8080/v1")
        assert out is not None
        assert "Hi Sam" in out
        assert len(out) <= 280

        # Also test craft_outreach_message using this LLM output
        profile = {"name": "Alex Rivera", "skills": ["React"]}
        job = {"title": "Senior Frontend Developer", "company": "CLOUDSUFI"}
        msg = craft_outreach_message(
            profile,
            job,
            recruiter_name="Sam Taylor",
            llm_config={"enabled": True, "endpoint": "http://127.0.0.1:8080/v1", "model": "deepseek-coder-v2-lite"},
        )
        assert msg == "Hi Sam, I applied for Senior Frontend role at CLOUDSUFI and would love to connect. Best, Alex."

    # 3. Test craft_outreach_message falls back to template/default when LLM is offline
    msg_fallback = craft_outreach_message(
        profile,
        job,
        recruiter_name="Sam Taylor",
        llm_config={"enabled": True, "endpoint": "http://127.0.0.1:9999/v1", "model": "deepseek-coder-v2-lite"},
    )
    assert "Sam" in msg_fallback
    assert "CLOUDSUFI" in msg_fallback
    assert len(msg_fallback) <= 280
