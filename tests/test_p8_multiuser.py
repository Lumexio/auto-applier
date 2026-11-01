"""Anyone should be able to clone this repository and use it for themselves.

* config.yaml is the shared default; a person's own settings live in config.local.yaml, which is never committed.
* profile.json is built only from the person's resume: nothing is added, and what a resume cannot say stays empty.
* A licence says they may.

Everything here uses made-up people and resumes.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
from pathlib import Path

import yaml

import answers
import config_loader
import daemon
import profile_gen
import source
from facts import facts_template
from fit import candidate_block
from stage2_filter.filters import salary_ok
from stage2_filter.relevance import score_with_reason, title_relevance
from tests.test_p0_safety import tmp_cwd
from tui import data as tui_data

ROOT = Path(__file__).resolve().parent.parent

JORDAN_MD = """# Jordan Lee

+351 912 345 678 | jordan.lee@example.com | [LinkedIn](https://www.linkedin.com/in/jordan-lee) | [GitHub](https://github.com/jordan-lee) | [Site](https://jordan.example.com)

## Experience
### Backend Engineer - Initech (2018 - 2021)
- Built services in Go

## Skills
- **Languages**: Go, Rust
- **Tools I use**: Terraform, Kubernetes
- **Spoken**: English (Fluent), Portuguese (Native)
"""


def say(fn, *args, **kwargs):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = fn(*args, **kwargs)
    return code, out.getvalue()


# --------------------------------------------------------------------------- config.yaml + config.local.yaml

def test_your_settings_go_over_the_defaults_key_by_key_and_nothing_is_modified():
    base = {"a": {"x": 1, "y": {"z": 1, "w": 2}}, "list": [1, 2], "n": 5, "keep": "k", "scalar": 1, "section": {"k": 1}}
    over = {"a": {"y": {"z": 9}}, "list": [3], "n": None, "new": {"q": 1}, "scalar": {"now": "a section"}, "section": 7}
    snapshot = json.dumps([base, over], sort_keys=True)
    merged = config_loader.deep_merge(base, over)
    assert merged == {"a": {"x": 1, "y": {"z": 9, "w": 2}}, "list": [3], "n": None, "keep": "k", "new": {"q": 1}, "scalar": {"now": "a section"}, "section": 7}
    assert json.dumps([base, over], sort_keys=True) == snapshot, "the inputs must not be modified"
    merged["a"]["y"]["z"] = 0
    merged["new"]["q"] = 0
    merged["list"].append(4)
    assert base["a"]["y"]["z"] == 1 and over["a"]["y"]["z"] == 9 and over["new"]["q"] == 1 and over["list"] == [3], "the result must not share anything with its inputs"
    assert config_loader.deep_merge(base, {})["section"] is not base["section"]
    assert config_loader.deep_merge({}, {"a": 1}) == {"a": 1} and config_loader.deep_merge({"a": 1}, {}) == {"a": 1}


def test_config_local_yaml_is_found_beside_the_config_and_is_optional():
    assert config_loader.local_path("config.yaml") == Path("config.local.yaml")
    assert config_loader.local_path(Path("sub") / "alt.yml") == Path("sub") / "alt.local.yml"
    with tmp_cwd() as tmp:
        assert config_loader.load_config(tmp / "nothing.yaml") == {}                                  # no config at all: empty, like before
        (tmp / "config.yaml").write_text("llm: {model: a, temperature: 0.1}\nstage1:\n  target_urls: [x, y]\nkeep: 1\n", encoding="utf-8")
        assert config_loader.load_config(tmp / "config.yaml") == {"llm": {"model": "a", "temperature": 0.1}, "stage1": {"target_urls": ["x", "y"]}, "keep": 1}
        (tmp / "config.local.yaml").write_text("llm: {model: b}\nstage1:\n  target_urls: [z]\n", encoding="utf-8")
        assert config_loader.load_config(tmp / "config.yaml") == {"llm": {"model": "b", "temperature": 0.1}, "stage1": {"target_urls": ["z"]}, "keep": 1}
        (tmp / "config.local.yaml").write_text("", encoding="utf-8")
        assert config_loader.load_config(tmp / "config.yaml")["llm"]["model"] == "a"                  # an empty local file changes nothing
        (tmp / "config.local.yaml").write_text("- not\n- a mapping\n", encoding="utf-8")
        try:
            config_loader.load_config(tmp / "config.yaml")
        except ValueError as e:
            assert "config.local.yaml" in str(e) and "mapping" in str(e)
        else:
            raise AssertionError("a local file that is not a mapping must be reported, not ignored")


def test_every_reader_of_the_config_sees_your_local_settings():
    with tmp_cwd() as tmp:
        (tmp / "config.yaml").write_text("a: {x: 1}\nstage3: {mode: dry_run}\ndaemon: {state_db: base.db}\n", encoding="utf-8")
        (tmp / "config.local.yaml").write_text("a: {y: 2}\ndaemon: {state_db: mine.db}\n", encoding="utf-8")
        want_a = {"x": 1, "y": 2}
        assert daemon.load_yaml_config(str(tmp / "config.yaml"))["a"] == want_a
        assert answers._config(str(tmp / "config.yaml"))["a"] == want_a
        assert source.load_config(str(tmp / "config.yaml"))["a"] == want_a
        assert tui_data.load_config(tmp)["a"] == want_a and tui_data.load_config(tmp)["daemon"]["state_db"] == "mine.db"
    for name in ("fit_review.py", "sites.py"):                                                        # these read it inside their command-line entry point
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "config_loader" in text and "yaml.safe_load(Path(args.config)" not in text, f"{name} reads config.yaml without config.local.yaml"
    for name in ("daemon.py", "answers.py", "source.py"):
        assert "yaml.safe_load(cfg_file" not in (ROOT / name).read_text(encoding="utf-8") and "config_loader" in (ROOT / name).read_text(encoding="utf-8")


def test_the_shipped_defaults_carry_nobodys_personal_setup():
    text = (ROOT / "config.yaml").read_text(encoding="utf-8")
    cfg = yaml.safe_load(text)
    live = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))          # what the program reads, without the comments
    assert "config.local.yaml" in text.splitlines()[0] + text.splitlines()[1]
    assert "models" not in cfg, "the multi-model block depends on what each person has installed: it belongs in config.local.yaml"
    hosts = {re.sub(r"^https?://", "", u).split("/")[0] for u in cfg["stage1"]["target_urls"]}
    assert hosts <= {"www.linkedin.com", "app.joinhandshake.com"}, f"company boards chosen for one person are in the defaults: {hosts}"
    assert not (cfg.get("source") or {}).get("sync_from"), "no machine- or person-specific folder in the defaults"
    for marker in ("-local\"", "boards.greenhouse.io/", "jobs.lever.co/", "../resume-template"):
        assert marker not in live, f"{marker} is a personal choice and belongs in config.local.yaml"
    assert cfg["stage3"]["mode"] in ("autonomous", "assisted", "dry_run") and cfg["llm"]["model"]
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "config.local.yaml" in ignore and "/me/" in ignore


# --------------------------------------------------------------------------- profile.json built from a resume, and nothing else

def test_a_profile_lists_only_the_technologies_the_resume_lists():
    with tmp_cwd() as tmp:
        (tmp / "resume.md").write_text(JORDAN_MD, encoding="utf-8")
        skills = profile_gen.parse_resume_to_profile(tmp / "resume.md")["skills"]
        assert skills == ["Go", "Rust", "Terraform", "Kubernetes"]                                    # the labels do not matter; spoken languages are not skills
        for injected in ("Python", "TypeScript", "JavaScript", "SQL", "Docker", "API Design", "Playwright"):
            assert injected not in skills, f"{injected} is not in the resume: it must not be added to the profile"


def test_what_a_resume_cannot_say_stays_empty_and_is_reported():
    with tmp_cwd() as tmp:
        (tmp / "resume.md").write_text(JORDAN_MD, encoding="utf-8")
        p = profile_gen.parse_resume_to_profile(tmp / "resume.md")
        assert (p["name"], p["first_name"], p["last_name"]) == ("Jordan Lee", "Jordan", "Lee")
        assert (p["email"], p["phone"]) == ("jordan.lee@example.com", "+351912345678")
        assert (p["linkedin"], p["github"], p["portfolio"]) == ("https://www.linkedin.com/in/jordan-lee", "https://github.com/jordan-lee", "https://jordan.example.com")
        assert p["location"] == "" and p["work_authorization"] == [] and p["min_salary"] is None and p["currency"] == ""
        assert p["seniority"] == "" and p["remote_ok"] is None
        assert p["exclusions"] == {"companies": [], "keywords": [], "domains": []}, "excluded words are the user's own choice"
        assert p["mode"] == "dry_run", "a generated profile must not submit anything until its owner says so"
        gaps = "\n".join(profile_gen.describe_gaps(p))
        for what in ("where you live", "countries you may work in", "lowest pay", "your level", "remote work"):
            assert what in gaps
        assert "--location" in gaps and "--authorized" in gaps and "--min-salary" in gaps and "--seniority" in gaps and "--remote" in gaps
        assert "your name" not in gaps and "your email" not in gaps                                    # what the resume did say is not asked for again
        full = profile_gen.parse_resume_to_profile(tmp / "resume.md", location="Lisbon, Portugal", authorized=["pt", " es"], min_salary=60000, currency="eur",
                                                   seniority="Mid", remote=False, mode="assisted", exclude_companies=["Acme"], exclude_keywords=["unpaid"])
        assert (full["location"], full["work_authorization"], full["min_salary"], full["currency"], full["seniority"]) == ("Lisbon, Portugal", ["PT", "ES"], 60000, "EUR", "mid")
        assert full["remote_ok"] is False and full["mode"] == "assisted" and full["exclusions"]["companies"] == ["Acme"] and full["exclusions"]["keywords"] == ["unpaid"]
        assert profile_gen.describe_gaps(full) == [], "nothing is left to report once everything is given"
        assert len(profile_gen.describe_gaps({**full, "remote_ok": None})) == 1 and profile_gen.describe_gaps({**full, "remote_ok": False}) == []     # "no" is an answer


def test_a_date_range_is_not_mistaken_for_a_phone_number():
    with tmp_cwd() as tmp:
        (tmp / "r.md").write_text("# Sam Doe\n\nsam@example.com\n\n## Education\n- Example University, 2015 - 2021\n- Course 2010 - 2012 2013\n", encoding="utf-8")
        assert profile_gen.parse_resume_to_profile(tmp / "r.md")["phone"] == ""
        (tmp / "r.md").write_text("# Sam Doe\n\n(555) 555-0123 | sam@example.com. \n", encoding="utf-8")
        p = profile_gen.parse_resume_to_profile(tmp / "r.md")
        assert p["phone"] == "5555550123" and p["email"] == "sam@example.com"                          # a trailing full stop is not part of the address


def test_the_resume_is_found_in_the_usual_places_or_the_user_is_told_where_to_put_it():
    with tmp_cwd() as tmp:
        (tmp / "me" / "resumes").mkdir(parents=True)
        (tmp / "me" / "resumes" / "a-notes.md").write_text("# Not a resume\n\nsome notes\n", encoding="utf-8")          # sorts first, but carries no contact details
        (tmp / "me" / "resumes" / "jordan.md").write_text(JORDAN_MD, encoding="utf-8")
        (tmp / "me" / "resumes" / "jordan.pdf").write_bytes(b"%PDF-1.4\n")
        p = profile_gen.parse_resume_to_profile()                                                     # nothing given: me/resumes, the version with the most contact details
        assert p["name"] == "Jordan Lee" and p["attachments"]["resume_path"].endswith("jordan.pdf")
        legacy = tmp / "elsewhere"                                                                     # the resume-template layout still works
        (legacy / "resume-versions").mkdir(parents=True)
        (legacy / "out").mkdir()
        (legacy / "resume-versions" / "resume-backend.md").write_text(JORDAN_MD.replace("Jordan Lee", "Legacy Person"), encoding="utf-8")
        (legacy / "out" / "resume-base.pdf").write_bytes(b"%PDF")
        assert profile_gen.parse_resume_to_profile(legacy)["name"] == "Legacy Person"
        (legacy / "resume-versions" / "resume-a-richer.md").write_text(JORDAN_MD.replace("Jordan Lee", "Richer Version"), encoding="utf-8")
        assert profile_gen.parse_resume_to_profile(legacy)["name"] == "Legacy Person", "the resume-template layout keeps preferring resume-backend.md"
        assert profile_gen.parse_resume_to_profile(tmp / "me" / "resumes" / "jordan.md")["name"] == "Jordan Lee"          # a single file
        (tmp / "empty").mkdir()
        try:
            profile_gen.parse_resume_to_profile(tmp / "empty")
        except FileNotFoundError as e:
            assert "Resume markdown source not found" in str(e) and "--resume" in str(e) and "me/resumes" in str(e)
        else:
            raise AssertionError("no resume must be reported, with where to put one")


def test_the_command_never_overwrites_a_profile_and_says_what_is_still_empty():
    with tmp_cwd() as tmp:
        (tmp / "me" / "resumes").mkdir(parents=True)
        (tmp / "me" / "resumes" / "jordan.md").write_text(JORDAN_MD, encoding="utf-8")
        code, out = say(profile_gen.main, [])
        assert code == 0 and "Wrote" in out and "Still empty" in out and "--location" in out and "dry_run" in out
        first = (tmp / "profile.json").read_text(encoding="utf-8")
        assert json.loads(first)["mode"] == "dry_run" and json.loads(first)["skills"][0] == "Go"
        (tmp / "profile.json").write_text('{"name": "hand edited"}', encoding="utf-8")
        code, out = say(profile_gen.main, [])
        assert code == 1 and "already exists" in out and (tmp / "profile.json").read_text(encoding="utf-8") == '{"name": "hand edited"}'
        code, out = say(profile_gen.main, ["--force", "--location", "Lisbon, Portugal", "--authorized", "pt,es", "--min-salary", "60000", "--currency", "eur",
                                           "--seniority", "mid", "--remote", "yes", "--mode", "assisted", "--exclude-company", "Acme", "--exclude-keyword", "unpaid"])
        done = json.loads((tmp / "profile.json").read_text(encoding="utf-8"))
        assert code == 0 and "Still empty" not in out
        assert (done["location"], done["work_authorization"], done["min_salary"], done["currency"], done["seniority"], done["remote_ok"], done["mode"]) == (
            "Lisbon, Portugal", ["PT", "ES"], 60000, "EUR", "mid", True, "assisted")
        assert done["exclusions"]["companies"] == ["Acme"] and done["exclusions"]["keywords"] == ["unpaid"]
        (tmp / "me" / "resumes" / "jordan.md").unlink()
        code, out = say(profile_gen.main, ["--out", "other.json"])
        assert code == 1 and "not found" in out and not (tmp / "other.json").exists()


def test_the_daemon_tells_a_new_user_what_the_profile_it_generated_lacks():
    with tmp_cwd() as tmp:
        (tmp / "me" / "resumes").mkdir(parents=True)
        (tmp / "me" / "resumes" / "jordan.md").write_text(JORDAN_MD, encoding="utf-8")
        d = daemon.Daemon.__new__(daemon.Daemon)                                                       # only the profile step is under test
        d.profile_path = tmp / "profile.json"
        code, out = say(d.load_or_generate_profile)
        assert code["name"] == "Jordan Lee" and (tmp / "profile.json").is_file() and "Generating from resume source of truth" in out
        for what in ("where you live", "the countries you may work in", "the lowest pay you would take", "your level", "whether remote work is fine"):
            assert f"[Daemon] profile.json does not say yet: {what}" in out, what
        code, out = say(d.load_or_generate_profile)                                                    # an existing profile is just read
        assert code["name"] == "Jordan Lee" and "does not say yet" not in out


def test_unknown_values_do_not_become_assumptions_downstream():
    title = {"title": "Senior Backend Engineer", "description": "Python and Docker services", "company": "Acme"}
    plain = {"skills": ["Python", "Docker"], "seniority": ""}
    senior = {"skills": ["Python", "Docker"], "seniority": "senior"}
    unstated_score, unstated_why = score_with_reason(title, plain)
    senior_score, senior_why = score_with_reason(title, senior)
    assert "seniority match" in senior_why and "seniority match" not in unstated_why, "an empty level must not match every title"
    assert round(senior_score - unstated_score, 2) == 0.2
    assert "seniority match" in score_with_reason(title, {"skills": ["Python"]})[1], "a profile written before the field existed keeps its old default"
    junior = {"title": "Junior Developer", "company": "Acme"}
    assert title_relevance(junior, {**plain})[0] is True and title_relevance(junior, {**senior})[0] is False        # only a stated senior level skips junior roles
    assert salary_ok({"salary_max": 20000}, {"min_salary": None}) and salary_ok({"salary_max": 20000}, {}) and not salary_ok({"salary_max": 20000}, {"min_salary": 100000})
    block = candidate_block({"skills": ["Go"], "seniority": "", "min_salary": None, "currency": "", "remote_ok": None}, {})
    assert "seniority: unspecified" in block and "minimum salary" not in block and "open to remote" not in block
    for remote, expected in ((None, None), (True, True), (False, False)):
        facts = yaml.safe_load(facts_template({"skills": [], "remote_ok": remote, "min_salary": None, "currency": ""}))
        assert facts["location"]["open_to_remote"] is expected, remote
        assert facts["compensation"]["expected_salary"] is None and facts["compensation"]["currency"] is None


# --------------------------------------------------------------------------- the licence

def test_the_licence_is_mit_and_the_readme_says_so():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License\n")
    assert re.search(r"^Copyright \(c\) 20\d\d \S+", text, re.M), "the copyright line needs a year and a holder"
    for clause in ("Permission is hereby granted, free of charge, to any person obtaining a copy",
                   "to use, copy, modify, merge, publish, distribute, sublicense, and/or sell",
                   "The above copyright notice and this permission notice shall be included in all",
                   'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND'):
        assert clause in text, f"the MIT text is incomplete: {clause!r}"
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert re.search(r"^## License\s+MIT, see \[LICENSE\]\(LICENSE\)", readme, re.M)
    setup = (ROOT / "SETUP.md").read_text(encoding="utf-8")
    assert "config.local.yaml" in setup and "profile_gen.py --resume" in setup and "config.local.yaml" in readme
