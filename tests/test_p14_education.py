"""Degrees, grades and "how many companies" come from the dated lines of the resumes -- worked out, never guessed.

Everything here is fictional (Harbor Technical Institute, Lakeside University, Northwind Labs ...). The numbers are worked out by hand in
the comments. Two forms that asked for this and found nothing were parked for a person: "how many companies in the past ten years
(since your first degree)" and "was your first degree completed in 2025 or 2026".
"""

from __future__ import annotations

from datetime import date

import resume_facts as rf
import source
from answerer import Answer, AnswerContext, Blocked, apply_rules
from facts import overlay_derived
from form_questions import Question
from tests.test_p0_safety import tmp_cwd
from tests.test_p7_source import cfg_for, job, put

TODAY = date(2026, 6, 15)
INDEX = lambda y, m: y * 12 + m - 1                                                              # noqa: E731


def item(school_line: str, degree_line: str) -> str:
    return f'<div class="education-item"><div>{school_line}</div><div>{degree_line}</div></div>'


HARBOR = item("Harbor Technical Institute, Harbor City, Ohio, USA.", "Bachelor's Degree in Computer Science, GPA: 3.4/4.00 Aug 2015 - May 2019")
LAKESIDE = item("Lakeside University, Lakeside, Ontario, Canada<br><i>Relevant Coursework/Research Interests: Databases, Compilers</i>",
                "Master's Degree in Data Science, 2025 - Current.")


def page(education: str, jobs: str = "") -> str:
    return (f'<div class="resumeContainer"><div><h1>Alex Rivera</h1></div><div><h2>Education</h2>{education}</div>'
            f'<div><h2>Professional Experience</h2>{jobs}</div></div>')


def work(*stints: tuple[str, str]) -> str:
    return "".join(job("Software Engineer", company, dates, ["<b>Built</b> things with <b>Python</b>."]) for company, dates in stints)


def degrees(html: str):
    return rf.parse_template_education(html, "alex.html", TODAY)


# --------------------------------------------------------------------------- reading one education entry

def test_a_degree_is_read_with_its_school_field_grade_and_the_month_it_ended():
    bachelor, = degrees(HARBOR)
    assert (bachelor.degree, bachelor.field, bachelor.school) == ("Bachelor's Degree", "Computer Science", "Harbor Technical Institute")
    assert (bachelor.gpa, bachelor.scale, bachelor.finished, bachelor.month_known) == (3.4, 4.0, True, True)
    assert bachelor.end == INDEX(2019, 5)                                                       # "May 2019": that month is the last one studied
    master, = degrees(LAKESIDE)
    assert (master.degree, master.field, master.school) == ("Master's Degree", "Data Science", "Lakeside University")     # the coursework line is not the school
    assert (master.finished, master.gpa) == (False, None) and master.end == INDEX(2026, 6)     # "Current" is this month: still studying


