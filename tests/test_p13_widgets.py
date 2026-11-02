"""The page furniture and widgets that used to park or fail applications for no good reason.

* "Slide 2 of 5" (a carousel's position marker) was recorded as a required question nobody could answer.
* A dropdown that builds its list when opened had no options, so "Professional" or a country could not be matched, and filling one
  that could not be typed into waited 30 s and failed with a TimeoutError.
* A required upload slot called "Upload File (PDF, Word or Text)" parked the job although a résumé was configured.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from agent import AgentStop, ApplyAgent, file_to_attach
from answerer import COMBOBOXES_OPENED_MAX, WHOLE_LIST_MAX, QuestionSolver, deliver, read_combobox_options
from form_questions import Question, is_position_marker, parse_questions
from page_state import FileField, build_snapshot
from tests import form_fixtures as F
from tests.test_p2_forms import _by_label, _require_a_browser, pages
from tests.test_p3_agent import expect, raw_frame


# =========================================================================== "Slide 2 of 5" is not a question

def test_a_carousels_position_marker_is_not_a_question_and_real_questions_are_untouched():
    for label in ("Slide 2 of 5", "slide 12 of 12", "  Step 1 of 3 ", "Page 3 / 4", "Slide 2 of 5 *"):
        assert is_position_marker(label), label
    for label in ("How many slides of 5 did you review?", "Slide deck experience", "Step 1 of 3 completed?", "Page", "Years of 5", ""):
        assert not is_position_marker(label), label
    raw = [{"id": 1, "kind": "text", "label": "Slide 2 of 5", "required": True}, {"id": 2, "kind": "text", "label": "Email", "required": True}]
    assert [q.label for q in parse_questions(raw)] == ["Email"]


# =========================================================================== choosing from a dropdown's choices

def combo(options, **kw):
    return Question(1, "combobox", kw.pop("label", "English Level"), options=list(options), required=True, **kw)


def test_an_answer_is_matched_to_the_dropdowns_own_choice_and_said_the_way_the_dropdown_says_it():
    q = combo(["Native or bilingual", "Professional", "Conversational"])
    got = deliver(q, "professional", "bank", "bank:english level")
    assert got.value == "Professional"                                           # the page's spelling, so picking it by text finds it
    assert deliver(combo(["Yes, I would need sponsorship", "No"], label="Sponsorship"), True, "fact", "x").value == "Yes, I would need sponsorship"


def test_a_short_list_is_the_whole_list_so_an_answer_outside_it_is_refused_with_the_choices_named():
    blocked = deliver(combo(["Canada", "Mexico", "Spain"], label="Country"), "Atlantis", "bank", "x")
    assert "none of the options (Canada, Mexico, Spain)" in blocked.reason


def test_a_long_list_is_taken_for_a_searchable_dropdowns_first_screenful_and_the_answer_is_typed():
    long = [f"Country {i}" for i in range(WHOLE_LIST_MAX + 1)]
    got = deliver(combo(long, label="Country"), "Atlantis", "bank", "x")
    assert got.value == "Atlantis"                                               # the page's own search will find it, or the fill will say it did not
    assert deliver(combo([], label="City"), "Madrid", "bank", "x").value == "Madrid"       # nothing was listed: typed, as before


def test_only_required_empty_dropdowns_are_opened_to_read_their_choices_and_one_that_will_not_open_is_typed_into():
    opened: list[int] = []

    class Session:
        def combobox_options(self, q):
            opened.append(q.id)
            if q.id == 5:
                raise RuntimeError("would not open")
            return ["A", " B ", ""]

    qs = [Question(1, "combobox", "Needs", required=True), Question(2, "combobox", "Optional", required=False), Question(3, "combobox", "Filled", required=True, value="x"),
          Question(4, "text", "Name", required=True), Question(5, "combobox", "Broken", required=True), Question(6, "select", "Pick", options=["a"], required=True),
          Question(7, "combobox", "Listed", options=["x", "y"], required=True)]
    read_combobox_options(Session(), qs)
    assert opened == [1, 5] and qs[0].options == ["A", "B"] and qs[4].options == []
    assert qs[6].options == ["x", "y"], "a dropdown whose choices are already known is not opened again"
    read_combobox_options(object(), qs)                                             # a session that cannot open dropdowns: nothing happens

    opened.clear()
    many = [Question(i, "combobox", f"Q{i}", required=True) for i in range(1, COMBOBOXES_OPENED_MAX + 4)]
    read_combobox_options(Session(), many)
    assert len(opened) == COMBOBOXES_OPENED_MAX, "a page full of dropdowns must not be opened forever"


# =========================================================================== real browsers: dropdowns that exist only while open

def test_a_dropdown_is_opened_read_and_left_as_it_was():
    with pages(F.COMBOBOX_LISTS) as opened:
        if not _require_a_browser(opened):
            return
        for engine, session, page in opened:
            qs = parse_questions(session.extract_questions())
            country, level, city = _by_label(qs, "country"), _by_label(qs, "English"), _by_label(qs, "city")
            assert [q.kind for q in (country, level, city)] == ["combobox"] * 3, engine
            assert session.combobox_options(country) == ["Canada", "Mexico", "United States", "Spain"], engine
            assert session.combobox_options(level) == ["Native or bilingual", "Professional", "Conversational"], engine
            assert session.combobox_options(city) == [], engine                         # a search lists nothing until you type
            assert page.evaluate("[...document.querySelectorAll('input')].map(e => e.value + '|' + (e.dataset.chosen || ''))") == ["|", "|", "|"], engine
            assert page.evaluate("document.querySelectorAll('[role=option]').length") == 0, "the list was closed again"


def test_a_choice_is_picked_by_its_text_even_in_a_dropdown_that_cannot_be_typed_into():
    with pages(F.COMBOBOX_LISTS) as opened:
        if not _require_a_browser(opened):
            return
        for engine, session, page in opened:
            qs = parse_questions(session.extract_questions())
            level, country, city = _by_label(qs, "English"), _by_label(qs, "country"), _by_label(qs, "city")
            session.fill_answer(level, "Professional")                                  # read-only: typing would time out; the list is clicked
            session.fill_answer(country, "United States")                               # searchable: the exact choice among those it shows
            session.fill_answer(city, "Monterrey")                                      # nothing until typed: type, then pick
            chosen = page.evaluate("['c1', 'c2', 'c3'].map(id => document.getElementById(id).dataset.chosen)")
            assert chosen == ["United States", "Professional", "Monterrey"], (engine, chosen)


def test_an_answer_no_choice_reads_is_never_replaced_by_whichever_choice_is_highlighted():
    with pages(F.COMBOBOX_LISTS) as opened:
        if not _require_a_browser(opened):
            return
        for engine, session, page in opened:
            qs = parse_questions(session.extract_questions())
            level, city = _by_label(qs, "English"), _by_label(qs, "city")
            error = expect(RuntimeError, lambda: session.fill_answer(level, "Fluent"))
            assert "none of the choices reads 'Fluent'" in str(error), engine
            assert page.evaluate("document.getElementById('c2').dataset.chosen || ''") == "", engine
            error = expect(RuntimeError, lambda: session.fill_answer(city, "Mon"))     # two cities contain it: not a guess between them
            assert "none of the choices reads 'Mon'" in str(error), engine
            assert page.evaluate("document.getElementById('c3').dataset.chosen || ''") == "", engine


def test_the_solver_reads_each_dropdowns_choices_picks_from_them_and_hands_a_refused_answer_to_the_person_with_the_choices():
    answers = {"in which country do you currently work": "Atlantis", "english level": "professional", "search for your city": "Monterrey"}
    with pages(F.COMBOBOX_LISTS) as opened:
        if not _require_a_browser(opened):
            return
        for engine, session, page in opened:
            solver = QuestionSolver({"name": "T"}, {}, llm_cfg={}, answers_cfg={"open_ended": "review"}, bank_get=answers.get)
            report = solver.solve(session, {"title": "Backend Engineer", "company": "Acme"})
            chosen = page.evaluate("['c1', 'c2', 'c3'].map(id => document.getElementById(id).dataset.chosen || '')")
            assert chosen == ["", "Professional", "Monterrey"], (engine, chosen, report.unanswered)       # a list that opens is read; a search is typed into
            (left,) = report.unanswered
            assert left["label"] == "In which country do you currently work?" and left["kind"] == "combobox", engine
            assert left["options"] == ["Canada", "Mexico", "United States", "Spain"] and "none of the options" in left["reason"], engine
            assert page.evaluate("document.getElementById('c1').value") == "", "nothing was typed into the refused one"


# =========================================================================== a generic upload slot

def slot(label="", name="", required=True):
    return FileField(id=1, label=label, name=name, required=required)


ATTACHED = {"resume_path": "/x/resume.pdf", "cover_letter_path": "/x/cover.pdf"}


def test_a_generic_upload_slot_takes_the_resume_unless_the_form_has_its_own_or_the_slot_names_another_document():
    generic = slot("Upload File (PDF, Word or Text)")
    assert file_to_attach(generic, [generic], ATTACHED) == "/x/resume.pdf"
    assert file_to_attach(slot("Resume/CV"), [], ATTACHED) == "/x/resume.pdf"
    assert file_to_attach(slot("Cover letter"), [], ATTACHED) == "/x/cover.pdf"
    assert file_to_attach(generic, [generic, slot("Your résumé")], ATTACHED) is None                 # the résumé has its own slot
    for other in ("Upload your transcript", "Portfolio (PDF)", "Copy of passport", "Writing sample", "Degree certificate", "Work authorization document"):
        assert file_to_attach(slot(other), [], ATTACHED) is None, other
    assert file_to_attach(slot("Upload your transcript"), [], {**ATTACHED, "default_path": "/x/any.pdf"}) == "/x/any.pdf"   # you said what a generic slot gets
    assert file_to_attach(generic, [generic], {}) is None                                                # nothing configured: nothing to send
    optional = slot("Additional documents (optional)", required=False)
    assert file_to_attach(optional, [optional], {**ATTACHED, "default_path": "/x/any.pdf"}) is None      # an optional generic slot is left empty


class Uploads:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def current_url(self):
        return "https://careers.acme.com/jobs/1"

    def upload_file(self, field, path):
        self.sent.append((field.label, path))


def run_uploads(files, attachments):
    snap = build_snapshot([raw_frame(files=files)], [])
    session = Uploads()
    agent = ApplyAgent(session, {"attachments": attachments}, {"title": "Backend Engineer", "company": "Acme"}, solve=lambda: None, cfg={"consensus": 2})
    agent._upload_files(snap)
    return session.sent, agent.history


def test_the_agent_attaches_the_resume_to_a_generic_required_slot_and_parks_one_it_cannot_decide():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        resume = Path(d) / "resume.pdf"
        resume.write_bytes(b"%PDF-1.4")
        sent, history = run_uploads([dict(id=1, label="Upload File (PDF, Word or Text)", required=True, has_file=False)], {"resume_path": str(resume)})
        assert sent == [("Upload File (PDF, Word or Text)", str(resume))] and history == ["attached a file to 'Upload File (PDF, Word or Text)'"]

        e = expect(AgentStop, lambda: run_uploads([dict(id=1, label="Upload your transcript", required=True, has_file=False)], {"resume_path": str(resume)}))
        assert e.kind == "needs_input" and e.gaps == ["upload: Upload your transcript"]

        sent, _ = run_uploads([dict(id=1, label="Anything else (optional)", required=False, has_file=False)], {"resume_path": str(resume)})
        assert sent == [], "an optional generic slot is left empty, and never a reason to park the job"
