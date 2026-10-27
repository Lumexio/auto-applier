"""Regression tests for the P1 quality fixes.

Fixtures mirror the *real* card text recorded in state.db / daemon.log (duplicated LinkedIn titles, Handshake
cards that concatenate employer + industry + title + pay, "Delivery Driver" and "Jobs"/"Preferences" junk), so
each test pins a failure that was seen in production:
  * descriptions were always empty (scrapers emitted `content`, stages 2/3 read `description`);
  * every LinkedIn job had company == title; nav links were scraped as jobs; Handshake titles were whole cards;
  * nothing stopped a senior backend profile applying to a delivery-driver internship;
  * URLs like /appliedintuition/... counted as "application submitted";
  * `channel='none'` crashed outreach, 'Pending' was read as "already connected", own profile could be picked;
  * the LLM config pointed at a port nothing listened on; an unparseable board URL silently scraped Stripe.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import job_text
import scraper
from daemon import Daemon, load_yaml_config, parse_ats_target
from llm_client import check_health
from outreach import RecruiterInfo, extract_recruiter_from_job_page, generate_llm_outreach, send_recruiter_outreach
from stage2_filter.dedupe import dedupe
from stage2_filter.normalize import normalize_job
from stage2_filter.rank import stage2_run
from stage2_filter.relevance import skill_in, title_relevance
from stage3_apply.confirm import looks_submitted, wait_for_confirmation
from stage3_apply.run import _pause_between_applications, stage3_run
from state import StateStore
from tests.test_p0_safety import PROFILE, GH_URL, _FakePage, app_row, cfg_for, fake_playwright, record, save_job, tmp_cwd

PROJECT_DIR = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
SENIOR_PROFILE = {
    "name": "Test User",
    "seniority": "senior",
    "skills": ["Python", "Docker", "PostgreSQL", "Node.js", "Git", "Java", "C/C++"],
}


# --------------------------------------------------------------------------- text helpers

def test_html_to_text_handles_escaped_and_real_markup():
    escaped = "&lt;div&gt;&lt;p&gt;Build &amp;amp; ship&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;li&gt;SQL&lt;/li&gt;&lt;/ul&gt;&lt;/div&gt;"   # how Greenhouse sends it
    assert job_text.html_to_text(escaped) == "Build & ship\nPython\nSQL"
    assert job_text.html_to_text("<p>One</p><script>evil()</script><p>Two&nbsp;words</p>") == "One\nTwo words"
    assert job_text.html_to_text(None) == "" and job_text.html_to_text("") == ""


def test_clean_title_unescapes_and_strips_card_metadata():
    assert job_text.clean_title("Data &amp; AI Engineer") == "Data & AI Engineer"
    assert job_text.clean_title("Web Developer $20-25/hr ∙ Part-time job Remote") == "Web Developer"
    assert job_text.clean_title("Staff Engineer • Austin, TX") == "Staff Engineer"
    assert job_text.clean_title("Backend Engineer Backend Engineer") == "Backend Engineer"
    assert job_text.clean_title("Part-Time Web Developer | AI Integration") == "Part-Time Web Developer | AI Integration"
    assert len(job_text.clean_title("x" * 500)) == 140


def test_relative_ages_become_timestamps():
    def days_ago(text):
        got = job_text.parse_posted_age(text, now=NOW)
        return None if got is None else (NOW - datetime.fromisoformat(got)).days

    assert days_ago("Posted 4 months ago") == 120
    assert days_ago("3wk ago") == 21 and days_ago("1mo ago") == 30 and days_ago("2 weeks ago") == 14
    assert days_ago("12 hours ago") == 0 and days_ago("5d ago") == 5 and days_ago("1 year ago") == 365
    assert days_ago("Chicago, IL") is None and days_ago("Oct 14–Jan 14") is None and days_ago("") is None


def test_salary_parsing_annualises_and_ignores_ambiguous_numbers():
    assert job_text.parse_salary("$25/hr - $30/hr") == (52000, 62400)
    assert job_text.parse_salary("$23.50/HR Apply Today $23–23.50/hr") == (48880, 48880)
    assert job_text.parse_salary("Part-time $4K/mo") == (48000, 48000)
    assert job_text.parse_salary("$120,000 - $150,000") == (120000, 150000)
    assert job_text.parse_salary("$100k to $150k a year") == (100000, 150000)
    assert job_text.parse_salary("pays $20") is None            # no unit, could be anything
    assert job_text.parse_salary("no money here") is None


# --------------------------------------------------------------------------- scrapers

REAL_LINKEDIN_CARD = """
<ul>
  <li><a href="/jobs/view/4471185828/?trackingId=x">
    <span aria-hidden="true">Senior Frontend Developer(React + TypeScript)</span>
    <span class="visually-hidden">Senior Frontend Developer(React + TypeScript)</span>
    <span>CLOUDSUFI</span><span>Guadalajara, JAL (On-site)</span><span>$25/hr - $30/hr</span>
    <span>You’d be a top applicant</span><span>Posted 1 week ago</span><span>1 week ago</span><span>Easy Apply</span>
  </a></li>
  <li><a href="/jobs/view/4471185829/">
    <span>Software Engineer (Verified job)</span><span>Software Engineer</span>
    <span>Aspida • Chicago, IL</span><span>Posted 3 weeks ago</span>
  </a></li>