def test_the_other_ways_resumes_write_a_degree():
    cases = [
        # (school line, degree line, level, field, school, finished, gpa, scale)
        ("MS in Computer Science at The University of Lakeside.", "Aug 2025 - May 2027", 3, "Computer Science", "The University of Lakeside", False, None, None),
        ("<b>Harbor Technical Institute (HTI)</b>, Harbor City, Ohio", "<b>Bachelor of Science in Computer Science</b>, GPA: 3.8/4.0, 2015 &ndash; 2019",
         2, "Computer Science", "Harbor Technical Institute", True, 3.8, 4.0),
        ("Riverside College, Oxford, MS", "Associate Degree in Welding, 2010", 1, "Welding", "Riverside College", True, None, None),       # "MS" here is Mississippi
        ("Boston University, Boston, MA", "Bachelor of Arts, 2014", 2, "", "Boston University", True, None, None),                         # "MA" is Massachusetts
        ("Example University", "B.S. 2010 - 2014", 2, "", "Example University", True, None, None),
        ("Hill University", "Ph.D. in Physics, 2012 - 2017", 4, "Physics", "Hill University", True, None, None),
        ("Hill University", "Master of Computer Science, expected May 2027", 3, "Computer Science", "Hill University", False, None, None),
        ("Hill University", "Master's Degree in Law, in progress", 3, "Law", "Hill University", False, None, None),
        ("Hill University", "Licenciatura en Informatica, 2012 - 2016", 2, "", "Hill University", True, None, None),
        ("Hill University", "Bachelor's Degree in Computer Science, GPA: 340 2012 - 2016", 2, "Computer Science", "Hill University", True, None, None),     # not a grade
        ("Hill University", "Bachelor's Degree in Computer Science, GPA: 4.7/4.0 2012 - 2016", 2, "Computer Science", "Hill University", True, None, None),   # above its own scale
        ("BS in Computer Systems Engineering at Hill University. <b>GPA: 3.4/4.00</b>", "Aug 2015 - May 2019", 2, "Computer Systems Engineering", "Hill University", True, 3.4, 4.0),
        ("St. Louis University, Missouri", "B.S. in Biology, 2014", 2, "Biology", "St. Louis University", True, None, None),                  # "St." is not the end of a sentence
        ("Downtown Campus, Hill University", "B.S. in Biology, 2014", 2, "Biology", "Hill University", True, None, None),                       # the part that says university
        ("Hill University<br><i>Relevant coursework: Master Data Management, Currently Hiring Trends</i>", "B.S. in Biology, 2014", 2, "Biology", "Hill University", True, None, None),
        ("Hill University", "Master's Degree in Law, after a Bachelor's Degree in History, 2012 - 2014", 3, "Law", "Hill University", True, None, None),       # the first one named
        ("Hill University", "Bachelor's Degree in Biology Aug 2015 - May 2019", 2, "Biology", "Hill University", True, None, None),                      # no comma before the dates
        ("MS in Computer Science at Lakeside University (LU).", "2020 - 2022", 3, "Computer Science", "Lakeside University", True, None, None),
        ("BS in Biology at Hill University GPA: 3.4/4.0", "2012 - 2016", 2, "Biology", "Hill University", True, 3.4, 4.0),
    ]
    for school_line, degree_line, level, field, school, finished, gpa, scale in cases:
        got, = degrees(item(school_line, degree_line))
        assert (got.level, got.field, got.school, got.finished, got.gpa, got.scale) == (level, field, school, finished, gpa, scale), (school_line, degree_line, got)
    assert degrees(item("Hill University", "Certificate in Welding, 2012")) == []             # not a degree
    assert degrees(item("Hill University", "Courses in Boston, MA, 2012")) == []
    assert degrees(item("Universidad de Buenos Aires, Buenos Aires, BA", "Certificate in Welding, 2012")) == []              # "BA" is a province


def test_a_year_alone_ends_in_december_and_says_so_and_a_lone_year_is_the_end():
    year_only, = degrees(item("Hill University", "Bachelor of Science in Biology, 2015 - 2019"))
    assert year_only.end == INDEX(2019, 12) and year_only.month_known is False
    lone, = degrees(item("Hill University", "B.S. in Biology, 2014"))
    assert lone.end == INDEX(2014, 12) and lone.month_known is False
    graduated, = degrees(item("Hill University", "B.S. in Biology, graduated May 2014"))
    assert graduated.end == INDEX(2014, 5) and graduated.month_known is True
    undated, = degrees(item("Hill University", "B.S. in Biology"))
    assert undated.end is None and undated.finished is True                                    # no dates and no "expected": counted as finished


def test_a_degree_that_ends_this_month_or_later_is_not_finished_yet():
    ends = lambda text: degrees(item("Hill University", text))[0].finished                       # noqa: E731
    assert ends("B.S. in Biology, 2022 - May 2026") is True                                     # last month: done
    assert ends("B.S. in Biology, 2022 - Jun 2026") is False                                    # this month: not yet conferred
    assert ends("B.S. in Biology, 2022 - Jul 2026") is False


def test_the_markdown_layout_is_read_too():
    md = ("## Education\n"
          "**Lakeside University** - Master's Degree in Data Science (2025 - Present)\n"
          "- Relevant coursework: Databases\n"
          "### Harbor Technical Institute\n"
          "Bachelor of Science in Computer Science, GPA 3.7 / 4.0, Aug 2014 - May 2018\n"
          "- Dean's list\n\n"
          "## Skills\n- Python\n")
    with tmp_cwd() as tmp:
        parsed = rf.parse_resume(put(tmp, "alex.md", md), TODAY)
        master, bachelor = parsed.education
        assert (master.degree, master.field, master.school, master.finished) == ("Master's Degree", "Data Science", "Lakeside University", False)
        assert (bachelor.degree, bachelor.field, bachelor.school, bachelor.gpa, bachelor.scale) == ("Bachelor's Degree", "Computer Science", "Harbor Technical Institute", 3.7, 4.0)
        assert bachelor.end == INDEX(2018, 5) and bachelor.finished
        assert parsed.listed == ["Python"], "the education heading must not swallow the sections after it"


