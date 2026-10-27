"""Tests for resume parsing and profile generation (against a made-up resume, so they run on any machine)."""

import tempfile
from pathlib import Path

from profile_gen import parse_resume_to_profile

RESUME_MD = """<link rel="stylesheet" href="styles.css">
<div class="resumeContainer">
    <h1 style="text-align: center; ">Alex Rivera Lopez</h1>
    <p>(+1)5555550123 | alex.rivera@example.com | <a href="https://www.linkedin.com/in/alex-rivera-lopez/">LinkedIn</a> | <a href="https://github.com/alex-rivera">GitHub</a> | <a href="https://alex-rivera.example.com">Portfolio</a></p>
    <ul>
        <li><b>Core Languages</b>: Python, TypeScript, SQL.</li>
        <li><b>Backend Frameworks & DBs</b>: FastAPI, PostgreSQL (primary), Redis.</li>
        <li><b>Infrastructure & OS</b>: Docker, Linux.</li>
    </ul>
    <b><a href="https://github.com/alex-rivera/some-project">Some Project</a></b>
</div>
"""


def _make_resume(root: Path, markdown: str) -> Path:
    (root / "resume-versions").mkdir(parents=True)
    (root / "out").mkdir()
    (root / "resume-versions" / "resume-backend.md").write_text(markdown, encoding="utf-8")
    (root / "out" / "resume-base.pdf").write_bytes(b"%PDF-1.4\n")
    return root


def test_resume_parser_extracts_correct_fields():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        profile = parse_resume_to_profile(_make_resume(Path(d), RESUME_MD))

        assert profile["name"] == "Alex Rivera Lopez"           # the <h1> carries a style attribute, as real resumes do
        assert profile["first_name"] == "Alex"
        assert profile["last_name"] == "Rivera Lopez"
        assert profile["email"] == "alex.rivera@example.com"
        assert profile["phone"] == "+15555550123"
        assert profile["linkedin"] == "https://www.linkedin.com/in/alex-rivera-lopez/"
        assert profile["github"] == "https://github.com/alex-rivera"      # the profile link, not the project link
        assert profile["portfolio"] == "https://alex-rivera.example.com"

        # Verify skills list
        skills = profile["skills"]
        for expected in ("Python", "Docker", "Redis", "PostgreSQL", "FastAPI"):
            assert expected in skills
        assert "(primary)" not in " ".join(skills)

        # Verify attachment
        resume_path = profile["attachments"]["resume_path"]
        assert resume_path.endswith("resume-base.pdf")
        assert Path(resume_path).is_file()
        assert profile["attachments"]["cover_template"].endswith("Alex Rivera Lopez")


def test_resume_parser_leaves_unstated_details_empty():
    """A detail the resume does not state must stay empty: a guessed one would be sent to employers."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        profile = parse_resume_to_profile(_make_resume(Path(d), "<ul><li><b>Core Languages</b>: Go.</li></ul>"))

        for field in ("name", "first_name", "last_name", "email", "phone", "linkedin", "github", "portfolio", "location"):
            assert profile[field] == "", field
        assert profile["exclusions"]["companies"] == [] and profile["work_authorization"] == []
        assert "Go" in profile["skills"]


def test_resume_folder_comes_from_the_environment():
    import importlib
    import os

    import profile_gen

    saved = os.environ.get("AUTO_APPLIER_RESUME_DIR")
    elsewhere = Path(tempfile.gettempdir()) / "somewhere-else"
    try:
        os.environ["AUTO_APPLIER_RESUME_DIR"] = str(elsewhere)
        assert importlib.reload(profile_gen).RESUME_BASE_DIR == elsewhere
        os.environ.pop("AUTO_APPLIER_RESUME_DIR")
        assert importlib.reload(profile_gen).RESUME_BASE_DIR == Path("resume-template")      # no machine-specific default
    finally:
        if saved is None:
            os.environ.pop("AUTO_APPLIER_RESUME_DIR", None)
        else:
            os.environ["AUTO_APPLIER_RESUME_DIR"] = saved
        importlib.reload(profile_gen)


def test_missing_resume_folder_is_reported_not_guessed():
    try:
        parse_resume_to_profile(Path(tempfile.gettempdir()) / "no-such-resume-folder")
    except FileNotFoundError as e:
        assert "Resume markdown source not found" in str(e)
    else:
        raise AssertionError("a missing resume folder must be reported, not guessed")
