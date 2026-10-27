"""Tests for Stage 2: Filtering, deduplication, scoring, and diagnostics."""

from stage2_filter.dedupe import canonicalize_url, dedupe, normalize_company
from stage2_filter.filters import is_excluded, is_scam, salary_ok
from stage2_filter.rank import score_job, stage2_run


def test_deduplication_and_normalization():
    assert normalize_company("Acme Inc.") == "acme"
    assert normalize_company("Globex Corp, LLC") == "globex"

    clean_url = canonicalize_url("https://jobs.test/1?utm_source=linkedin&ref=board&id=42")
    assert "utm_source" not in clean_url
    assert "ref" not in clean_url
    assert "id=42" in clean_url

    jobs = [
        {"company": "Acme Inc", "title": "Senior Dev", "apply_url": "https://a.test/1?utm=x"},
        {"company": "acme", "title": "Senior Dev", "apply_url": "https://a.test/1"},
        {"company": "Beta", "title": "Junior Dev", "apply_url": "https://b.test/2"},
    ]
    kept, dups = dedupe(jobs)
    assert len(kept) == 2
    assert dups == 1


def test_filters_and_scam_detection(sample_profile):
    excluded_job = {"company": "EvilCorp", "title": "Engineer", "apply_url": "https://e.test/1"}
    assert is_excluded(excluded_job, sample_profile) is True

    keyword_excluded = {"company": "GoodCo", "title": "Unpaid Intern", "apply_url": "https://g.test/1"}
    assert is_excluded(keyword_excluded, sample_profile) is True

    scam_job = {"title": "Assistant", "description": "Please wire transfer $50 for onboarding."}
    assert is_scam(scam_job) is True

    low_salary = {"salary_max": 60000}
    assert salary_ok(low_salary, sample_profile) is False


def test_stage2_pipeline_run(tmp_state_store, sample_profile):
    run_id = tmp_state_store.start_run("stage2", "cfg_hash")
    raw_jobs = [
        {"id": "j1", "company": "EvilCorp", "title": "Lead Dev", "apply_url": "https://a.test/1"},
        {"id": "j2", "company": "Acme", "title": "Senior Python Engineer", "description": "Docker and PostgreSQL", "apply_url": "https://a.test/2"},
        {"id": "j3", "company": "Acme Inc.", "title": "Senior Python Engineer", "description": "Docker and PostgreSQL", "apply_url": "https://a.test/2?utm=camp"},
        {"id": "j4", "company": "Globex", "title": "Wire Transfer Operator", "description": "Send money daily", "apply_url": "https://g.test/4"},
    ]

    filtered, diag = stage2_run(raw_jobs, sample_profile, state=tmp_state_store, run_id=run_id)

    # j1 excluded by company, j3 duplicate of j2, j4 excluded by scam
    assert len(filtered) == 1
    assert filtered[0]["id"] == "j2"
    assert filtered[0]["score"] > 0.5
    assert diag.removed_exclusions == 1
    assert diag.removed_duplicates == 1
    assert diag.removed_scam == 1