def test_markdown_lines_that_each_hold_a_whole_degree_are_separate_entries():
    md = "## Education\nMaster of Law, Hill University, 2012 - 2014\nBachelor of Arts in History, Dale College, 2008 - 2012\n"
    with tmp_cwd() as tmp:
        master, bachelor = rf.parse_resume(put(tmp, "alex.md", md), TODAY).education
        assert (master.degree, master.school, master.field) == ("Master's Degree", "Hill University", "Law")
        assert (bachelor.degree, bachelor.school, bachelor.field) == ("Bachelor's Degree", "Dale College", "History")


# --------------------------------------------------------------------------- what the resumes together say

def derive_with(tmp, **files):
    for name, text in files.items():
        put(tmp, name, text)
    return rf.derive(cfg_for(tmp), TODAY)


def test_the_highest_finished_degree_is_the_answer_and_one_still_in_progress_is_not():
    with tmp_cwd() as tmp:
        data = derive_with(tmp, **{"a.html": page(LAKESIDE + HARBOR)})
        edu, prov = data["education"], data["provenance"]
        assert edu == {"highest_degree": "Bachelor's Degree", "school": "Harbor Technical Institute", "field_of_study": "Computer Science", "gpa": 3.4,
                       "gpa_degree": "Bachelor's Degree", "gpa_scale": 4.0, "first_degree_graduated": "2019-05", "first_degree_year": 2019}
        assert "Harbor Technical Institute" in prov["education.highest_degree"] and "still in progress: Master's Degree at Lakeside University" in prov["education.highest_degree"]
        assert "GPA 3.4/4" in prov["education.gpa"] and "a.html" in prov["education.gpa"] and "ended 2019-05" in prov["education.school"]
    with tmp_cwd() as tmp:                                                                      # the master's is finished, and states no grade
        done = item("Lakeside University", "Master's Degree in Data Science, 2023 - 2025 Mar")
        data = derive_with(tmp, **{"a.html": page(done + HARBOR)})
        edu = data["education"]
        assert (edu["highest_degree"], edu["school"], edu["field_of_study"]) == ("Master's Degree", "Lakeside University", "Data Science")
        assert "gpa" not in edu, "the bachelor's grade must not be passed off as the master's"
        assert edu["first_degree_year"] == 2019 and edu["first_degree_graduated"] == "2019-05"       # still the bachelor's end
    with tmp_cwd() as tmp:                                                                      # nothing finished yet
        data = derive_with(tmp, **{"a.html": page(LAKESIDE)})
        assert data["education"] == {} and "first_degree_year" not in data["education"]


def test_the_first_degree_is_the_earliest_bachelors_and_a_year_only_end_says_so():
    with tmp_cwd() as tmp:
        two = item("Hill University", "Bachelor of Arts, 2008 - 2012") + HARBOR
        data = derive_with(tmp, **{"a.html": page(two)})
        assert data["education"]["first_degree_year"] == 2012 and data["education"]["first_degree_graduated"] == "2012-12"
        assert "only the year is written" in data["provenance"]["education.first_degree_year"]
    with tmp_cwd() as tmp:
        data = derive_with(tmp, **{"a.html": page(item("Hill University", "Master of Law, 2012 - 2014"))})
        assert "first_degree_year" not in data["education"] and data["education"]["highest_degree"] == "Master's Degree"     # a master's is not "the first bachelor's"


