"""Self-contained verification suite for JobStore, filtering, and adapters."""

import tempfile
from pathlib import Path

from applier import GreenhouseAdapter, LeverAdapter, pick_adapter
from scraper import filter_jobs
from store import JobStore


def test_store_idempotency():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test_state.db")
        store = JobStore(db_path)

        assert not store.is_seen("https://example.com/job/1")
        store.record("https://example.com/job/1", "Acme", "Dev", "applied")

        assert store.is_seen("https://example.com/job/1")
        assert store.get_status("https://example.com/job/1") == "applied"

        # Update status
        store.record("https://example.com/job/1", "Acme", "Dev", "skipped")
        assert store.get_status("https://example.com/job/1") == "skipped"

        store.close()


def test_filtering_and_scoring():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test_state.db")
        store = JobStore(db_path)

        profile = {
            "name": "Jane",
            "skills": ["python", "playwright"],
            "exclusions": {
                "companies": ["EvilCorp"],
                "keywords": ["unpaid", "commission only"],
            },
        }

        jobs = [
            {"id": "1", "company": "EvilCorp", "title": "Python Dev", "apply_url": "https://a.test/1"},
            {"id": "2", "company": "GoodCo", "title": "Unpaid Intern", "apply_url": "https://a.test/2"},
            {"id": "3", "company": "GoodCo", "title": "Senior Python Engineer", "content": "Uses playwright", "apply_url": "https://a.test/3"},
            {"id": "4", "company": "GoodCo", "title": "React Engineer", "content": "Frontend only", "apply_url": "https://a.test/4"},
        ]

        filtered = filter_jobs(jobs, profile, store)

        # 1 excluded by company, 2 excluded by keyword, 3 and 4 survive
        assert len(filtered) == 2
        assert filtered[0]["id"] == "3"  # Score: 2 (python + playwright)
        assert filtered[0]["score"] == 2
        assert filtered[1]["id"] == "4"  # Score: 0
        assert filtered[1]["score"] == 0

        # Mark 3 as seen; should now be filtered out
        store.record("https://a.test/3", "GoodCo", "Senior Python Engineer", "applied")
        filtered_again = filter_jobs(jobs, profile, store)
        assert len(filtered_again) == 1
        assert filtered_again[0]["id"] == "4"

        store.close()


def test_adapter_picker():
    assert pick_adapter("https://boards.greenhouse.io/stripe/jobs/12345") is GreenhouseAdapter
    assert pick_adapter("https://jobs.lever.co/netflix/abcdef-1234") is LeverAdapter
    assert pick_adapter("https://workday.com/job/1") is None


if __name__ == "__main__":
    print("Running tests...")
    test_store_idempotency()
    test_filtering_and_scoring()
    test_adapter_picker()
    print("All tests passed successfully!")
