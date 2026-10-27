"""Tests for StateStore, mutual exclusion locks, runs, and transactions."""

def test_daemon_lock_mutual_exclusion(tmp_state_store):
    store = tmp_state_store
    assert store.acquire_lock(pid=1001, host="node-1") is True
    # Second acquisition fails while heartbeat is active
    assert store.acquire_lock(pid=1002, host="node-2") is False

    store.release_lock(pid=1001)
    # Now second acquisition succeeds
    assert store.acquire_lock(pid=1002, host="node-2") is True


def test_runs_and_job_lifecycle(tmp_state_store):
    store = tmp_state_store
    run_id = store.start_run("stage1", "cfg_hash_1", "prof_hash_1")
    assert run_id > 0

    job = {
        "id": "job_42",
        "title": "Senior Backend Engineer",
        "company": "Acme",
        "apply_url": "https://acme.test/jobs/42",
        "score": 0.88,
    }
    store.save_job(job, run_id)

    pending = store.get_pending_jobs()
    assert len(pending) == 1
    assert pending[0]["id"] == "job_42"
    assert pending[0]["score"] == 0.88

    store.finish_run(run_id, "completed")


def test_application_transaction_and_checkpoints(tmp_state_store):
    store = tmp_state_store
    run_id = store.start_run("stage3", "cfg", "prof")
    job = {
        "id": "job_100",
        "title": "Staff Engineer",
        "company": "Globex",
        "apply_url": "https://globex.test/apply",
        "score": 0.95,
    }
    store.save_job(job, run_id)

    with store.transaction("job_100", run_id, "autonomous") as tx:
        tx.checkpoint("my_information")
        tx.checkpoint("my_experience")
        tx.mark("submitted", ats="workday", ats_id="WD-999")

    # Confirm submission reflected in idempotency check
    assert store.is_already_submitted("https://globex.test/apply") is True


def test_rate_limiting_counts(tmp_state_store):
    store = tmp_state_store
    assert store.count_hourly_domain_attempts("stripe.com") == 0
    assert store.count_daily_global_submits() == 0

    store.record_rate_event("stripe.com", "j1", "submit")
    store.record_rate_event("stripe.com", "j2", "submit")

    assert store.count_hourly_domain_attempts("stripe.com") == 2
    assert store.count_daily_global_submits() == 2