def test_versions_that_disagree_leave_the_value_out_and_say_so_and_a_month_beats_a_bare_year():
    sys_eng = item("Harbor Technical Institute, Harbor City", "Bachelor's Degree in Computer Systems Engineering, GPA: 3.4/4.00 Aug 2015 - May 2019")
    year_only = item("Harbor Technical Institute (HTI), Harbor City", "Bachelor's Degree in Computer Science, GPA: 3.4/4.0, 2015 - 2019")
    with tmp_cwd() as tmp:
        data = derive_with(tmp, **{"a.html": page(HARBOR), "b.html": page(sys_eng), "c.html": page(year_only)})
        edu = data["education"]
        assert "field_of_study" not in edu, "two fields of study: nothing is claimed"
        assert edu["first_degree_graduated"] == "2019-05" and edu["gpa"] == 3.4 and edu["gpa_scale"] == 4.0        # the year-only version agrees on the year, the month wins
        assert len(data["notices"]) == 1 and "field of study" in data["notices"][0]
        assert "Computer Science (a.html, c.html) vs Computer Systems Engineering (b.html)" in data["notices"][0] and "facts.yaml" in data["notices"][0]
    with tmp_cwd() as tmp:
        other_grade = item("Harbor Technical Institute", "Bachelor's Degree in Computer Science, GPA: 3.5/4.00 Aug 2015 - May 2019")
        data = derive_with(tmp, **{"a.html": page(HARBOR), "b.html": page(other_grade)})
        assert "gpa" not in data["education"] and data["education"]["field_of_study"] == "Computer Science"
        assert len(data["notices"]) == 1 and "GPA: 3.4 (a.html) vs 3.5 (b.html)" in data["notices"][0]
    with tmp_cwd() as tmp:
        other_month = item("Harbor Technical Institute", "Bachelor's Degree in Computer Science, GPA: 3.4/4.00 Aug 2015 - Jun 2019")
        data = derive_with(tmp, **{"a.html": page(HARBOR), "b.html": page(other_month)})
        assert "first_degree_graduated" not in data["education"] and "month it ended" in data["notices"][0]
    with tmp_cwd() as tmp:                                                                      # versions that agree say nothing
        data = derive_with(tmp, **{"a.html": page(HARBOR), "b.html": page(HARBOR.replace("Harbor Technical Institute,", "The Harbor Technical Institute,"))})
        assert data["notices"] == [] and data["education"]["field_of_study"] == "Computer Science"
        assert "a.html, b.html" in data["provenance"]["education.school"], "'The Harbor ...' and 'Harbor ...' are one school"


def test_versions_that_disagree_only_about_a_degree_still_in_progress_say_nothing():
    with tmp_cwd() as tmp:
        later = item("Lakeside University", "Master's Degree in Data Science, Aug 2025 - May 2027")
        data = derive_with(tmp, **{"a.html": page(LAKESIDE + HARBOR), "b.html": page(later + HARBOR)})
        assert data["notices"] == [] and data["education"]["highest_degree"] == "Bachelor's Degree"


def test_versions_that_disagree_about_whether_a_degree_is_finished_count_it_as_not_finished():
    with tmp_cwd() as tmp:
        ended = item("Lakeside University", "Master's Degree in Data Science, 2023 - 2025 Mar")
        data = derive_with(tmp, **{"a.html": page(ended + HARBOR), "b.html": page(item("Lakeside University", "Master's Degree in Data Science, 2025 - Current") + HARBOR)})
        assert data["education"]["highest_degree"] == "Bachelor's Degree"
        assert any("some versions say it is finished" in n for n in data["notices"])


# --------------------------------------------------------------------------- how many companies

JOBS = work(("Edge Co", "Jan 2015 - Jun 2016"), ("Edge Two Co", "Jan 2015 - Jul 2016"), ("Tiny Co", "Jan 2018 - Mar 2019"),
            ("Grad Co", "Jan 2019 - May 2019"), ("June Co", "Jan 2019 - Jun 2019"),
            ("Northwind Labs", "Mar 2019 - Dec 2022"), ("Contoso Systems", "Jan 2023 - Present"), ("Northwind Labs", "Jan 2024 - Mar 2024"),
            ("Old Corp", "Jan 2010 - Dec 2013"), ("Soon Co", "Sep 2026 - Dec 2026"))


