"""Probation: a kind of site is filled in and reviewed, but not submitted, until it has earned trust.

The live database holds zero confirmed submissions, so nothing about any site type has ever been proven.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from unittest.mock import patch

import sites as sites_cli
from llm_client import LLMError
from sites import site_fingerprint
from stage3_apply.run import stage3_run
from state import StateStore
from tests.test_p0_safety import PROFILE, app_row, cfg_for, fake_playwright, save_job, tmp_cwd
from tests.test_p2_flow import DynPage

GH = "https://boards.greenhouse.io/acme/jobs/{}"
ON = {"enabled": True, "clean_runs_to_trust": 3}


def run_once(tmp, store, rid, job, page, mode="autonomous", probation=None):
    cfg = cfg_for(tmp, mode)
    cfg["agent"] = {"probation": ON if probation is None else probation}
    with fake_playwright(page), patch("answerer.chat_json", side_effect=LLMError("model down")), patch("stage3_apply.run.wait_for_confirmation", return_value=False):
        stage3_run([job], dict(PROFILE, mode=mode), state=store, run_id=rid, config=cfg)


def fresh(tmp):
    store = StateStore(tmp / "s.db")
    return store, store.start_run("stage3", "h", "p")


def test_site_fingerprints_group_by_ats_platform_or_company_domain():
    cases = {
        "https://boards.greenhouse.io/acme/jobs/1": "greenhouse", "https://job-boards.greenhouse.io/acme": "greenhouse",
        "https://jobs.lever.co/acme/abc": "lever", "https://acme.wd1.myworkdayjobs.com/en-US/careers": "workday",
        "https://jobs.ashbyhq.com/acme/123": "ashby", "https://www.linkedin.com/jobs/view/1": "linkedin",
        "https://app.joinhandshake.com/jobs/1": "handshake", "https://careers.acme.com/jobs/7": "acme.com",
        "https://jobs.acme.co.uk/x": "acme.co.uk", "http://127.0.0.1:8123/apply/1": "127.0.0.1", "http://localhost:3000/x": "localhost", "http://[::1]:80/": "::1",
        "": "unknown", "not a url": "unknown",
    }
    for url, expected in cases.items():
        assert site_fingerprint(url) == expected, url


def test_trust_is_earned_by_approval_clean_runs_or_a_confirmed_submission():
    with tmp_cwd() as tmp:
        store, _ = fresh(tmp)
        assert store.site_is_trusted("greenhouse", 3) is False

        assert store.record_clean_run("greenhouse", "j1", 3) is False
        assert store.record_clean_run("greenhouse", "j1", 3) is False             # the same job again is not another clean run
        assert store.record_clean_run("greenhouse", "j2", 3) is False
        assert store.record_clean_run("greenhouse", "j3", 3) is True              # exactly one call reports the transition
        assert store.record_clean_run("greenhouse", "j4", 3) is False
        assert store.site_is_trusted("greenhouse", 3) is True and store.site_is_trusted("lever", 3) is False

        store.revoke_site("greenhouse")
        assert store.site_is_trusted("greenhouse", 3) is False                    # back to square one, clean runs forgotten too

        store.approve_site("lever")
        assert store.site_is_trusted("lever", 3) is True and store.site_is_trusted("lever", 0) is True

        store.record_site_submission("workday")
        assert store.site_is_trusted("workday", 3) is True                         # one confirmed application proves the flow

        for n in range(5):
            store.record_clean_run("ashby", f"a{n}", 0)
        assert store.site_is_trusted("ashby", 0) is False                          # 0 = only approval or a real submission can trust it
        store.close()


def test_an_untrusted_site_is_filled_in_and_reviewed_but_never_submitted():
    with tmp_cwd() as tmp:
        store, rid = fresh(tmp)
        job = save_job(store, rid, "g1", GH.format(1))
        page = DynPage(after_submit="success")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            run_once(tmp, store, rid, job, page)
        assert page.submit_clicks() == [] and not page.submitted, "an unproven site type was submitted to"
        row = app_row(store, "g1")
        assert (row["status"], row["error_code"]) == ("needs_review", "probation") and "sites.py approve greenhouse" in row["error_message"]
        bundle = json.loads((tmp / "applications" / "g1" / "probation.json").read_text(encoding="utf-8"))
        assert bundle["site"] == "greenhouse" and bundle["clean_runs"] == 1 and bundle["needed"] == 3 and "form_snapshot.png" in bundle["review"]
        assert (tmp / "applications" / "g1" / "form_snapshot.png").is_file()
        assert store.count_daily_global_submits() == 0 and "[Probation]" in out.getvalue()
        assert store.is_already_submitted(GH.format(1)) is True                    # parked, not retried every cycle
        store.close()


def test_three_clean_runs_earn_trust_release_the_parked_jobs_and_the_next_application_goes_through():
    with tmp_cwd() as tmp:
        store, rid = fresh(tmp)
        for n in (1, 2, 3):
            run_once(tmp, store, rid, save_job(store, rid, f"g{n}", GH.format(n), title=f"Role {n}", company=f"Co{n}"), DynPage(after_submit="success"))
            if n < 3:
                assert app_row(store, f"g{n}")["error_code"] == "probation"
        # the third clean run trusted the site and released everything that was waiting on it
        assert store.site_is_trusted("greenhouse", 3) is True
        assert all(app_row(store, f"g{n}") is None for n in (1, 2, 3)), "parked jobs must be released so they can be queued again"

        page = DynPage(after_submit="success")
        run_once(tmp, store, rid, save_job(store, rid, "g4", GH.format(4), title="Role 4", company="Co4"), page)
        assert page.submitted and app_row(store, "g4")["status"] == "submitted"
        assert store.conn.execute("SELECT confirmed_submissions FROM site_trust WHERE site_key = 'greenhouse'").fetchone()[0] == 1
        store.close()


def test_dry_runs_count_towards_trust_without_leaving_application_state():
    with tmp_cwd() as tmp:
        store, rid = fresh(tmp)
        for n in (1, 2, 3):
            run_once(tmp, store, rid, save_job(store, rid, f"d{n}", GH.format(n), title=f"Role {n}", company=f"Co{n}"), DynPage(), mode="dry_run")
        assert store.site_is_trusted("greenhouse", 3) is True
        assert store.conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0
        store.close()


def test_a_site_that_failed_to_prepare_cleanly_earns_nothing():
    with tmp_cwd() as tmp:
        store, rid = fresh(tmp)
        question = {"id": 1, "kind": "text", "label": "Do you hold an active clearance? *", "options": [], "required": True, "value": "", "input_type": "text"}
        for n in (1, 2, 3):
            run_once(tmp, store, rid, save_job(store, rid, f"p{n}", GH.format(n), title=f"Role {n}", company=f"Co{n}"), DynPage(questions=[question]), mode="dry_run")
        assert store.site_is_trusted("greenhouse", 3) is False                      # every run stopped on an unanswerable question
        store.close()


def test_probation_can_be_switched_off_and_a_confirmed_submission_trusts_a_site_type():
    with tmp_cwd() as tmp:
        store, rid = fresh(tmp)
        page = DynPage(after_submit="success")
        run_once(tmp, store, rid, save_job(store, rid, "o1", GH.format(1)), page, probation={"enabled": False})
        assert page.submitted and app_row(store, "o1")["status"] == "submitted"

        page2 = DynPage(after_submit="success")
        store.record_site_submission("greenhouse")
        run_once(tmp, store, rid, save_job(store, rid, "o2", GH.format(2), title="Other", company="Beta"), page2)
        assert page2.submitted and app_row(store, "o2")["status"] == "submitted"
        store.close()


def test_the_cli_lists_approves_and_revokes_and_approval_releases_parked_jobs():
    with tmp_cwd() as tmp:
        (tmp / "config.yaml").write_text(f'daemon:\n  state_db: "{(tmp / "s.db").as_posix()}"\n', encoding="utf-8")
        store, rid = fresh(tmp)
        run_once(tmp, store, rid, save_job(store, rid, "c1", GH.format(1)), DynPage(after_submit="success"))
        assert app_row(store, "c1")["error_code"] == "probation"
        store.close()

        def cli(*argv):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = sites_cli.main(["--config", str(tmp / "config.yaml"), *argv])
            return code, out.getvalue()

        code, out = cli("list")
        assert code == 0 and "greenhouse" in out and "probation" in out and "1/3" in out
        code, out = cli("approve", "Greenhouse")
        assert code == 0 and "1 job(s)" in out
        store = StateStore(tmp / "s.db")
        assert store.site_is_trusted("greenhouse", 3) is True and app_row(store, "c1") is None
        store.close()
        assert "trusted" in cli("list")[1]
        cli("revoke", "greenhouse")
        store = StateStore(tmp / "s.db")
        assert store.site_is_trusted("greenhouse", 3) is False
        store.close()
