"""Zero-dependency test runner using Python standard library."""

import sys
import tempfile
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from state import StateStore
from tests.conftest import FakeSession
from tests.test_profile_gen import (
    test_missing_resume_folder_is_reported_not_guessed,
    test_resume_folder_comes_from_the_environment,
    test_resume_parser_extracts_correct_fields,
    test_resume_parser_leaves_unstated_details_empty,
)
from tests.test_stage1 import (
    test_ast_security_policy,
    test_antibot_and_spa_detection,
    test_blocklist_enforcement,
    test_prompt_injection_sanitization,
    test_sandbox_and_fixture_regression,
)
from tests.test_stage2 import (
    test_deduplication_and_normalization,
    test_filters_and_scam_detection,
    test_stage2_pipeline_run,
)
from tests.test_stage3 import (
    test_ats_handler_resolution,
    test_confirmation_monitor,
    test_greenhouse_and_lever_form_preparation,
    test_handshake_quick_apply,
    test_linkedin_easy_apply_multistep,
    test_workday_login_and_captcha_detection,
    test_workday_multistep_autonomous_execution,
    test_persistent_context_and_langflow_components,
    test_firefox_browser_engine_configuration,
)
from tests.test_outreach import (
    test_craft_outreach_message_length_and_fields,
    test_craft_outreach_message_truncation_for_very_long_inputs,
    test_extract_recruiter_from_job_page_mocked,
    test_generate_llm_outreach_mocked_and_fallback,
    test_send_recruiter_outreach_dry_run,
    test_state_outreach_tracking,
)
from tests.test_state import (
    test_application_transaction_and_checkpoints,
    test_daemon_lock_mutual_exclusion,
    test_rate_limiting_counts,
    test_runs_and_job_lifecycle,
)
from tests.test_import_profile import (
    test_clean_stale_firefox_lock,
    test_find_system_firefox_profile_runs,
    test_safe_copy_sqlite_and_integrity,
    test_sync_system_firefox_profile_mocked,
)
from tests.test_browser_scraper import (
    test_parse_generic_career_links_fallback,
    test_parse_handshake_job_cards,
    test_parse_json_ld_job_posting,
    test_parse_linkedin_job_cards,
)


def get_sample_profile():
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