def test_companies_are_counted_by_the_dates_of_the_jobs_and_each_employer_once():
    with tmp_cwd() as tmp:
        data = derive_with(tmp, **{"a.html": page(HARBOR, JOBS)})
        exp, prov = data["experience"], data["provenance"]
        # now = Jun 2026, so the ten years are Jul 2016 .. Jun 2026. Edge Co ended Jun 2016 (before), Edge Two Co ended Jul 2016 (one month inside),
        # Old Corp ended 2013 (long before), Soon Co starts in Sep 2026 (not worked yet). The first degree ended May 2019, so "since" starts Jun 2019:
        # Tiny Co ended Mar 2019 and Grad Co May 2019 (before), June Co ended Jun 2019 (it worked a month after).
        assert exp["companies_last_10_years"] == 6                                              # Edge Two, Tiny, Grad, June, Northwind (two stints, once), Contoso
        assert exp["companies_since_first_degree"] == 3                                         # June, Northwind (it carried on past May 2019), Contoso
        assert exp["companies_since_first_degree_last_10_years"] == 3
        last10 = prov["experience.companies_last_10_years"]
        assert "Edge Two Co, Tiny Co, Grad Co, June Co, Northwind Labs, Contoso Systems" in last10 and "2016-07..2026-06" in last10
        assert "Edge Co," not in last10 and "Old Corp" not in last10 and "Soon" not in last10
        since = prov["experience.companies_since_first_degree"]
        assert "June Co, Northwind Labs, Contoso Systems" in since and "2019-06..2026-06" in since
        assert prov["experience.companies_since_first_degree_last_10_years"].count("Northwind Labs") == 1


def test_a_first_degree_older_than_ten_years_leaves_the_ten_year_window_as_the_limit():
    with tmp_cwd() as tmp:
        old = item("Hill University", "Bachelor of Arts, 2004 - 2008")
        data = derive_with(tmp, **{"a.html": page(old, JOBS)})
        exp = data["experience"]
        # the degree ended Dec 2008 (a bare year): everything since, but not Soon Co: Edge, Edge Two, Old Corp (2010-2013), Tiny, Grad, June, Northwind, Contoso
        assert exp["companies_last_10_years"] == 6 and exp["companies_since_first_degree"] == 8
        assert exp["companies_since_first_degree_last_10_years"] == 6, "both limits: the later start wins"


def test_without_a_finished_first_degree_only_the_ten_year_count_is_given():
    with tmp_cwd() as tmp:
        data = derive_with(tmp, **{"a.html": page(LAKESIDE, JOBS)})
        exp = data["experience"]
        assert exp["companies_last_10_years"] == 6 and "companies_since_first_degree" not in exp and "companies_since_first_degree_last_10_years" not in exp


def test_no_count_is_given_when_a_job_names_no_company_or_there_are_no_jobs():
    with tmp_cwd() as tmp:
        nameless = ('<ul><h3><div class="carrer-item"><div><b>Freelance Developer</b></div><div class="date-item">Jan 2021 - Dec 2021</div></div></h3>'
                    '<li><b>Built</b> sites.</li></ul>')
        data = derive_with(tmp, **{"a.html": page(HARBOR, JOBS + nameless)})
        assert not [k for k in data["experience"] if k.startswith("companies_")]
        assert any("names no company" in n for n in data["notices"])
    with tmp_cwd() as tmp:
        data = derive_with(tmp, **{"a.html": page(HARBOR)})
        assert not [k for k in data["experience"] if k.startswith("companies_")], "no dated job: not 'zero companies'"


def test_the_stored_file_is_rewritten_when_the_derivation_learns_something_new():
    with tmp_cwd() as tmp:
        files = [put(tmp, "a.html", page(HARBOR, JOBS))]
        before = rf.fingerprint(files, {}, TODAY)
        assert rf.fingerprint(files, {}, TODAY) == before
        old = rf.DERIVE_VERSION
        try:
            rf.DERIVE_VERSION = old + 1
            assert rf.fingerprint(files, {}, TODAY) != before
        finally:
            rf.DERIVE_VERSION = old


# --------------------------------------------------------------------------- facts.yaml wins, and the report says what it found

def test_what_you_state_wins_and_the_rest_comes_from_the_resumes_with_the_reason():
    with tmp_cwd() as tmp:
        derived = derive_with(tmp, **{"a.html": page(HARBOR, JOBS)})
        merged = overlay_derived({"education": {"highest_degree": "Master's Degree", "school": None}}, derived)
        assert merged["education"]["highest_degree"] == "Master's Degree"                       # yours
        assert merged["education"]["school"] == "Harbor Technical Institute" and merged["experience"]["companies_last_10_years"] == 6
        assert "education.school" in merged["_provenance"] and "education.highest_degree" not in merged["_provenance"]


