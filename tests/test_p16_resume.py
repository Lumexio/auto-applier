"""Picking the resume that fits the job: keywords, the title first, the default whenever it is unclear. Fictional files only."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import resume_pick
from stage3_apply.ats_handlers import GreenhouseHandler
from stage3_apply.run import stage3_run
from state import StateStore
from tests.test_p0_safety import PROFILE, _FakePage, cfg_for, fake_playwright, save_job, tmp_cwd


def make_resumes(tmp: Path, *names: str) -> Path:
    folder = tmp / "resumes"
    folder.mkdir(exist_ok=True)
    for name in names:
        (folder / f"resume-{name}.pdf").write_bytes(b"%PDF-1.4 fictional")
    return folder


def profile_for(folder: Path, **attachments) -> dict:
    return {"name": "Test User", "attachments": {"resume_path": str((folder / "resume-base.pdf").resolve()), "cover_template": "Dear team", **attachments}}


def pick(profile, title, description=""):
    got = resume_pick.choose(profile, {"title": title, "description": description})
    return got[0] if got else None


def test_the_title_decides_and_anything_unclear_keeps_the_default():
    with tmp_cwd() as tmp:
        profile = profile_for(make_resumes(tmp, "base", "frontend", "backend"))
        assert pick(profile, "Senior Frontend Engineer") == "frontend" and pick(profile, "Front-End Developer") == "frontend"
        assert pick(profile, "Backend Engineer (Python)") == "backend" and pick(profile, "Platform Engineer") == "backend"
        assert pick(profile, "React Developer") == "frontend" and pick(profile, "Staff Software Engineer") is None
        assert pick(profile, "Full Stack Engineer - React and Python") is None and pick(profile, "Fullstack Developer") is None
        assert pick(profile, "Frontend and Backend Engineer") is None, "a tie says nothing"
        assert pick(profile, "Full Stack Engineer (React, TypeScript, CSS)") is None, "full stack is neither kind, whatever it lists"
        assert pick(profile, "Python and Django Developer") == "backend", "more than one word of the same kind is still one kind"
        assert pick(profile, "Reactive Systems Engineer") is None, "whole words only"
        assert pick(profile, "Java Developer") == "backend" and pick(profile, "JavaScript Developer") == "frontend"
        assert pick(profile, "Backend Engineer", "we use react and typescript and css and vue every day") == "backend", "the title beats the text"
        assert pick(profile, "") is None and resume_pick.choose(profile, {}) is None


def test_the_posting_text_only_decides_when_it_clearly_leans_one_way():
    with tmp_cwd() as tmp:
        profile = profile_for(make_resumes(tmp, "base", "frontend", "backend"))
        front = "You will build interfaces in React and TypeScript with CSS, plus some React testing. A little Python for scripts."
        assert pick(profile, "Software Engineer", front) == "frontend"
        assert pick(profile, "Software Engineer", "React, CSS and TypeScript. Python, Django and Flask.") is None, "balanced"
        assert pick(profile, "Software Engineer", "React, CSS and TypeScript") is None, "three hits are not enough"
        assert pick(profile, "Software Engineer", "React React React React Python Python") == "frontend", "four against two is twice as many"
        assert pick(profile, "Software Engineer", "React React React React Python Python Python") is None, "four against three is not"
        assert pick(profile, "Software Engineer", "Python Python Python Python microservice API API") == "backend"


def test_a_kind_without_a_pdf_never_sends_the_other_kind():
    with tmp_cwd() as tmp:
        profile = profile_for(make_resumes(tmp, "base", "frontend"))                     # no backend version
        assert pick(profile, "Backend Engineer") is None, "a backend job gets the default, not the frontend resume"
        assert pick(profile, "Frontend Engineer") == "frontend"
        solo = tmp / "solo"
        solo.mkdir()
        (solo / "resume-base.pdf").write_bytes(b"%PDF fictional")
        assert resume_pick.choose(profile_for(solo), {"title": "Frontend Engineer"}) is None, "only one resume: nothing to choose between"
        assert resume_pick.choose({"attachments": {}}, {"title": "Frontend Engineer"}) is None and resume_pick.choose({}, {"title": "x"}) is None
        profile_default_is_it = profile_for(tmp / "resumes")
        profile_default_is_it["attachments"]["resume_path"] = str((tmp / "resumes" / "resume-frontend.pdf").resolve())
        assert pick(profile_default_is_it, "Frontend Engineer") is None, "already the default: nothing to switch"


def test_listed_resumes_and_your_own_keywords_replace_the_automatic_ones():
    with tmp_cwd() as tmp:
        folder = make_resumes(tmp, "base", "frontend")
        devops = tmp / "ops.pdf"
        devops.write_bytes(b"%PDF fictional")
        profile = profile_for(folder, resumes={"DevOps": str(devops), "ghost": str(tmp / "missing.pdf")}, resume_rules={"devops": ["terraform", "kubernetes"]})
        assert pick(profile, "Terraform Engineer") == "devops"
        assert pick(profile, "Frontend Engineer") is None, "listing resumes means only the listed ones are used"
        assert resume_pick.available(profile) == {"devops": str(devops)}, "a listed file that does not exist is ignored"
        profile = profile_for(folder, resume_rules={"frontend": ["design systems"]})
        assert pick(profile, "React Engineer") is None and pick(profile, "Design Systems Engineer") == "frontend", "your words replace the defaults for that name"


def test_for_job_gives_a_copy_with_the_resume_and_says_which():
    with tmp_cwd() as tmp:
        folder = make_resumes(tmp, "base", "frontend")
        profile = profile_for(folder)
        original = dict(profile["attachments"])
        changed, note = resume_pick.for_job(profile, {"title": "Frontend Engineer"})
        assert changed["attachments"]["resume_path"].endswith("resume-frontend.pdf") and changed["attachments"]["cover_template"] == "Dear team"
        assert note == "frontend version (resume-frontend.pdf)" and profile["attachments"] == original, "the shared profile is not changed"
        same, nothing = resume_pick.for_job(profile, {"title": "Staff Engineer"})
        assert same is profile and nothing == ""


def test_stage_three_prepares_each_job_with_its_own_resume_unless_switched_off():
    seen: list[tuple[str, str]] = []

    def record(self, profile, job, **kwargs):
        seen.append((job["title"], Path(profile["attachments"]["resume_path"]).name))

    with tmp_cwd() as tmp:
        folder = make_resumes(tmp, "base", "frontend")
        profile = dict(PROFILE, mode="dry_run", attachments=profile_for(folder)["attachments"])
        store = StateStore(tmp / "s.db")
        rid = store.start_run("stage3", "h", "p")
        jobs = [save_job(store, rid, "f", "https://boards.greenhouse.io/acme/jobs/1", company="Acme", title="Frontend Engineer"),
                save_job(store, rid, "b", "https://boards.greenhouse.io/acme/jobs/2", company="Acme", title="Backend Engineer")]
        with fake_playwright(_FakePage()), patch.object(GreenhouseHandler, "prepare", record):
            stage3_run(jobs, profile, state=store, run_id=rid, config=cfg_for(tmp, "dry_run"))
            assert seen == [("Frontend Engineer", "resume-frontend.pdf"), ("Backend Engineer", "resume-base.pdf")]
            seen.clear()
            off = cfg_for(tmp, "dry_run")
            off["stage3"]["resume_per_job"] = False
            stage3_run(jobs, profile, state=store, run_id=rid, config=off)
            assert seen == [("Frontend Engineer", "resume-base.pdf"), ("Backend Engineer", "resume-base.pdf")]
        assert profile["attachments"]["resume_path"].endswith("resume-base.pdf"), "the shared profile is untouched"
        store.close()