</ul>
"""

PUBLIC_LINKEDIN_CARDS = """
<div class="base-card"><a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/4000000001/?refId=x"><span class="sr-only">Backend Engineer</span></a>
  <div class="info"><h3>Backend Engineer</h3><h4>Initech</h4><span>Chicago, IL</span><time>3 days ago</time></div></div>
<div class="base-card"><a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/4000000002/"><span class="sr-only">Platform Engineer</span></a>
  <div class="info"><h3>Platform Engineer</h3><h4>Globex</h4><span>Remote</span></div></div>
"""


def test_linkedin_cards_no_longer_use_the_title_as_the_company():
    jobs = scraper.parse_jobs_from_html(REAL_LINKEDIN_CARD, base_url="https://www.linkedin.com/jobs/")
    first, second = jobs
    assert (first["title"], first["company"], first["location"]) == ("Senior Frontend Developer(React + TypeScript)", "CLOUDSUFI", "Guadalajara, JAL (On-site)")
    assert (first["salary_min"], first["salary_max"]) == (52000, 62400)
    assert first["posted_at"] is not None and first["remote"] is False
    assert first["description"] == "" and first["content"] == ""      # cards carry no text, but the canonical key is always present
    # "(Verified job)" decoration is dropped, "Company • Location" is split, and "Chicago" is not mistaken for "...ago"
    assert (second["title"], second["company"], second["location"]) == ("Software Engineer", "Aspida", "Chicago, IL")


def test_linkedin_public_markup_reads_company_from_beside_the_link():
    a, b = scraper.parse_jobs_from_html(PUBLIC_LINKEDIN_CARDS, base_url="https://www.linkedin.com/jobs/search")
    assert (a["title"], a["company"], a["location"]) == ("Backend Engineer", "Initech", "Chicago, IL")
    assert a["posted_at"] is not None
    assert (b["title"], b["company"], b["remote"]) == ("Platform Engineer", "Globex", True)


def test_navigation_links_are_never_scraped_as_jobs_on_linkedin_or_handshake():
    # The three titles seen in production, plus links whose text is NOT on any blocklist: it is the page type
    # (LinkedIn / Handshake) that rules them out, not a list of known-bad titles.
    nav = (
        '<a href="/jobs/">Jobs</a><a href="/jobs/preferences/">Preferences</a><a href="/jobs/collections/recommended/">Show all</a>'
        '<a href="/jobs/collections/remote-jobs/">Remote Jobs Hiring Now</a><a href="/jobs/saved-searches/new">Create a search alert for Python</a>'
    )
    assert scraper.parse_jobs_from_html(nav, base_url="https://www.linkedin.com/jobs/") == []
    assert scraper.parse_jobs_from_html(nav, base_url="https://app.joinhandshake.com/job-search/") == []
    # ...whereas the same markup on an ordinary company site is treated as career links.
    assert len(scraper.parse_jobs_from_html(nav, base_url="https://example.com/company")) >= 2


def test_handshake_card_text_is_split_into_employer_and_title():
    card = (
        '<a href="/jobs/11111111?x=1"><div>Forward Logistix Partners Inc.</div><div>Transportation &amp; Logistics</div>'
        "<div>Delivery Driver (SEASONAL)</div><div>$23.50/HR Apply Today $23–23.50/hr</div>"
        "<div>Full-time job ∙ Oct 14–Jan 14</div><div>Promoted ∙ Manassas, Virginia, United States ∙ 3wk ago</div></a>"
    )
    (job,) = scraper.parse_jobs_from_html(card, base_url="https://app.joinhandshake.com/job-search/")
    assert (job["id"], job["title"], job["company"]) == ("11111111", "Delivery Driver (SEASONAL)", "Forward Logistix Partners Inc.")
    assert job["apply_url"] == "https://app.joinhandshake.com/jobs/11111111" and job["posted_at"] is not None
    assert job["salary_max"] == 48880

    # If the whole card arrives as one text node the title is at least cut off at the pay/metadata.
    one_node = '<a href="/jobs/22222222">McCurdy Real Estate &amp; Auction Real Estate Web Developer Intern $15–20/hr ∙ Internship Wichita ∙ 1mo ago</a>'
    (job,) = scraper.parse_jobs_from_html(one_node, base_url="https://app.joinhandshake.com/")
    assert job["title"] == "McCurdy Real Estate & Auction Real Estate Web Developer Intern"
    assert job["company"] == scraper.UNKNOWN_HANDSHAKE_COMPANY


def test_generic_career_links_get_stable_url_derived_ids():
    html = '<main><a href="/careers/infra-architect">Infra Architect</a><a href="/jobs/lead-designer">Lead Designer</a><a href="/about">About</a></main>'
    first = scraper.parse_jobs_from_html(html, base_url="https://example.com/company")
    second = scraper.parse_jobs_from_html(html, base_url="https://example.com/company")
    assert [j["id"] for j in first] == [j["id"] for j in second]
    assert all(len(j["id"]) == 12 and not j["id"].isdigit() for j in first)     # not the "1".."10" counters that collided across sites
    assert len({j["id"] for j in first}) == 2


def test_json_ld_postings_carry_description_date_salary_and_remote():
    ld = {
        "@type": "JobPosting", "title": "Dev", "url": "https://acme.test/jobs/1",
        "description": "&lt;p&gt;Build &amp;amp; ship&lt;/p&gt;", "datePosted": "2026-10-01", "jobLocationType": "TELECOMMUTE",
        "hiringOrganization": {"name": "Acme"},
        "baseSalary": {"currency": "USD", "value": {"minValue": 100000, "maxValue": 140000, "unitText": "YEAR"}},
    }
    html = f'<script type="application/ld+json">{json.dumps(ld)}</script>'
    (job,) = scraper.parse_jobs_from_html(html, base_url="https://acme.test/jobs")
    assert job["description"] == "Build & ship" and job["content"] == job["description"]
    assert (job["posted_at"], job["remote"], job["salary_min"], job["salary_max"]) == ("2026-10-01", True, 100000, 140000)


def test_greenhouse_and_lever_fetchers_return_descriptions_and_dates():
    gh_payload = {"jobs": [{
        "id": 7, "title": "Backend Engineer", "company_name": "Acme Corp", "absolute_url": "https://boards.greenhouse.io/acme/jobs/7",
        "location": {"name": "Remote - US"}, "content": "&lt;p&gt;Python and Docker&lt;/p&gt;", "first_published": "2026-10-20T10:00:00-04:00",
    }]}
    with patch.object(scraper, "_fetch_json", return_value=gh_payload) as fetch:
        (job,) = scraper.fetch_greenhouse_jobs("acme")
    assert "content=true" in fetch.call_args.args[0]          # without it Greenhouse sends no description at all
    assert (job["company"], job["description"], job["remote"], job["posted_at"]) == ("Acme Corp", "Python and Docker", True, "2026-10-20T10:00:00-04:00")

    lever_payload = [{
        "id": "abc", "text": "Platform Engineer", "applyUrl": "https://jobs.lever.co/netco/abc/apply", "categories": {"location": "Remote"},
        "descriptionPlain": "We build things.", "lists": [{"text": "Requirements", "content": "<li>Python</li><li>SQL</li>"}],
        "additionalPlain": "Great benefits.", "createdAt": 1760000000000, "workplaceType": "remote",
    }]
    with patch.object(scraper, "_fetch_json", return_value=lever_payload):
        (job,) = scraper.fetch_lever_jobs("netco")
    assert "We build things." in job["description"] and "Requirements" in job["description"]
    assert "Python" in job["description"] and "Great benefits." in job["description"]
    assert job["remote"] is True and job["posted_at"].startswith("2025-10-0")


# --------------------------------------------------------------------------- stage 2

def test_normalize_folds_content_into_description_and_cleans_titles():
    job = normalize_job({"title": "Data &amp; AI Engineer $50/hr", "company": "  Acme   Inc ", "content": "&lt;p&gt;Python rocks&lt;/p&gt;", "location": "Remote"})
    assert job["description"] == "Python rocks"                # stages 2/3 used to see an empty description
    assert (job["title"], job["company"], job["remote"]) == ("Data & AI Engineer", "Acme Inc", True)


def test_skill_matching_uses_word_boundaries():
    assert skill_in("we use git and docker", "Git") and not skill_in("digital marketing on github", "Git")
    assert skill_in("java developer", "Java") and not skill_in("javascript developer", "Java")
    assert skill_in("c++ and rust", "C/C++") and skill_in("built with nodejs", "Node.js") and skill_in("uses postgres", "PostgreSQL")
    assert not skill_in("postgresql", "SQL")


def test_title_gate_removes_the_junk_seen_in_production():
    drop = [
        "Delivery Driver (SEASONAL-FULL/PART TIME)", "AI-Native Web Intern", "Web Developer Intern", "Jobs", "Preferences", "Show all",
        "Senior Sales Engineer", "Junior Software Engineer", "Registered Nurse",
    ]
    keep = [
        "Full Stack Software Engineer (Founding Team)", "Software Engineer", "Senior Frontend Developer(React + TypeScript)",
        "Part-Time Web Developer | AI Integration", "Platform Engineer", "Lead Full-Stack Developer (TypeScript/React/Next.js)", "DevOps Lead",
    ]
    assert [t for t in drop if title_relevance({"title": t}, SENIOR_PROFILE)[0]] == []
    assert [t for t in keep if not title_relevance({"title": t}, SENIOR_PROFILE)[0]] == []

    # Seniority rule depends on the profile; the lists are overridable.
    assert title_relevance({"title": "Software Engineering Intern"}, {"seniority": "junior"})[0] is True
    assert title_relevance({"title": "Chef"}, {"target_titles": ["chef"], "title_exclusions": []})[0] is True
    assert title_relevance({"title": "Sales Engineer"}, {"title_exclusions": []})[0] is True


def test_stage2_pipeline_end_to_end_on_realistic_postings():
    rich = "We build backend services with Python, Docker and PostgreSQL deployed via Git. " * 4          # > 200 chars, 4 skills
    unrelated = "We are looking for someone to manage our office plants and answer the front desk phone. " * 4  # > 200 chars, 0 skills
    raw = [
        {"id": "1", "title": "Backend Engineer", "company": "Acme", "apply_url": "https://a.test/1", "content": rich},
        {"id": "2", "title": "Software Engineer", "company": "Plantco", "apply_url": "https://a.test/2", "content": unrelated},
        {"id": "3", "title": "Full Stack Engineer", "company": "LinkedIn Portal", "apply_url": "https://a.test/3"},          # card only
        {"id": "4", "title": "Delivery Driver", "company": "Handshake Network", "apply_url": "https://a.test/4"},
        {"id": "5", "title": "Web Developer Intern", "company": "Handshake Network", "apply_url": "https://a.test/5"},
        {"id": "6", "title": "Jobs", "company": "x", "apply_url": "https://a.test/6"},
        {"id": "7", "title": "Software Engineer", "company": "Stale", "apply_url": "https://a.test/7", "posted_at": (datetime.now(timezone.utc) - timedelta(days=120)).isoformat(), "content": rich},
    ]
    with tmp_cwd() as cwd, tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as db_dir:
        store = StateStore(Path(db_dir) / "s.db")
        rid = store.start_run("stage2", "h", "p")
        kept, diag = stage2_run(raw, dict(SENIOR_PROFILE), state=store, run_id=rid, config={})

        assert [j["id"] for j in kept] == ["1", "3"]
        assert kept[0]["score"] > kept[1]["score"] and "Python" in kept[0]["reason"]
        assert "title-only" in kept[1]["reason"]                                    # card-only postings are judged on the title alone
        assert (diag.removed_relevance, diag.removed_expired) == (4, 1)             # unrelated text, driver, intern, "Jobs"; stale 120 days

        row = store.conn.execute("SELECT removed_relevance FROM run_diagnostics WHERE run_id = ?", (rid,)).fetchone()
        assert row["removed_relevance"] == 4
        assert store.conn.execute("SELECT reason FROM jobs WHERE id = '1'").fetchone()["reason"].startswith("4/7 skills")
        assert store.conn.execute("SELECT description FROM jobs WHERE id = '1'").fetchone()["description"].startswith("We build backend")
        # the artifact is written beside the database, not into whatever directory the process happens to run in
        assert (Path(db_dir) / "jobs.json").is_file() and not (cwd / "jobs.json").exists()
        store.close()


def test_dedupe_distinguishes_locations_and_unknown_employers():
    jobs = [
        {"company": "Acme", "title": "Software Engineer", "location": "Austin, TX", "apply_url": "https://a.test/1"},
        {"company": "Acme", "title": "Software Engineer", "location": "Berlin", "apply_url": "https://a.test/2"},
        {"company": "Acme Inc", "title": "software engineer", "location": "Austin, TX", "apply_url": "https://a.test/3"},     # real duplicate
        {"company": "LinkedIn Portal", "title": "Data Engineer", "apply_url": "https://a.test/4"},
        {"company": "LinkedIn Portal", "title": "Data Engineer", "apply_url": "https://a.test/5"},                           # different employers
        {"company": "LinkedIn Portal", "title": "Data Engineer", "apply_url": "https://a.test/4?utm_source=x"},              # same URL
    ]
    kept, dups = dedupe(jobs)
    assert [j["apply_url"] for j in kept] == ["https://a.test/1", "https://a.test/2", "https://a.test/4", "https://a.test/5"]
    assert dups == 2


# --------------------------------------------------------------------------- confirmation

class _Browser:
    def __init__(self, url, present=()):
        self.url, self._present = url, set(present)

    def current_url(self):
        return self.url

    def has(self, sel, wait_timeout_s=0):
        return sel in self._present


def test_confirmation_needs_a_whole_path_segment_not_a_substring():
    for url in (
        "https://boards.greenhouse.io/appliedintuition/jobs/4001", "https://jobs.lever.co/successfactors-partners/abc-123",
        "https://boards.greenhouse.io/confirmio/jobs/77", "https://boards.greenhouse.io/thankful/jobs/12",
        "https://www.linkedin.com/jobs/applied-jobs/", "https://boards.greenhouse.io/acme/jobs/1?gh_src=confirmation",
    ):
        assert looks_submitted(url) is False, url
    for url in (
        "https://boards.greenhouse.io/acme/jobs/123/confirmation", "https://boards.greenhouse.io/thank-you",
        "https://acme.wd1.myworkdayjobs.com/en-US/site/job/Eng_R1/thank_you", "https://jobs.lever.co/acme/abc/thanks",
        "https://x.test/application-received", "https://x.test/apply/success",
    ):
        assert looks_submitted(url) is True, url


def test_a_confirmation_looking_url_only_counts_after_the_browser_moved_off_it():
    board_called_success = "https://boards.greenhouse.io/success/jobs/9"
    assert looks_submitted(board_called_success)                                       # the heuristic alone is fooled...
    assert wait_for_confirmation(_Browser(board_called_success), timeout_seconds=1, baseline_url=board_called_success) is False
    assert wait_for_confirmation(_Browser("https://boards.greenhouse.io/success/jobs/9/confirmation"), timeout_seconds=1, baseline_url=board_called_success) is True
    assert wait_for_confirmation(_Browser("https://x.test/form", present={"#application_confirmation"}), timeout_seconds=1, baseline_url="https://x.test/form") is True


# --------------------------------------------------------------------------- stage 3

def _autonomous(tmp, **extra_stage3):
    cfg = cfg_for(tmp, "autonomous")
    cfg["stage3"].update(extra_stage3)
    return cfg, dict(PROFILE, mode="autonomous")


def test_external_application_is_parked_for_review_not_reported_as_submitted():
    with tmp_cwd() as tmp:
        store = StateStore(tmp / "s.db")
        rid = store.start_run("stage3", "h", "p")
        url = "https://www.linkedin.com/jobs/view/4000000001"
        job = save_job(store, rid, "li1", url, title="Backend Engineer")
        cfg, profile = _autonomous(tmp)
        page = _FakePage(present=("button:has-text('Apply')",))      # an Apply button, but not Easy Apply
        with fake_playwright(page), patch("stage3_apply.run.wait_for_confirmation") as confirm:
            stage3_run([job], profile, state=store, run_id=rid, config=cfg)
        row = app_row(store, "li1")
        assert row["status"] == "needs_review" and "external_application_required" in row["error_message"]
        assert not confirm.called, "waited for a confirmation of a submit that never happened"
        assert store.is_already_submitted(url) is True                   # and it is not retried
        assert store.count_daily_global_submits() == 0                   # no phantom submission counted against the budget
        store.close()


def test_a_full_domain_skips_only_that_domain_but_a_full_day_stops_everything():
    with tmp_cwd() as tmp:
        store = StateStore(tmp / "s.db")
        rid = store.start_run("stage3", "h", "p")
        li = save_job(store, rid, "li", "https://www.linkedin.com/jobs/view/1", title="Backend Engineer")
        gh = save_job(store, rid, "gh", GH_URL, title="Platform Engineer", company="Beta")
        for _ in range(5):
            store.record_rate_event("www.linkedin.com", "old", "submit")
        cfg, profile = _autonomous(tmp)

        with fake_playwright(_FakePage()), patch("stage3_apply.run.wait_for_confirmation", return_value=False):
            stage3_run([li, gh], profile, state=store, run_id=rid, config=cfg)
        assert app_row(store, "li") is None                 # LinkedIn is at its hourly cap...
        assert app_row(store, "gh")["status"] == "unconfirmed"   # ...which used to stop the whole batch before this job ran

        # Daily cap taken from stage3.rate_limit (the profile sets none): 6 submits so far today, cap 1.
        store2 = StateStore(tmp / "s2.db")
        rid2 = store2.start_run("stage3", "h", "p")
        gh2 = save_job(store2, rid2, "gh2", "https://boards.greenhouse.io/acme/jobs/2", title="Other")
        store2.record_rate_event("example.com", "old", "submit")
        cfg2, profile2 = _autonomous(tmp, rate_limit={"global_per_day": 1})
        with fake_playwright(_FakePage()), patch("stage3_apply.run.wait_for_confirmation", return_value=False):
            stage3_run([gh2], profile2, state=store2, run_id=rid2, config=cfg2)
        assert app_row(store2, "gh2") is None
        store.close()
        store2.close()


def test_recruiter_outreach_only_follows_a_confirmed_submission():
    with tmp_cwd() as tmp:
        store = StateStore(tmp / "s.db")
        rid = store.start_run("stage3", "h", "p")
        j1 = save_job(store, rid, "a", "https://boards.greenhouse.io/acme/jobs/1", title="Backend Engineer")
        j2 = save_job(store, rid, "b", "https://boards.greenhouse.io/acme/jobs/2", title="Platform Engineer", company="Beta")
        cfg, profile = _autonomous(tmp)
        with fake_playwright(_FakePage()), patch("stage3_apply.run.wait_for_confirmation", side_effect=[False, True]), \
                patch("stage3_apply.run._handle_recruiter_outreach") as outreach:
            stage3_run([j1, j2], profile, state=store, run_id=rid, config=cfg)
        assert [c.args[1]["title"] for c in outreach.call_args_list] == ["Platform Engineer"]   # not the unconfirmed one
        store.close()


def test_pacing_between_applications_is_random_within_bounds_and_interruptible():
    clock = [0.0]
    slept = []

    def fake_sleep(seconds):
        slept.append(seconds)
        clock[0] += seconds

    with patch("stage3_apply.run.time.monotonic", lambda: clock[0]), patch("stage3_apply.run.time.sleep", fake_sleep):
        _pause_between_applications({"min_delay_seconds": 5, "max_delay_seconds": 15}, None)
        assert 5 <= sum(slept) <= 15 + 1
        slept.clear()
        clock[0] = 0.0
        _pause_between_applications({}, None)                                    # nothing configured -> no delay
        assert slept == []
        _pause_between_applications({"min_delay_seconds": 30, "max_delay_seconds": 30}, lambda: True)   # shutdown requested
        assert slept == []


# --------------------------------------------------------------------------- daemon & LLM wiring

def test_board_urls_resolve_to_the_right_company_and_never_to_a_default():
    cases = {
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs": ("greenhouse", "acme"),
        "https://boards.greenhouse.io/acme": ("greenhouse", "acme"),
        "https://boards.greenhouse.io/acme/jobs/123": ("greenhouse", "acme"),
        "https://job-boards.greenhouse.io/acme": ("greenhouse", "acme"),
        "https://boards.greenhouse.io/embed/job_board?for=acme": ("greenhouse", "acme"),
        "https://api.lever.co/v0/postings/netco?mode=json": ("lever", "netco"),
        "https://jobs.lever.co/netco": ("lever", "netco"),
        "https://jobs.lever.co/netco/abc-123": ("lever", "netco"),
        "https://boards.greenhouse.io/": None,          # used to silently become "stripe"
        "https://jobs.lever.co/": None,                 # used to silently become "netflix"
        "https://www.linkedin.com/jobs/": None,
    }
    assert {u: parse_ats_target(u) for u in cases} == cases


def test_unresolvable_board_is_skipped_instead_of_scraping_another_company():
    with tmp_cwd() as tmp:
        cfg = tmp / "config.yaml"
        cfg.write_text(
            f'daemon:\n  state_db: "{(tmp / "d.db").as_posix()}"\nstage1:\n  target_urls:\n'
            '    - "https://boards.greenhouse.io/"\n    - "https://boards.greenhouse.io/acme"\n    - "https://jobs.lever.co/netco"\n',
            encoding="utf-8",
        )
        (tmp / "profile.json").write_text(json.dumps(dict(PROFILE, mode="dry_run")), encoding="utf-8")
        d = Daemon(config_path=str(cfg), profile_path=str(tmp / "profile.json"))
        with patch("daemon.fetch_greenhouse_jobs", return_value=[]) as gh, patch("daemon.fetch_lever_jobs", return_value=[]) as lv, patch("daemon.stage3_run"):
            d.run_cycle(dry_run=True)
        assert [c.args for c in gh.call_args_list] == [("acme",)]
        assert [c.args for c in lv.call_args_list] == [("netco",)]
        d.state.close()


def test_config_loading_is_strict_and_the_llm_points_at_a_real_server():
    with tmp_cwd() as tmp:
        (tmp / "empty.yaml").write_text("", encoding="utf-8")
        assert load_yaml_config(str(tmp / "empty.yaml")) == {}         # used to crash Daemon.__init__ (None.get)
    llm = load_yaml_config(str(PROJECT_DIR / "config.yaml"))["llm"]
    assert ":11434" in llm["endpoint"] and llm["model"] == "qwen2.5:7b" and llm["timeout_seconds"] >= 30
    assert "pyyaml" in (PROJECT_DIR / "requirements.txt").read_text(encoding="utf-8").lower()


def test_llm_health_check_reports_each_failure_mode():
    cfg = {"endpoint": "http://127.0.0.1:11434/v1", "model": "qwen2.5:7b"}
    with patch("llm_client.list_models", return_value=["qwen2.5:7b"]):
        assert check_health(cfg)[0] is True
        assert check_health(dict(cfg, model="qwen2.5"))[0] is True                 # tag omitted
    with patch("llm_client.list_models", return_value=["llama3:8b"]):
        ok, msg = check_health(cfg)
        assert ok is False and "qwen2.5:7b" in msg and "llama3:8b" in msg
    with patch("llm_client.list_models", side_effect=ConnectionRefusedError()):
        ok, msg = check_health({"endpoint": "http://127.0.0.1:8080/v1", "model": "x"})
        assert ok is False and "not reachable" in msg and "8080" in msg


# --------------------------------------------------------------------------- outreach logic (feature stays disabled by default)

class _Count:
    def __init__(self, n):
        self.n = n

    def count(self):
        return self.n


class _Link:
    def __init__(self, href, text, in_chrome=False):
        self.href, self.text, self.in_chrome = href, text, in_chrome

    def get_attribute(self, attr):
        return self.href if attr == "href" else ""

    def inner_text(self):
        return self.text

    def locator(self, sel):
        return _Count(1 if (self.in_chrome and "ancestor::header" in sel) else 0)


class _Many:
    def __init__(self, items):
        self.items = items

    def all(self):
        return self.items


class _LinksPage:
    def __init__(self, links):
        self.links = links

    def evaluate(self, script):
        pass

    def locator(self, sel):
        return _Many(self.links) if "/in/" in sel else _Count(0)


class _Session:
    def __init__(self, page):
        self.page = page

    def wait_for_timeout(self, ms):
        pass

    def goto(self, url):
        self.page.url = url

    def screenshot(self):
        return b"png"


def test_recruiter_pick_skips_the_applicants_own_profile_and_site_chrome():
    own = "https://www.linkedin.com/in/alex-rivera-lopez/"
    links = [
        _Link(own + "?miniProfileUrn=x", "Alex Rivera Lopez\nSoftware Engineer"),
        _Link("https://www.linkedin.com/in/some-menu-person/", "Menu Person\nHead of Nav", in_chrome=True),
        _Link("https://www.linkedin.com/in/sam-taylor-1/?ref=jobs", "Sam Taylor • 3rd\nTalent Acquisition"),
    ]
    session = _Session(_LinksPage(links))
    assert extract_recruiter_from_job_page(session).name == "Alex Rivera Lopez"          # the old behaviour
    picked = extract_recruiter_from_job_page(session, exclude_urls=[own])
    assert (picked.name, picked.profile_url) == ("Sam Taylor", "https://www.linkedin.com/in/sam-taylor-1")


class _Loc:
    def __init__(self, page, sel, n):
        self.page, self.sel, self.n = page, sel, n

    def count(self):
        return self.n

    def is_visible(self):
        return self.n > 0

    @property
    def first(self):
        return self

    def click(self):
        if "button:has-text('Connect')" in self.sel:
            self.page.connected = True

    def fill(self, text):
        pass


class _ProfilePage:
    """LinkedIn profile with a configurable set of buttons; Connect can flip the button to 'Pending'."""

    url = "https://www.linkedin.com/in/sam-taylor-1"

    def __init__(self, connect=False, pending=False, pending_after_connect=False, boom=False):
        self.connect, self.pending, self.pending_after_connect, self.boom, self.connected = connect, pending, pending_after_connect, boom, False

    def locator(self, sel):
        if self.boom:
            raise RuntimeError("page crashed")
        if "button:has-text('Pending')" in sel:
            n = 1 if (self.pending or (self.pending_after_connect and self.connected)) else 0
        elif "button:has-text('Connect')" in sel and "menu" not in sel:
            n = 1 if self.connect else 0
        else:
            n = 0
        return _Loc(self, sel, n)


RECRUITER = RecruiterInfo(name="Sam Taylor", title="TA", profile_url="https://www.linkedin.com/in/sam-taylor-1", source="job_page_poster")


def test_outreach_outcomes_always_fit_the_schema_and_are_classified_correctly():
    def run(**page_kwargs):
        return send_recruiter_outreach(_Session(_ProfilePage(**page_kwargs)), RECRUITER, "Hi!", is_dry_run=False)

    assert run(pending=True) == ("connection_note", "skipped")                 # invitation already pending: do not click Connect again
    assert run() == ("inmail", "inmail_locked")                                # nothing to click
    assert run(boom=True) == ("message", "failed")
    assert run(connect=True, pending_after_connect=True) == ("connection_note", "failed")   # Connect sent a bare invite: say so

    with tmp_cwd() as tmp:
        store = StateStore(tmp / "s.db")
        for channel, status in (run(), run(boom=True), run(pending=True)):
            store.record_outreach("j", "Acme", "Sam", "https://www.linkedin.com/in/sam-taylor-1", "m", channel, status)   # used to raise IntegrityError
        try:
            store.record_outreach("j", "Acme", "Sam", "u", "m", "none", "failed")
            raise AssertionError("the schema must keep rejecting channel='none'")
        except sqlite3.IntegrityError:
            pass
        store.close()


def test_a_recruiter_we_already_reached_or_are_pending_with_is_not_contacted_again():
    with tmp_cwd() as tmp:
        store = StateStore(tmp / "s.db")
        url = lambda n: f"https://www.linkedin.com/in/r{n}"  # noqa: E731
        for n, status in enumerate(("sent", "drafted", "already_connected", "skipped", "failed", "inmail_locked", "limit_reached")):
            store.record_outreach("j", "Acme", "R", url(n), "m", "connection_note", status)
        contacted = {status: store.has_recruiter_been_contacted(url(n)) for n, status in enumerate(("sent", "drafted", "already_connected", "skipped", "failed", "inmail_locked", "limit_reached"))}
        assert contacted == {"sent": True, "drafted": True, "already_connected": True, "skipped": True,
                             "failed": False, "inmail_locked": False, "limit_reached": False}
        store.close()


def test_model_written_notes_with_links_or_addresses_are_rejected():
    def reply(text):
        resp = MagicMock()
        resp.read.return_value = json.dumps({"choices": [{"message": {"content": text}}]}).encode()
        resp.__enter__.return_value = resp
        return resp

    for bad in ("Hi! See http://evil.example/cv", "Write me at x@evil.example", "Visit www.evil.example"):
        with patch("urllib.request.urlopen", return_value=reply(bad)):
            assert generate_llm_outreach("p") is None, bad
    with patch("urllib.request.urlopen", return_value=reply("Hi Sam,\n  great to\nconnect.")):
        assert generate_llm_outreach("p") == "Hi Sam, great to connect."


def test_a_database_from_before_this_release_gets_the_new_diagnostics_column():
    import re

    old_schema = re.sub(r"\n\s*removed_relevance\s+INTEGER NOT NULL DEFAULT 0,", "", (PROJECT_DIR / "schema.sql").read_text(encoding="utf-8"))
    assert old_schema != (PROJECT_DIR / "schema.sql").read_text(encoding="utf-8")
    with tmp_cwd() as tmp:
        db = tmp / "old.db"
        conn = sqlite3.connect(db)
        conn.executescript(old_schema)
        conn.commit()
        conn.close()

        store = StateStore(db)                                          # migrates in place
        rid = store.start_run("stage2", "h", "p")
        stage2_run([{"id": "1", "title": "Delivery Driver", "company": "X", "apply_url": "https://x.test/1"}], dict(SENIOR_PROFILE), state=store, run_id=rid, config={})
        assert store.conn.execute("SELECT removed_relevance FROM run_diagnostics WHERE run_id = ?", (rid,)).fetchone()[0] == 1
        store.close()


def test_pipeline_passes_the_applicants_own_profile_to_the_recruiter_lookup():
    from stage3_apply.run import _handle_recruiter_outreach

    state = MagicMock()
    state.get_outreach_count_today.return_value = 0
    with patch("outreach.extract_recruiter_from_job_page", return_value=None) as extract, \
            patch("outreach.search_company_recruiter", return_value=None) as search:
        _handle_recruiter_outreach(
            MagicMock(), {"company": "Acme"}, {"linkedin": "https://www.linkedin.com/in/me"}, state, "j", Path("."),
            {"outreach": {"enabled": True}}, is_dry_run=False,
        )
    assert extract.call_args.kwargs["exclude_urls"] == ["https://www.linkedin.com/in/me"]
    assert search.call_args.kwargs["exclude_urls"] == ["https://www.linkedin.com/in/me"]
