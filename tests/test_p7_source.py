"""The source of truth: years of experience are computed from the dated jobs in the resumes, never invented.

Everything here uses a fictional resume (Northwind Labs, Contoso Systems ...). The numbers are worked out by hand in the comments.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
from datetime import date
from pathlib import Path
from unittest.mock import patch

import resume_facts as rf
import source
from answerer import Answer, AnswerContext, Blocked, apply_rules
from facts import flatten, overlay_derived, render_lines
from form_questions import Question
from tests.test_p0_safety import tmp_cwd

TODAY = date(2026, 6, 15)
SUMMARY = ("Backend engineer with 7+ years of experience building billing and data platforms, comfortable across the stack and "
           "happiest when a slow query turns into a fast one.")


def job(title, company, dates, bullets):
    lis = "".join(f"<li>{b}</li>" for b in bullets)
    return (f'<ul style="margin-left: 20px;"><h3><div class="carrer-item"><div><b>{title}</b>, {company}, Austin, Texas</div>'
            f'<div class="date-item">{dates}</div></div></h3>{lis}</ul>')


def project(name, description, dates):
    return (f'<li><div class="project-item"><div><b><a href="https://example.test/{name}">{name}</a></b>, Austin<p>{description}</p></div>'
            f'<div class="date-item"><b>{dates}</b></div></div></li>')


def resume(summary=SUMMARY, northwind="Mar 2019 - Dec 2022", skills=None, extra_job="", vue_project=True):
    projects = project("Pocket CRM", "A small CRM built with <b>Vue.js</b> and <b>SQLite</b>, improving follow-ups by <b>40%</b>.", "Jan 2016 - Dec 2017") if vue_project else ""
    projects += project("Gopher Tools", "Command line tools written in <b>Go</b>.", "Mar 2021 - Present")
    skills = skills or ('<li><b>Core Languages</b>: Python, JavaScript (ES6+), TypeScript, Go, Java.</li>'
                        '<li><b>Tools</b>: Docker, Git, Unit Testing (pytest/Jest).</li>'
                        '<li><b>Languages</b>: English (Professional), Spanish (Native).</li>')
    return (
        '<link rel="stylesheet" href="styles.css"><div class="resumeContainer"><div><h1>Alex Rivera</h1><p>(+1)5550100 | alex@example.test</p></div>'
        f'<div><p>{summary}</p></div><div><h2>Education</h2><div class="education-item"><div>Example University</div><div>B.S. 2010 - 2014</div></div></div>'
        '<div><h2>Professional Experience</h2>'
        + job("Senior Backend Engineer", "Northwind Labs", northwind,
              ["<b>Built</b> the billing API with <b>Django</b>, <b>PostgreSQL</b> and <b>Celery</b>, cutting latency by <b>30%</b>."])
        + job("Software Engineer", "Contoso Systems", "Jan 2023 - Present",
              ["<b>Shipped</b> a dashboard in <b>React.js</b> and <b>TypeScript</b> on a <b>Node.js</b> backend."]) + extra_job
        + f'</div><div><h2>Key Engineering Projects</h2><ul>{projects}</ul></div>'
        f'<div><h2>Technical Skills &amp; Expertise</h2><ul>{skills}</ul></div></div>')


def cfg_for(tmp: Path, **experience) -> dict:
    return {"paths": {"source_dir": str(tmp / "me")}, "source": {"experience": experience}}


def put(tmp: Path, name: str, text: str, folder: str = "resumes") -> Path:
    p = tmp / "me" / folder / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def derived_in(tmp: Path, **experience):
    return rf.derive(cfg_for(tmp, **experience), TODAY)


def ask(label, facts, *, profile=None, kind="text", options=None, input_type="text"):
    q = Question(1, kind, label, options or [], True, "", input_type)
    return apply_rules(q, AnswerContext(profile=profile or {"skills": []}, facts=facts))


# --------------------------------------------------------------------------- dates and terms

def test_dates_are_read_the_way_resumes_write_them():
    r = lambda t: rf.parse_range(t, TODAY)                                         # noqa: E731
    jan = lambda y, m: y * 12 + m - 1                                              # noqa: E731
    assert r("Mar 2019 - Dec 2022") == (jan(2019, 3), jan(2022, 12) + 1)           # the last month counts as worked
    assert r("Feb 2020 – Mar 2024") == (jan(2020, 2), jan(2024, 3) + 1) and r("Feb 2020 — Mar 2024") == r("Feb 2020 - Mar 2024")
    assert r("Aug 2015 to May 2021") == (jan(2015, 8), jan(2021, 5) + 1) and r("Sept 2015 - Jan 2016") == (jan(2015, 9), jan(2016, 1) + 1)
    assert r("Jan 2023 - Present") == (jan(2023, 1), jan(2026, 6) + 1) and r("2025 - current")[1] == jan(2026, 6) + 1       # "Present" is this month
    assert r("2015 - 2021") == (jan(2015, 1), jan(2021, 12) + 1)                   # years alone cover whole years
    assert r("Dec 2022 - Mar 2019") is None and r("no dates here") is None and r("born 1980") is None
    assert rf.union_months([(0, 12), (6, 18), (30, 36)]) == 24 and rf.union_months([(0, 12), (12, 24)]) == 24 and rf.union_months([]) == 0
    assert rf.years([(0, 46)]) == 3.8 and rf.span((jan(2019, 3), jan(2022, 12) + 1)) == "2019-03..2022-12"


def test_a_skills_line_becomes_technologies_and_not_languages_people_speak():
    assert rf.split_terms("Jest/React Testing Library, JavaScript (ES6+) and Docker.") == ["Jest", "React Testing Library", "JavaScript", "Docker"]
    assert rf.split_terms("C/C++, CI/CD &amp; SQLite/MariaDB") == ["C/C++", "CI/CD", "SQLite", "MariaDB"]
    assert rf.split_terms("Arch Linux (System &amp; Kernel Tuning), Unit Testing (Jest/PHPUnit)") == ["Arch Linux", "Unit Testing"]
    assert rf.split_terms("English (Professional), Spanish (Native), Japanese (Conversational)") == []
    assert rf.split_terms("Spanish, Python") == ["Python"] and rf.split_terms("<b>ReactJS</b>, vue, NestJS, node, postgres") == ["React.js", "Vue.js", "Nest.js", "Node.js", "PostgreSQL"]
    assert rf.split_terms("40%, 12") == [] and rf.split_terms("") == []
    assert rf.split_terms("Tagalog (Native), Python, Klingon (Conversational)") == ["Python"]                         # a language nobody listed is still recognised by its level
    assert rf.split_terms("34% increase, React, 2x faster, 3 times") == ["React.js"] and rf.split_terms("3D, 2FA, 5G") == ["3D", "2FA", "5G"]   # metrics out, real names kept


# --------------------------------------------------------------------------- reading resumes

def test_the_template_layout_is_read_into_jobs_projects_skills_and_summary():
    with tmp_cwd() as tmp:
        parsed = rf.parse_resume(put(tmp, "alex.html", resume()), TODAY)
        jobs = [e for e in parsed.entries if e.kind == "job"]
        projects = [e for e in parsed.entries if e.kind == "project"]
        assert [(e.title, e.company) for e in jobs] == [("Senior Backend Engineer", "Northwind Labs"), ("Software Engineer", "Contoso Systems")]
        assert [e.title for e in projects] == ["Pocket CRM", "Gopher Tools"]
        assert jobs[0].bold == ["Django", "PostgreSQL", "Celery"]                  # the opening verb "Built" and the metric "30%" are not technologies
        assert jobs[1].bold == ["React.js", "TypeScript", "Node.js"] and projects[0].bold == ["Vue.js", "SQLite"]
        assert parsed.listed == ["Python", "JavaScript", "TypeScript", "Go", "Java", "Docker", "Git", "Unit Testing"]       # no English / Spanish
        assert parsed.summary == SUMMARY and "alex@example.test" not in parsed.summary                                    # the contact line is not the summary
        assert jobs[0].bullets == ["Built the billing API with Django, PostgreSQL and Celery, cutting latency by 30%."]


def test_plain_markdown_and_notes_are_read_too():
    md = ("# Alex Rivera\n\n## Summary\nBackend engineer who likes small, sharp tools and long, boring migrations.\n\n"
          "## Experience\n### Staff Engineer - Initech Labs (Jan 2010 - Dec 2012)\n- Built tooling in Ruby for the data team\n- Wrote a Perl importer\n"
          "**Engineer at Hooli (2013 - 2014)**\nDid Ruby on Rails work.\n\n## Projects\n### Side Thing (2015 - 2016)\n- A Go experiment\n\n"
          "## Skills\n- **Languages**: Ruby, Perl, Go\n- **Spoken**: English (Fluent)\n\n## Education\n- Example University (2005 - 2009)\n")
    with tmp_cwd() as tmp:
        parsed = rf.parse_resume(put(tmp, "notes.md", md), TODAY)
        assert [(e.kind, e.title, e.company, rf.span(e.rng)) for e in parsed.entries] == [
            ("job", "Staff Engineer", "Initech Labs", "2010-01..2012-12"), ("job", "Engineer", "Hooli", "2013-01..2014-12"), ("project", "Side Thing", "", "2015-01..2016-12")]
        assert parsed.entries[0].bullets == ["Built tooling in Ruby for the data team", "Wrote a Perl importer"]
        assert parsed.listed == ["Ruby", "Perl", "Go"] and parsed.summary.startswith("Backend engineer who likes")
        data = rf.derive(cfg_for(tmp), TODAY)
        assert data["experience"]["total_years"] == 5.0 and data["experience"]["years_by_skill"]["Ruby"] == 5.0        # 3 + 2 years of jobs; the project and the university are not work
        assert data["experience"]["years_by_skill"]["Perl"] == 3.0                                                       # only the first job mentions it


def test_project_years_never_exceed_the_years_of_actual_jobs():
    md = ("## Experience\n### Developer - Acme (2020 - 2021)\n- Wrote Python services\n\n## Projects\n### Old Project (2010 - 2018)\n- Wrote it in Rust\n\n"
          "## Skills\n- Rust, Python\n")
    with tmp_cwd() as tmp:
        put(tmp, "a.md", md)
        data = rf.derive(cfg_for(tmp), TODAY)
        assert data["experience"]["total_years"] == 2.0
        assert data["experience"]["years_by_skill"]["Rust"] == 2.0                                               # nine years of project, capped at the two of work
        assert "capped at your 2.0 job years" in data["provenance"]["experience.years_by_skill.Rust"]
        assert rf.derive(cfg_for(tmp, count="jobs_and_projects"), TODAY)["experience"]["years_by_skill"]["Rust"] == 9.0      # unless projects are told to count


# --------------------------------------------------------------------------- the numbers

def test_years_come_from_job_dates_frameworks_imply_languages_and_the_rest_follows_the_policy():
    with tmp_cwd() as tmp:
        put(tmp, "alex.html", resume())
        data = derived_in(tmp)
        exp, prov = data["experience"], data["provenance"]
        by = exp["years_by_skill"]
        # Northwind Mar 2019 - Dec 2022 = 46 months = 3.8 y; Contoso Jan 2023 - Jun 2026 = 42 months = 3.5 y; no gap: 88 months = 7.3 y
        assert exp["total_years"] == 7.3
        assert (by["Django"], by["PostgreSQL"], by["Celery"]) == (3.8, 3.8, 3.8) and (by["React.js"], by["TypeScript"], by["Node.js"]) == (3.5, 3.5, 3.5)
        assert by["Python"] == 3.8 and "implied by Django" in prov["experience.years_by_skill.Python"]       # Django is Python, though no bullet says "Python"
        assert by["JavaScript"] == 3.5 and "implied by" in prov["experience.years_by_skill.JavaScript"]
        assert "named in dated work: Northwind Labs" == prov["experience.years_by_skill.Django"]
        # projects only: counted for what they are, never more than the job years
        assert by["Vue.js"] == 2.0 and by["SQLite"] == 2.0                                                   # Pocket CRM, Jan 2016 - Dec 2017
        assert by["Go"] == 5.3 and "dated projects only (Gopher Tools)" in prov["experience.years_by_skill.Go"]    # Mar 2021 - Jun 2026 = 64 months
        # listed in the skills but no dated job names it: the career, as the policy says
        assert by["Docker"] == by["Git"] == by["Java"] == by["Unit Testing"] == 7.3
        assert "no dated job names it" in prov["experience.years_by_skill.Docker"] and "7.3-year career" in prov["experience.years_by_skill.Docker"]
        assert "English" not in by and "Spanish" not in by and "Rust" not in by                              # people languages and unlisted technologies: no claim
        assert prov["experience.total_years"].startswith("dates of 2 job(s): Northwind Labs 2019-03..2022-12, Contoso Systems 2023-01..2026-06")
        assert [j["dates"] for j in data["jobs"]] == ["2019-03..2022-12", "2023-01..2026-06"] and [p["title"] for p in data["projects"]] == ["Pocket CRM", "Gopher Tools"]
        assert data["narrative"]["summary"] == SUMMARY and data["narrative"]["highlights"][0].startswith("Shipped a dashboard")      # newest job first
        assert list(by.values()) == sorted(by.values(), reverse=True)


def test_the_policy_decides_whose_dates_count_and_whether_listed_technologies_count_at_all():
    with tmp_cwd() as tmp:
        put(tmp, "alex.html", resume())
        both = derived_in(tmp, count="jobs_and_projects")
        # jobs 2019-03..2026-06 (88 months) + Pocket CRM (24 months, a separate stretch) = 112 months; Gopher Tools lies inside the jobs
        assert both["experience"]["total_years"] == 9.3 and both["experience"]["years_by_skill"]["Vue.js"] == 2.0
        assert "named in dated work: Pocket CRM" == both["provenance"]["experience.years_by_skill.Vue.js"]
        off = derived_in(tmp, undated_skills="off")
        by = off["experience"]["years_by_skill"]
        assert "Docker" not in by and "Git" not in by and "Java" not in by and by["Python"] == 3.8                 # dated evidence is unaffected
        assert off["experience"]["total_years"] == 7.3
    with tmp_cwd() as tmp:                                                                                      # no job dates at all: nothing to base a career on
        put(tmp, "x.md", "## Skills\n- Python, Docker\n")
        none = rf.derive(cfg_for(tmp), TODAY)
        assert none["experience"]["total_years"] == 0 and none["experience"]["years_by_skill"] == {}


def test_a_word_in_a_sentence_proves_nothing_but_a_shown_technology_does():
    with tmp_cwd() as tmp:
        extra = job("Engineer", "Initech Labs", "Jan 2000 - Dec 2000", ["<b>Led</b> a team that would express interest in Spring releases and Rails of work."])
        put(tmp, "a.html", resume(extra_job=extra))
        by = derived_in(tmp)["experience"]["years_by_skill"]
        assert "Express" not in by and "Spring" not in by and "Rails" not in by and "Ruby" not in by and "Java" in by       # Java is only here because it is listed


def test_versions_that_disagree_on_a_date_use_the_shorter_one_and_say_so():
    with tmp_cwd() as tmp:
        put(tmp, "a.html", resume())
        put(tmp, "b.html", resume(northwind="Mar 2018 - Dec 2022"))                                             # claims a year more at Northwind
        data = derived_in(tmp)
        assert data["jobs"][0]["dates"] == "2019-03..2022-12" and data["experience"]["total_years"] == 7.3
        assert len(data["conflicts"]) == 1 and "Northwind Labs" in data["conflicts"][0] and "2018-03..2022-12 (b.html)" in data["conflicts"][0]
        put(tmp, "b.html", resume())
        assert derived_in(tmp)["conflicts"] == []                                                               # versions that agree are no conflict
    with tmp_cwd() as tmp:
        put(tmp, "a.html", resume())
        put(tmp, "b.html", resume(vue_project=False))                                                           # a version may leave a project out
        data = derived_in(tmp)
        assert data["conflicts"] == [] and "Pocket CRM" in [p["title"] for p in data["projects"]]


def test_with_no_source_folder_configured_or_no_resumes_nothing_is_read():
    with tmp_cwd() as tmp:
        put(tmp, "a.html", resume())
        assert rf.source_dir({}) is None and rf.source_files({}) == [] and rf.derive({}) == {} and rf.ensure_derived({}) == {}      # opt-in: never reads by default
        assert rf.ensure_derived({"paths": {"source_dir": str(tmp / "empty")}}) == {}
        (tmp / "me" / "resumes" / "a.pdf").write_bytes(b"%PDF")                                                  # the printout of a.html: never read a second time
        (tmp / "me" / "resumes" / "x.doc").write_bytes(bytes([0xD0, 0xCF]))                                  # the old binary Word format is not read
        (tmp / "me" / "resumes" / ".hidden.md").write_text("## Experience", encoding="utf-8")
        assert [p.name for p in rf.source_files(cfg_for(tmp))] == ["a.html"]                                   # printouts, .doc and dot files are not resumes


def test_the_derived_file_is_reused_until_a_resume_the_policy_or_the_month_changes():
    with tmp_cwd() as tmp:
        cfg = cfg_for(tmp)
        a = put(tmp, "a.html", resume())
        first = rf.ensure_derived(cfg, TODAY)
        path = rf.derived_path(cfg)
        assert path.is_file() and path.read_text(encoding="utf-8").startswith("# Written by resume_facts.py") and not list(path.parent.glob("*.tmp"))
        stamp = path.stat().st_mtime_ns
        assert rf.ensure_derived(cfg, TODAY)["fingerprint"] == first["fingerprint"] and path.stat().st_mtime_ns == stamp          # same inputs: not rewritten
        assert rf.ensure_derived(cfg, date(2026, 6, 30))["fingerprint"] == first["fingerprint"]                                   # same month
        a.write_text(resume(northwind="Mar 2019 - Dec 2021"), encoding="utf-8")
        changed = rf.ensure_derived(cfg, TODAY)
        assert changed["fingerprint"] != first["fingerprint"] and changed["experience"]["total_years"] == 6.3                       # a resume changed: recomputed (3 + 3.5)
        later = rf.ensure_derived(cfg, date(2026, 7, 2))
        assert later["fingerprint"] != changed["fingerprint"] and later["experience"]["total_years"] == 6.4                         # "Present" moved a month
        assert rf.ensure_derived(cfg_for(tmp, count="jobs_and_projects"), date(2026, 7, 2))["fingerprint"] != later["fingerprint"]    # a different policy
        assert rf.ensure_derived(cfg, date(2026, 7, 2), force=True)["fingerprint"] == later["fingerprint"]
        path.write_text("{ not: [valid", encoding="utf-8")
        assert rf.read_derived(cfg) == {} and rf.ensure_derived(cfg, date(2026, 7, 2))["experience"]["total_years"] == 6.4       # a damaged file is rebuilt


# --------------------------------------------------------------------------- layering under facts.yaml

def test_what_you_state_wins_and_a_null_falls_back_to_the_resumes():
    with tmp_cwd() as tmp:
        put(tmp, "a.html", resume())
        derived = derived_in(tmp)
        mine = {"experience": {"total_years": None, "years_by_skill": {"Python": 9, "Django": None}}, "narrative": {"summary": "Mine.", "strengths": []},
                "location": {"city": "Austin"}}
        before = repr(mine)
        merged = overlay_derived(mine, derived)
        assert repr(mine) == before                                                                            # the caller's facts are not modified
        by = merged["experience"]["years_by_skill"]
        assert by["Python"] == 9 and by["Django"] == 3.8 and merged["experience"]["total_years"] == 7.3 and merged["location"] == {"city": "Austin"}
        assert merged["narrative"]["summary"] == "Mine." and merged["narrative"]["highlights"] and "strengths" in merged["narrative"]
        prov = merged["_provenance"]
        assert "experience.years_by_skill.Python" not in prov and "experience.years_by_skill.Django" in prov and "experience.total_years" in prov
        assert "narrative.summary" not in prov and "narrative.highlights" in prov
        assert overlay_derived({"a": 1}, {}) == {"a": 1} and "_provenance" not in overlay_derived({"experience": {"total_years": 5}}, {"experience": {"total_years": 3}})
        assert overlay_derived({"experience": None}, derived)["experience"]["total_years"] == 7.3                # a null section is filled whole
        flat = flatten(merged)
        assert not any(k.startswith("_") for k in flat) and "_provenance" not in render_lines(merged)[0]         # bookkeeping never reaches a prompt


# --------------------------------------------------------------------------- answering from it

def layered(tmp: Path, mine: dict | None = None) -> dict:
    put(tmp, "a.html", resume())
    return overlay_derived(mine or {}, derived_in(tmp))


def test_a_question_about_years_is_answered_from_the_resumes_and_says_so():
    with tmp_cwd() as tmp:
        facts = layered(tmp, {"experience": {"total_years": None, "years_by_skill": {"JavaScript": None, "Python": None}}})
        a = ask("How many years of work experience do you have with JavaScript or modern JS frameworks (e.g., Vue, React)?", facts)
        assert isinstance(a, Answer) and a.value == "4" and a.source == "derived"                                # JS 3.5, Vue 2.0, React 3.5: 3.5 -> 4
        assert a.basis.startswith("experience.years_by_skill.") and "implied by" in a.basis or "named in dated work" in a.basis
        py = ask("How many years of Python experience do you have?", facts)
        assert py.value == "4" and py.source == "derived" and "implied by Django" in py.basis                      # 3.8 -> 4
        total = ask("Total years of professional experience", facts, input_type="number")
        assert total.value == "7" and total.source == "derived" and total.basis.startswith("experience.total_years (dates of 2 job(s)")
        docker = ask("Years of experience with Docker", facts)
        assert docker.value == "7" and "no dated job names it" in docker.basis
        go = ask("Years of Go experience", facts)
        assert go.value == "5" and "dated projects only" in go.basis
        chosen = ask("Years of experience with Python", facts, kind="select", options=["Less than 3 years", "3-5 years", "5+ years"])
        assert chosen.value == "3-5 years" and chosen.source == "derived"
        # technologies the resumes never show are not claimed, whatever the career length
        for rust in ("Years of experience with Rust?", "How many years of Kubernetes experience do you have?"):
            assert isinstance(ask(rust, facts), Blocked)


def test_the_career_total_is_only_offered_when_the_question_names_nothing_specific():
    with tmp_cwd() as tmp:
        facts = layered(tmp, {})
        for general in ("How many years of professional experience do you have?", "Total years of work experience", "How many years of experience do you have in total?",
                        "Years of professional software development experience", "How many years of relevant experience do you have in this role?",
                        "Number of years of experience", "How many years of experience do you have as a software engineer?"):
            a = ask(general, facts, input_type="number")
            assert isinstance(a, Answer) and a.value == "7" and a.source == "derived", general
        # a technology, field or industry named ANYWHERE in the question: never your whole career
        for specific in ("How many years of Kubernetes experience do you have?", "Years of Kubernetes experience", "Kubernetes experience in years",
                         "How many years of Rust experience?", "Years of experience in the financial services industry", "How many years of management experience do you have?",
                         "Years of experience with machine learning", "How many years of AWS experience do you have?", "Years of leadership experience",
                         "Years of experience working with Terraform"):
            assert isinstance(ask(specific, facts), Blocked), specific
        # the same wording for a technology the resumes DO show is answered for that technology, not with the total
        assert ask("How many years of Python experience do you have?", facts).value == "4"
        assert ask("Years of Docker experience", facts).value == "7"


def test_your_own_statement_is_used_exactly_as_written_and_labelled_fact():
    with tmp_cwd() as tmp:
        facts = layered(tmp, {"experience": {"total_years": 10, "years_by_skill": {"Python": 6.5, "Go": 0.4}}})
        py = ask("Years of Python experience", facts)
        assert (py.value, py.source, py.basis) == ("6.5", "fact", "experience.years_by_skill.Python")            # not rounded, not relabelled
        assert ask("Years of Go experience", facts).value == "0.4"
        t = ask("Total years of professional experience", facts)
        assert (t.value, t.source) == ("10", "fact")


def test_several_technologies_in_one_question_get_the_best_of_them_unless_all_are_required():
    with tmp_cwd() as tmp:
        facts = layered(tmp, {})
        # Python 3.8, Django 3.8, React 3.5, Go 5.3, Vue 2.0
        assert ask("Years of experience with Go or Python", facts).value == "5"
        assert ask("Years of experience with Go and Vue", facts).value == "2"                                  # both are required: the weaker one
        assert ask("Years of experience with frontend frameworks (e.g. Vue, React)", facts).value == "4"
        assert ask("Years of experience with Go and Vue (or similar)", facts).value == "5"                              # "or similar" makes it a list of examples


def test_the_stage3_loader_layers_the_resumes_under_facts_yaml_and_never_stops_a_cycle():
    from stage3_apply.run import _load_facts_or_empty

    with tmp_cwd() as tmp:
        put(tmp, "a.html", resume(northwind="Mar 2019 - Dec 2022").replace("Jan 2023 - Present", "Jan 2023 - Dec 2024"))
        (tmp / "facts.yaml").write_text("experience:\n  years_by_skill:\n    Django: 12\n    Python: null\nlocation:\n  city: Austin\n", encoding="utf-8")
        cfg = {"paths": {"facts": "facts.yaml", "source_dir": "me"}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            facts = _load_facts_or_empty(cfg)
        assert facts["experience"]["years_by_skill"]["Django"] == 12 and facts["experience"]["years_by_skill"]["Python"] == 3.8 and facts["location"]["city"] == "Austin"
        assert facts["experience"]["total_years"] == 5.8 and "Resumes read (1 files)" in out.getvalue() and "5.8 years of work" in out.getvalue()
        out = io.StringIO()
        with patch("stage3_apply.run.ensure_derived", side_effect=RuntimeError("boom")), contextlib.redirect_stdout(out):
            plain = _load_facts_or_empty(cfg)
        assert plain["experience"]["years_by_skill"]["Django"] == 12 and "total_years" not in plain["experience"] and "Could not read your resumes" in out.getvalue()
        (tmp / "facts.yaml").unlink()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            only_resumes = _load_facts_or_empty(cfg)                                                           # no facts.yaml at all: the resumes still answer
        assert only_resumes["experience"]["years_by_skill"]["Django"] == 3.8 and "No facts.yaml" in out.getvalue()
        assert _load_facts_or_empty({"paths": {"facts": "facts.yaml"}}) == {}                                   # no source folder configured: as before


def test_a_new_fact_unparks_only_the_jobs_that_were_waiting_for_nothing_else():
    from datetime import timedelta

    from answerer import release_answerable
    from form_questions import normalize_key
    from state import StateStore
    from tests.test_tui_data import add_app

    js = "How many years of work experience do you have with JavaScript or modern JS frameworks (e.g., Vue, React)?"
    docker = "Years of experience with Docker"
    rust = "Years of experience with Rust?"
    pending = lambda label, kind="text", options=(): {"label": label, "kind": kind, "options": list(options), "reason": "no fact"}      # noqa: E731
    with tmp_cwd() as tmp:
        facts = layered(tmp, {})
        store = StateStore(tmp / "state.db")
        run = store.start_run("stage3", "h", "p")
        for job_id in ("j1", "j2", "j3", "j4"):
            add_app(store, run, job_id, "needs_review", timedelta(hours=-1), code="needs_answers", message="needs answers")
        store.add_pending_questions("j1", [pending(js)])
        store.add_pending_questions("j2", [pending(js), pending("Why do you want to work here?", "textarea")])           # still has an essay to write
        store.add_pending_questions("j3", [pending(rust)])                                                               # the resumes never show Rust
        store.add_pending_questions("j4", [pending(docker, "select", ["Less than 1 year", "1-2 years"])])                # an answer the options cannot express
        ctx = AnswerContext(profile={"skills": []}, facts=facts, bank_get=store.get_answer)

        assert release_answerable(store, ctx) == (1, 1)                                                                  # one question resolved, one job queued again
        left = {r["job_id"] for r in store.conn.execute("SELECT job_id FROM applications WHERE status = 'needs_review'")}
        assert left == {"j2", "j3", "j4"}                                                                                # only j1 was waiting for the JavaScript answer alone
        assert {r["question_key"] for r in store.list_pending()} == {normalize_key("Why do you want to work here?"), normalize_key(rust), normalize_key(docker)}
        assert release_answerable(store, ctx) == (0, 0)                                                                  # nothing new: nothing happens

        store.put_answer(normalize_key(rust), rust, "3")                                                                 # a saved answer counts the same way
        assert release_answerable(store, ctx) == (1, 1) and {r["job_id"] for r in store.conn.execute("SELECT job_id FROM applications")} >= {"j2", "j4"}
        assert release_answerable(store, AnswerContext(profile={"skills": []}, facts={}, bank_get=store.get_answer)) == (0, 0)       # without the resumes' facts nothing is released
        store.close()
    run_py = (Path(__file__).resolve().parent.parent / "stage3_apply" / "run.py").read_text(encoding="utf-8")
    assert "release_answerable(state" in run_py, "stage 3 must re-check the parked questions once per run"


# --------------------------------------------------------------------------- the command

def test_the_command_syncs_shows_and_checks_without_deleting_anything():
    with tmp_cwd() as tmp:
        origin = tmp / "resume-template"
        (origin / "resume-versions").mkdir(parents=True)
        (origin / "out").mkdir()
        (origin / "resume-versions" / "resume-backend.md").write_text(resume(), encoding="utf-8")
        (origin / "resume.html").write_text(resume(), encoding="utf-8")
        (origin / "out" / "resume-base.pdf").write_bytes(b"%PDF-1.4 fake")
        (origin / "styles.css").write_text("body{}", encoding="utf-8")
        (tmp / "config.yaml").write_text(f"paths:\n  source_dir: me\nsource:\n  sync_from: {origin.as_posix()}\n", encoding="utf-8")
        run = lambda *args: _run(["--config", str(tmp / "config.yaml"), *args])                                # noqa: E731

        code, out = run("init")
        assert code == 0 and (tmp / "me" / "resumes").is_dir() and (tmp / "me" / "experience").is_dir() and "trusts what is in this folder" in (tmp / "me" / "README.md").read_text(encoding="utf-8")
        code, out = run("sync")
        assert code == 0 and "3 added, 0 updated, 0 unchanged" in out and "Nothing was deleted" in out and re.search(r"Computed from 2 file\(s\): \d\.\d years of work", out)
        assert sorted(p.name for p in (tmp / "me" / "resumes").iterdir()) == ["resume-backend.md", "resume-base.html", "resume-base.pdf"]       # styles.css is not a resume
        (tmp / "me" / "resumes" / "mine-only.md").write_text("## Experience\n### Dev - Acme (2001 - 2002)\n", encoding="utf-8")
        code, out = run("sync")
        assert code == 0 and "0 added, 0 updated, 3 unchanged" in out and (tmp / "me" / "resumes" / "mine-only.md").is_file()        # idempotent, and it never deletes
        (origin / "resume-versions" / "resume-backend.md").write_text(resume(northwind="Mar 2019 - Dec 2021"), encoding="utf-8")
        assert "1 updated" in run("sync")[1]

        code, out = run("show")
        assert code == 0 and "Work: " in out and "Python" in out and "implied by Django" in out and "wins over all of this" in out
        code, out = run("check")
        assert "the resume says 7+ years" in out and ("PROBLEM" in out) == (code == 1)

        (origin / "resume-versions" / "resume-backend.md").write_text(resume(summary=SUMMARY.replace("7+", "12+")), encoding="utf-8")
        run("sync")
        code, out = run("check")
        assert code == 1 and "PROBLEM  your resume says 12+ years but the job dates add up to" in out

        code, out = _run(["--config", str(tmp / "config.yaml"), "sync", "--from", str(tmp / "nowhere")], expect_exit=True)
        assert "does not exist" in out
        (tmp / "config2.yaml").write_text("paths: {}\n", encoding="utf-8")
        assert "paths.source_dir is not set" in _run(["--config", str(tmp / "config2.yaml"), "show"], expect_exit=True)[1]


def _run(argv, expect_exit=False):
    out = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out):
        try:
            code = source.main(argv)
        except SystemExit as e:
            if not expect_exit:
                raise
            print(e.code)
            code = 1
    return code, out.getvalue()


def test_the_resumes_folder_is_never_committed_and_never_read_by_accident():
    ignore = (Path(__file__).resolve().parent.parent / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "/me/" in ignore, "the source of truth holds personal resumes: it must stay out of the public repository"
    cfg = __import__("yaml").safe_load((Path(__file__).resolve().parent.parent / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["paths"]["source_dir"] == "me" and cfg["source"]["experience"] == {"count": "jobs", "undated_skills": "career"}
    assert os.environ.get("NOT_A_REAL_VAR") is None
