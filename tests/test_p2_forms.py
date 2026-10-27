"""The form-handling JavaScript, tested in REAL Chromium and Firefox against static HTML (no network, no profile).

If a browser is not installed the tests are skipped with a message rather than failing: the logic that sits on
top of this layer is covered by test_p2_answers.py with plain fakes.
"""

from __future__ import annotations

import contextlib
from unittest.mock import patch

from answerer import QuestionSolver
from form_questions import parse_questions
from stage3_apply.run import PlaywrightSession
from tests import form_fixtures as F

ENGINES = ("chromium", "firefox")


class _Skip(Exception):
    pass


@contextlib.contextmanager
def pages(html: str):
    """Yield [(engine, PlaywrightSession, page)] with ``html`` loaded in every available engine."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        yield []
        return
    with sync_playwright() as p:
        browsers, opened = [], []
        try:
            for engine in ENGINES:
                try:
                    b = getattr(p, engine).launch(headless=True)
                except Exception:
                    continue                                    # this engine is not installed here
                browsers.append(b)
                page = b.new_page()
                page.set_content(html)
                opened.append((engine, PlaywrightSession(page), page))
            yield opened
        finally:
            for b in browsers:
                b.close()


def _by_label(questions, fragment):
    matches = [q for q in questions if fragment.lower() in q.label.lower()]
    assert len(matches) == 1, f"expected exactly one question containing {fragment!r}, got {[q.label for q in questions]}"
    return matches[0]


def _require_a_browser(opened):
    if not opened:
        print("  (skipped: no Playwright browser available)")
    return bool(opened)


def test_extractor_reads_a_greenhouse_style_form_in_every_engine():
    with pages(F.GREENHOUSE_LIKE) as opened:
        if not _require_a_browser(opened):
            return
        for engine, session, _page in opened:
            qs = parse_questions(session.extract_questions())
            labels = [q.label for q in qs]
            assert len(qs) == 10, (engine, labels)                       # not the file input, the hidden token or the display:none trap
            assert "Hidden trap" not in labels and not any("Resume" in l for l in labels)
            auth = _by_label(qs, "legally authorized")
            assert (auth.kind, auth.required, auth.options) == ("select", True, ["Yes", "No"]), engine   # the "Select..." placeholder is not an option
            assert _by_label(qs, "years of experience").input_type == "number"
            assert _by_label(qs, "Why do you want").kind == "textarea"
            assert _by_label(qs, "privacy policy").kind == "checkbox" and _by_label(qs, "privacy policy").required
            assert not _by_label(qs, "newsletter").required and not _by_label(qs, "LinkedIn").required
            assert _by_label(qs, "Gender").options == ["Male", "Female", "Decline To Self Identify"]
            assert _by_label(qs, "First Name").label == "First Name"      # the required asterisk is not part of the question


def test_extractor_scopes_to_the_open_dialog_and_merges_screen_reader_duplicates():
    with pages(F.MODAL_LIKE) as opened:
        if not _require_a_browser(opened):
            return
        for engine, session, _page in opened:
            qs = parse_questions(session.extract_questions())
            assert [q.label for q in qs] == [
                "Years of work experience with Node.js?", "Are you comfortable working remotely?", "English proficiency", "Which of these do you know?",
            ], engine                                                    # the form behind the dialog is ignored; visible + sr-only copies merged
            assert [q.kind for q in qs] == ["text", "radio", "select", "checkbox_group"]
            assert qs[1].options == ["Yes", "No"] and qs[3].options == ["Python", "Go", "Rust"]


def test_every_control_type_can_be_filled_and_reads_back():
    with pages(F.GREENHOUSE_LIKE) as opened:
        if not _require_a_browser(opened):
            return
        for engine, session, page in opened:
            qs = parse_questions(session.extract_questions())
            session.fill_answer(_by_label(qs, "legally authorized"), "Yes")
            session.fill_answer(_by_label(qs, "require sponsorship"), "No")
            session.fill_answer(_by_label(qs, "years of experience"), "8")
            session.fill_answer(_by_label(qs, "Why do you want"), "Because.")
            session.fill_answer(_by_label(qs, "Gender"), "Decline To Self Identify")
            session.fill_answer(_by_label(qs, "privacy policy"), True)
            read = lambda js: page.evaluate(js)  # noqa: E731
            assert read("document.getElementById('q_auth').selectedOptions[0].textContent") == "Yes", engine
            assert read("document.getElementById('q_spons').value") == "No"
            assert read("document.getElementById('q_py').value") == "8"
            assert read("document.getElementById('q_why').value") == "Because."
            assert read("document.getElementById('q_gender').value") == "Decline To Self Identify"
            assert read("document.getElementById('q_priv').checked") is True
            assert read("document.getElementById('q_news').checked") is False

            # extracting again now reports the filled values, so a second pass leaves them alone
            again = parse_questions(session.extract_questions())
            assert _by_label(again, "legally authorized").value == "Yes" and _by_label(again, "privacy policy").value


def test_radios_checkbox_groups_and_dialog_questions_fill_correctly():
    with pages(F.MODAL_LIKE) as opened:
        if not _require_a_browser(opened):
            return
        for engine, session, page in opened:
            qs = parse_questions(session.extract_questions())
            session.fill_answer(_by_label(qs, "remotely"), "No")
            session.fill_answer(_by_label(qs, "English"), "Professional")
            session.fill_answer(_by_label(qs, "Which of these"), ["Python", "Rust"])
            session.fill_answer(_by_label(qs, "Node.js"), "6")
            assert page.evaluate("document.getElementById('r2').checked && !document.getElementById('r1').checked") is True, engine
            assert page.evaluate("document.getElementById('lang').value") == "Professional"
            assert page.evaluate("[...document.querySelectorAll('input[name=tech]')].map(e => e.checked)") == [True, False, True]
            assert page.evaluate("document.getElementById('rm').value") == "6"
            assert page.evaluate("document.getElementById('bg1').value") == ""        # the decoy form was never touched


def test_visually_hidden_styled_radios_and_a_custom_combobox():
    with pages(F.STYLED_CONTROLS) as opened:
        if not _require_a_browser(opened):
            return
        for engine, session, page in opened:
            qs = parse_questions(session.extract_questions())
            age, country = _by_label(qs, "18 years"), _by_label(qs, "Country")
            assert (age.kind, age.options, country.kind) == ("radio", ["Yes", "No"], "combobox"), engine
            session.fill_answer(age, "Yes")
            session.fill_answer(country, "Mexico")
            assert page.evaluate("document.getElementById('a1').checked") is True        # opacity:0 / 0x0 inputs are still clickable
            assert page.evaluate("document.getElementById('cb').dataset.chosen") == "Mexico"   # the listbox option was picked, not just typed


def test_solver_answers_a_real_form_from_the_fact_sheet_and_parks_what_it_cannot():
    profile = {"name": "Test User", "first_name": "Test", "email": "t@example.com", "linkedin": "https://linkedin.com/in/test", "skills": ["Python", "Docker"]}
    facts = {
        "work_authorization": {"authorized_countries": ["MX", "US"], "requires_sponsorship": False},
        "experience": {"total_years": 9, "years_by_skill": {"Python": 8, "Docker": None}},
        "demographics": {"gender": "decline"},
    }
    with pages(F.GREENHOUSE_LIKE) as opened:
        if not _require_a_browser(opened):
            return
        for engine, session, page in opened:
            solver = QuestionSolver(profile, facts, llm_cfg={}, answers_cfg={})
            with patch("answerer.chat_json", side_effect=AssertionError("the model must not be asked: rules cover everything else")):
                report = solver.solve(session, {"title": "Backend Engineer", "company": "Acme"})

            read = lambda js: page.evaluate(js)  # noqa: E731
            assert read("document.getElementById('first_name').value") == "Test", engine
            assert read("document.getElementById('email').value") == "t@example.com"
            assert read("document.getElementById('q_linkedin').value") == "https://linkedin.com/in/test"
            assert read("document.getElementById('q_auth').selectedOptions[0].textContent") == "Yes"
            assert read("document.getElementById('q_spons').value") == "No"
            assert read("document.getElementById('q_py').value") == "8"
            assert read("document.getElementById('q_gender').value") == "Decline To Self Identify"
            assert read("document.getElementById('q_priv').checked") is True          # consent box: accept_consents
            assert read("document.getElementById('q_news').checked") is False         # marketing opt-in is never ticked
            assert read("document.getElementById('q_why').value") == ""               # never invented

            # the one required question nobody can answer is reported, with the reason
            assert report.blocking and [u["label"] for u in report.unanswered] == ["Why do you want to work at Acme?"]
            assert "open_ended" in report.unanswered[0]["reason"]
            assert len(report.answered) >= 7