def main():
    print("=" * 60)
    print("Executing Auto Applier Test Suite (Python stdlib runner)")
    print("=" * 60)

    tests_run = 0

    # 1. Profile Generator Tests
    print("\n[Suite: Profile Generation]")
    test_resume_parser_extracts_correct_fields()
    print("  PASS: test_resume_parser_extracts_correct_fields")
    tests_run += 1
    test_resume_parser_leaves_unstated_details_empty()
    print("  PASS: test_resume_parser_leaves_unstated_details_empty")
    tests_run += 1
    test_resume_folder_comes_from_the_environment()
    print("  PASS: test_resume_folder_comes_from_the_environment")
    tests_run += 1
    test_missing_resume_folder_is_reported_not_guessed()
    print("  PASS: test_missing_resume_folder_is_reported_not_guessed")
    tests_run += 1

    # 2. State Store Tests
    print("\n[Suite: State Store & Mutual Exclusion]")
    with tempfile.TemporaryDirectory() as tmpdir:
        store = StateStore(Path(tmpdir) / "test.db")
        test_daemon_lock_mutual_exclusion(store)
        print("  PASS: test_daemon_lock_mutual_exclusion")
        tests_run += 1

        test_runs_and_job_lifecycle(store)
        print("  PASS: test_runs_and_job_lifecycle")
        tests_run += 1

        test_application_transaction_and_checkpoints(store)
        print("  PASS: test_application_transaction_and_checkpoints")
        tests_run += 1

        test_rate_limiting_counts(store)
        print("  PASS: test_rate_limiting_counts")
        tests_run += 1
        store.close()

    # 3. Stage 1 Tests
    print("\n[Suite: Stage 1 - Preflight & AST Sandbox]")
    test_blocklist_enforcement()
    print("  PASS: test_blocklist_enforcement")
    tests_run += 1

    test_antibot_and_spa_detection()
    print("  PASS: test_antibot_and_spa_detection")
    tests_run += 1

    test_prompt_injection_sanitization()
    print("  PASS: test_prompt_injection_sanitization")
    tests_run += 1

    test_ast_security_policy()
    print("  PASS: test_ast_security_policy")
    tests_run += 1

    test_sandbox_and_fixture_regression()
    print("  PASS: test_sandbox_and_fixture_regression")
    tests_run += 1

    # 4. Stage 2 Tests
    print("\n[Suite: Stage 2 - Filtering & Deduplication]")
    test_deduplication_and_normalization()
    print("  PASS: test_deduplication_and_normalization")
    tests_run += 1

    profile = get_sample_profile()
    test_filters_and_scam_detection(profile)
    print("  PASS: test_filters_and_scam_detection")
    tests_run += 1

    with tempfile.TemporaryDirectory() as tmpdir:
        store = StateStore(Path(tmpdir) / "test2.db")
        test_stage2_pipeline_run(store, profile)
        print("  PASS: test_stage2_pipeline_run")
        tests_run += 1
        store.close()

    # 5. Stage 3 Tests
    print("\n[Suite: Stage 3 - Multi-ATS Autonomous Handlers]")
    test_ats_handler_resolution()
    print("  PASS: test_ats_handler_resolution")
    tests_run += 1

    session = FakeSession()
    test_workday_multistep_autonomous_execution(profile, session)
    print("  PASS: test_workday_multistep_autonomous_execution")
    tests_run += 1

    session2 = FakeSession()
    test_workday_login_and_captcha_detection(profile, session2)
    print("  PASS: test_workday_login_and_captcha_detection")
    tests_run += 1

    session3 = FakeSession()
    test_greenhouse_and_lever_form_preparation(profile, session3)
    print("  PASS: test_greenhouse_and_lever_form_preparation")
    tests_run += 1

    session4 = FakeSession()
    test_confirmation_monitor(session4)
    print("  PASS: test_confirmation_monitor")
    tests_run += 1

    session_li = FakeSession()
    test_linkedin_easy_apply_multistep(profile, session_li)
    print("  PASS: test_linkedin_easy_apply_multistep")
    tests_run += 1

    session_hs = FakeSession()
    test_handshake_quick_apply(profile, session_hs)
    print("  PASS: test_handshake_quick_apply")
    tests_run += 1

    test_persistent_context_and_langflow_components(profile)
    print("  PASS: test_persistent_context_and_langflow_components")
    tests_run += 1

    test_firefox_browser_engine_configuration()
    print("  PASS: test_firefox_browser_engine_configuration")
    tests_run += 1

    # 6. Recruiter Outreach Tests
    print("\n[Suite: Recruiter Discovery & Outreach]")
    test_craft_outreach_message_length_and_fields()
    print("  PASS: test_craft_outreach_message_length_and_fields")
    tests_run += 1

    test_craft_outreach_message_truncation_for_very_long_inputs()
    print("  PASS: test_craft_outreach_message_truncation_for_very_long_inputs")
    tests_run += 1

    test_state_outreach_tracking()
    print("  PASS: test_state_outreach_tracking")
    tests_run += 1

    test_extract_recruiter_from_job_page_mocked()
    print("  PASS: test_extract_recruiter_from_job_page_mocked")
    tests_run += 1

    test_send_recruiter_outreach_dry_run()
    print("  PASS: test_send_recruiter_outreach_dry_run")
    tests_run += 1

    test_generate_llm_outreach_mocked_and_fallback()
    print("  PASS: test_generate_llm_outreach_mocked_and_fallback")
    tests_run += 1

    # 6. Firefox Session & Cookie Sync Tests
    print("\n[Suite: Firefox Session & Cookie Synchronization]")
    test_safe_copy_sqlite_and_integrity()
    print("  PASS: test_safe_copy_sqlite_and_integrity")
    tests_run += 1

    test_clean_stale_firefox_lock()
    print("  PASS: test_clean_stale_firefox_lock")
    tests_run += 1

    test_sync_system_firefox_profile_mocked()
    print("  PASS: test_sync_system_firefox_profile_mocked")
    tests_run += 1

    test_find_system_firefox_profile_runs()
    print("  PASS: test_find_system_firefox_profile_runs")
    tests_run += 1

    # 7. Universal Browser Job Scraper Tests
    print("\n[Suite: Universal Browser Scraper (LinkedIn / Handshake / JSON-LD)]")
    test_parse_json_ld_job_posting()
    print("  PASS: test_parse_json_ld_job_posting")
    tests_run += 1

    test_parse_linkedin_job_cards()
    print("  PASS: test_parse_linkedin_job_cards")
    tests_run += 1

    test_parse_handshake_job_cards()
    print("  PASS: test_parse_handshake_job_cards")
    tests_run += 1

    test_parse_generic_career_links_fallback()
    print("  PASS: test_parse_generic_career_links_fallback")
    tests_run += 1

    # 8. P0 Safety Regression Tests (dry-run authority, retry caps, upsert, daemon resilience, API auth)
    print("\n[Suite: P0 Safety Regressions]")
    from tests import test_p0_safety

    for name in sorted(n for n in vars(test_p0_safety) if n.startswith("test_")):
        getattr(test_p0_safety, name)()
        print(f"  PASS: {name}")
        tests_run += 1

    # 9. P1 Quality Regressions (descriptions, parsers, relevance gate, confirmation, outreach, LLM wiring)
    print("\n[Suite: P1 Quality Regressions]")
    from tests import test_p1_quality

    for name in sorted(n for n in vars(test_p1_quality) if n.startswith("test_")):
        getattr(test_p1_quality, name)()
        print(f"  PASS: {name}")
        tests_run += 1

    # 10. P2: confirmation evidence, form questions (real browsers), the answer engine, model fit scoring
    for title, module in (
        ("Form Handling in Real Browsers", "test_p2_forms"),
        ("Question Answering & Facts", "test_p2_answers"),
        ("LLM Client & Fit Scoring", "test_p2_fit"),
        ("Confirmation, Hooks & Stage 3 Flow", "test_p2_flow"),
        ("Site Trust & Probation", "test_p3_trust"),
        ("Dynamic Application Agent", "test_p3_agent"),
        ("Model Roles & Fallbacks", "test_p4_models"),
        ("Model Outages & Memory", "test_p4_outages"),
        ("Fit Veto Review", "test_p5_fit_review"),
        ("Terminal Dashboard (data and rendering)", "test_tui_data"),
        ("Location Filter", "test_p6_location"),
    ):
        print(f"\n[Suite: P2 {title}]")
        mod = __import__(f"tests.{module}", fromlist=[module])
        for name in sorted(n for n in vars(mod) if n.startswith("test_")):
            getattr(mod, name)()
            print(f"  PASS: {name}")
            tests_run += 1

    print("\n" + "=" * 60)
    print(f"SUCCESS: All {tests_run} test suites passed with 0 errors!")
    print("=" * 60)


if __name__ == "__main__":
    main()