def test_the_source_report_names_the_degree_the_counts_and_what_to_look_at(capsys=None):
    import contextlib
    import io
    import types

    sys_eng = item("Harbor Technical Institute", "Bachelor's Degree in Computer Systems Engineering, GPA: 3.4/4.00 Aug 2015 - May 2019")
    with tmp_cwd() as tmp:
        put(tmp, "a.html", page(HARBOR, JOBS))
        put(tmp, "b.html", page(sys_eng, JOBS))
        cfg = cfg_for(tmp)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            source.cmd_show(cfg, types.SimpleNamespace(all=False))
            source.cmd_check(cfg, types.SimpleNamespace())
        text = out.getvalue()
        assert "Bachelor's Degree" in text and "first_degree_year" in text and "companies_last_10_years" in text
        assert "PROBLEM" in text and "field of study" in text


# --------------------------------------------------------------------------- the answers

FACTS = {
    "education": {"highest_degree": "Bachelor's Degree", "school": "Harbor Technical Institute", "field_of_study": "Computer Science", "gpa": 3.4,
                  "gpa_degree": "Bachelor's Degree", "gpa_scale": 4.0, "first_degree_graduated": "2019-05", "first_degree_year": 2019},
    "experience": {"companies_last_10_years": 4, "companies_since_first_degree": 2, "companies_since_first_degree_last_10_years": 2},
    "_provenance": {"education.gpa": "written as GPA 3.4/4", "education.first_degree_year": "ended 2019-05", "education.highest_degree": "the highest finished degree",
                    "experience.companies_last_10_years": "employers: A, B", "experience.companies_since_first_degree": "employers: C",
                    "experience.companies_since_first_degree_last_10_years": "employers: D"},
}


def ask(label, facts=FACTS, *, kind="text", options=None, input_type="text"):
    q = Question(1, kind, label, options or [], True, "", input_type)
    return apply_rules(q, AnswerContext(profile={"skills": []}, facts=facts))


def test_the_ten_year_company_question_is_answered_from_the_dates_for_each_window():
    since_and_ten = ask("In the past ten years, looking only at the time since you graduated your first undergraduate degree, how many companies have you worked for?",
                        kind="combobox", options=["0", "1", "2", "3", "4+"])
    assert isinstance(since_and_ten, Answer) and since_and_ten.value == "2" and since_and_ten.source == "derived"
    assert since_and_ten.basis == "experience.companies_since_first_degree_last_10_years (employers: D)"
    ten = ask("How many companies have you worked for in the last 10 years?", kind="select", options=["1-2", "3-5", "6+"])
    assert ten.value == "3-5" and ten.basis.startswith("experience.companies_last_10_years")
    since = ask("Since you graduated from university, how many different employers have you had?", input_type="number")
    assert since.value == "2" and since.basis.startswith("experience.companies_since_first_degree (")
    assert ask("What is the number of companies you worked for in the previous ten years", kind="select", options=["4", "6"]).value == "4"


def test_other_company_questions_are_not_hijacked_or_guessed():
    assert ask("How many companies have you worked for in the last 5 years?") is None                 # a window nobody worked out
    assert ask("How many companies have you worked for?") is None
    assert ask("How many companies have you worked for since 2020?") is None                        # "since" what? only "since your degree" is worked out
    gone = ask("How many years of experience do you have working at tech companies?")
    assert isinstance(gone, Blocked) and "not in your skills" in gone.reason, "that is a years question, answered (or refused) as one"
    missing = ask("How many companies have you worked for in the last ten years?", facts={"experience": {}})
    assert isinstance(missing, Blocked) and "companies_last_10_years" in missing.reason
    no_degree = ask("Since you graduated, how many companies have you worked for?", facts={"experience": {"companies_last_10_years": 4}})
    assert isinstance(no_degree, Blocked) and "companies_since_first_degree" in no_degree.reason


def test_the_first_degree_year_question_is_a_yes_or_a_no_from_the_year_it_ended():
    label = "Please confirm that your first university degree (bachelor or undergrad) completed, or is expected to complete, in 2025 or 2026."
    no = ask(label, kind="combobox", options=["Yes", "No"])
    assert isinstance(no, Answer) and no.value == "No" and no.source == "derived" and no.basis == "education.first_degree_year (ended 2019-05)"
    this_year = dict(FACTS, education=dict(FACTS["education"], first_degree_year=2026))
    assert ask(label, this_year, kind="radio", options=["Yes", "No"]).value == "Yes"
    last_year = dict(FACTS, education=dict(FACTS["education"], first_degree_year=2025))
    assert ask(label, last_year, kind="radio", options=["Yes", "No"]).value == "Yes"
    assert ask("Did you graduate your bachelor's degree between 2018 and 2020?", kind="radio", options=["Yes", "No"]).value == "Yes"       # 2019 is inside
    assert ask("Did you graduate your bachelor's degree between 2020 and 2022?", kind="radio", options=["Yes", "No"]).value == "No"
    assert ask("Was your first degree completed in 2019?", kind="radio", options=["Yes", "No"]).value == "Yes"
    assert ask("Was your first degree completed in 2020, 2021 or 2022?", kind="radio", options=["Yes", "No"]).value == "No"
    assert ask("Was your first degree completed in 2018 or 2020?", kind="radio", options=["Yes", "No"]).value == "No"      # a list is not a range: 2019 is between them
    assert ask("Did you complete your first degree before 2020?", kind="radio", options=["Yes", "No"]) is None          # a comparison: not answered here
    assert ask("When did you complete your first degree?") is None                                                       # no year in the question
    missing = ask(label, facts={"education": {}}, kind="radio", options=["Yes", "No"])
    assert isinstance(missing, Blocked) and "first_degree_year" in missing.reason


def test_a_gpa_is_the_one_on_the_resume_on_the_scale_it_was_given_and_never_another_schools():
    plain = ask("What is your GPA?")
    assert isinstance(plain, Answer) and plain.value == "3.4" and plain.source == "derived" and plain.basis == "education.gpa (written as GPA 3.4/4)"
    assert ask("GPA (out of 4.0)", input_type="number").value == "3.4" and ask("Your cumulative grade point average on a 4.0 scale").value == "3.4"
    assert ask("Undergraduate GPA").value == "3.4" and ask("Bachelor's degree GPA").value == "3.4"
    for label in ("High school GPA", "Master's GPA", "Graduate GPA", "MBA GPA", "PhD GPA", "Secondary school grade point average", "GPA at your preparatory school"):
        assert ask(label) is None, label                                                                  # not this degree's grade: left to your notes and to you
    for label in ("GPA out of 5.0", "GPA on a 10 point scale", "GPA (out of 4.5)"):
        wrong = ask(label)
        assert isinstance(wrong, Blocked) and "your GPA is 3.4 out of 4" in wrong.reason, label
    assert isinstance(ask("GPA as a percentage (%)"), Blocked)
    assert isinstance(ask("What is your GPA?", facts={"education": {}}), Blocked)
    after_master = dict(FACTS, education=dict(FACTS["education"], gpa=3.9, gpa_degree="Master's Degree"))
    assert ask("Undergraduate GPA", after_master) is None, "the grade on file belongs to the master's"
    assert ask("What is your GPA?", after_master).value == "3.9"                                                         # an unqualified GPA is the highest degree's
    unscaled = dict(FACTS, education={"gpa": 3.4})
    assert ask("What is your GPA?", unscaled).value == "3.4" and isinstance(ask("GPA out of 4.0", unscaled), Blocked)


def test_the_degree_school_and_field_questions_say_they_come_from_your_resumes():
    highest = ask("What is your highest level of education?", kind="select", options=["High school", "Bachelor's degree", "Master's degree"])
    assert highest.value == "Bachelor's degree" and highest.source == "derived" and highest.basis.startswith("education.highest_degree (the highest finished degree")
    stated = ask("What is your highest level of education?", facts={"education": {"highest_degree": "Bachelor's Degree"}}, kind="select", options=["Bachelor's degree"])
    assert stated.source == "fact" and stated.basis == "education.highest_degree"                                       # you wrote it yourself
    assert isinstance(ask("What is your field of study?", facts={"education": {"highest_degree": "Bachelor's Degree"}}), Blocked)
