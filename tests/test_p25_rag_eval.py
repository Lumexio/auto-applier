"""python rag_eval.py: a test set built from your own answers, and how keyword and hybrid search score on it."""

from __future__ import annotations

import io
import shutil
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import evidence
import rag_eval
import retrieval
from evidence import NoEvidence, Passage
from llm_client import LLMError
from state import StateStore
from tests.test_p23_retrieval import PASSAGES, Embedder, Hybrid

DOCKER, MENTOR, BUILT, LANG = 0, 1, 2, 3          # indexes into PASSAGES


def test_the_quotes_of_a_drafts_basis_are_found_and_short_scraps_are_ignored():
    basis = 'resumes/resume.md: "Mentored four junior engineers" | "pairing" | "Built an order tracking service"'
    assert rag_eval.quotes_in(basis) == ["Mentored four junior engineers", "Built an order tracking service"]
    assert rag_eval.quotes_in("") == [] and rag_eval.quotes_in(None) == []
    assert rag_eval.gold_from_quotes(["mentored FOUR junior engineers"], PASSAGES) == {MENTOR}
    assert rag_eval.gold_from_quotes(["nothing like this anywhere"], PASSAGES) == frozenset()
    assert rag_eval.gold_from_quotes([], PASSAGES) == frozenset()


def test_the_line_an_answer_came_from_is_the_one_holding_most_of_its_words_or_none_at_all():
    answer = "I mentored four junior engineers through code review and pairing at Northwind."
    assert rag_eval.gold_from_answer(answer, PASSAGES) == {MENTOR}
    assert rag_eval.gold_from_answer("Yes, five years", PASSAGES) == frozenset()                       # too short to say anything
    assert rag_eval.gold_from_answer("I enjoy hiking and painting landscapes on weekends in autumn", PASSAGES) == frozenset()   # no line holds enough of it
    wordy = "I mentored four junior engineers while also running weekly workshops, hiring panels, onboarding programmes, budgeting reviews, roadmap sessions"
    assert rag_eval.gold_from_answer(wordy, PASSAGES) == frozenset()                                   # four words shared, but of a long answer: too little of it
    six = "mentored four people always eagerly, daily"
    assert len(set(evidence.tokens(six))) >= rag_eval.MIN_ANSWER_TOKENS and rag_eval.gold_from_answer(six, PASSAGES) == frozenset()   # a high share, but only 2 words in common
    twins = PASSAGES + [Passage("notes.md", "", "Mentored four junior engineers through code review and pairing sessions")]
    assert rag_eval.gold_from_answer(answer, twins) == {MENTOR, 4}                                      # two lines hold it equally well: both are right


def test_cases_come_from_waiting_drafts_and_saved_answers_once_each_and_never_from_facts():
    pending = [
        {"question_text": "Have you coached other developers?", "proposal_basis": 'resume.md: "Mentored four junior engineers"'},
        {"question_text": "Question whose quote is gone", "proposal_basis": 'resume.md: "a line you deleted from the resume"'},
        {"question_text": "No draft yet", "proposal_basis": None},
    ]
    saved = [
        {"question_text": "Have you coached other developers?", "answer": "I mentored four junior engineers through code review and pairing", "source": "user"},
        {"question_text": "Describe a service you built", "answer": "I built an order tracking service in Python and PostgreSQL at Northwind", "source": "llm"},
        {"question_text": "Years with Python", "answer": "I built an order tracking service in Python and PostgreSQL at Northwind", "source": "fact"},
        {"question_text": "Pronouns", "answer": "they", "source": "user"},
    ]
    cases = rag_eval.build_cases(pending, saved, PASSAGES)
    assert [(c.question, set(c.gold), c.origin) for c in cases] == [
        ("Have you coached other developers?", {MENTOR}, "pending"), ("Describe a service you built", {BUILT}, "saved"),
    ]


def cases_for_search():
    return [rag_eval.Case("Have you coached other developers?", frozenset({MENTOR}), "pending"),
            rag_eval.Case("Describe your Docker containers deployment work", frozenset({DOCKER}), "saved"),
            rag_eval.Case("What is your favourite colour?", frozenset({LANG}), "saved")]


def test_hybrid_is_scored_against_keyword_on_the_same_questions_and_the_difference_is_reported():
    with Hybrid():
        keyword = rag_eval.evaluate(cases_for_search(), PASSAGES, "keyword", 3)
        hybrid = rag_eval.evaluate(cases_for_search(), PASSAGES, "hybrid", 3)
        assert retrieval.mode() == "hybrid"                                                           # the setting is put back
        rag_eval.evaluate(cases_for_search(), PASSAGES, "keyword", 3)
        assert retrieval.mode() == "hybrid"                                                           # also after a keyword run
        assert (keyword.cases, keyword.hit1, keyword.hitk, keyword.empty) == (3, 1, 1, 2) and keyword.ranks == [None, 1, None]
        assert (hybrid.cases, hybrid.hit1, hybrid.hit3, hybrid.hitk, hybrid.empty) == (3, 2, 2, 2, 1) and hybrid.ranks == [1, 1, None]
        assert abs(hybrid.mrr - 2.0) < 1e-9 and abs(keyword.mrr - 1.0) < 1e-9
        assert hybrid.misses == ["What is your favourite colour?"]
        text = rag_eval.report([keyword, hybrid], 3)
    assert "keyword" in text and "hybrid" in text and "33%" in text and "67%" in text
    assert "hybrid found the right line for 1 question(s) keyword search missed, and lost 0." in text
    empty = rag_eval.report([rag_eval.Score("keyword")], 3)
    assert empty.count("\n") == 1 and empty.splitlines()[1].count(" -") >= 4                          # no cases: dashes, no division by zero


def test_a_right_line_beyond_k_counts_as_a_miss_but_its_rank_is_still_recorded():
    crowd = [Passage("r.md", "", f"Mentored engineers on testing practices batch {i}") for i in range(5)]
    weak = Passage("r.md", "", "Mentored people")                                                      # matches one word of the question: ranks after the five
    case = rag_eval.Case("Mentored engineers testing", frozenset({5}), "saved")
    retrieval.reset()
    shallow = rag_eval.evaluate([case], crowd + [weak], "keyword", 2)
    deep = rag_eval.evaluate([case], crowd + [weak], "keyword", 9)
    assert shallow.ranks == [6] and shallow.hitk == 0 and shallow.misses == [case.question] and shallow.mrr == 0     # beyond k: a miss
    assert deep.ranks == [6] and deep.hitk == 1 and deep.misses == [] and abs(deep.mrr - 1 / 6) < 1e-9


def test_the_first_three_and_top_k_columns_count_different_ranks():
    strong = [Passage("r.md", "", f"Mentored engineers testing batch {i}") for i in range(3)]
    gold = Passage("r.md", "", "Mentored people")
    retrieval.reset()
    fourth = rag_eval.evaluate([rag_eval.Case("Mentored engineers testing", frozenset({3}), "saved")], strong + [gold], "keyword", 6)
    assert fourth.ranks == [4] and (fourth.hit1, fourth.hit3, fourth.hitk) == (0, 0, 1)
    third = rag_eval.evaluate([rag_eval.Case("Mentored engineers testing", frozenset({2}), "saved")], strong + [gold], "keyword", 6)
    assert third.ranks == [3] and (third.hit1, third.hit3, third.hitk) == (0, 1, 1)
    second = rag_eval.evaluate([rag_eval.Case("Mentored engineers testing", frozenset({1}), "saved")], strong + [gold], "keyword", 6)
    assert second.ranks == [2] and (second.hit1, second.hit3, second.hitk) == (0, 1, 1)


def project(root: Path, resume=True, answers=True, pending=False) -> Path:
    (root / "me" / "resumes").mkdir(parents=True)
    if resume:
        (root / "me" / "resumes" / "resume.md").write_text(
            "## Experience\n### Northwind, Senior Engineer (Mar 2019 - Present)\n- Mentored four junior engineers through code review and pairing\n"
            "- Built an order tracking service in Python and PostgreSQL handling forty thousand shipments daily\n", encoding="utf-8")
    (root / "config.yaml").write_text(
        f"paths:\n  source_dir: \"{(root / 'me').as_posix()}\"\n  scratch_dir: \"{(root / 's').as_posix()}\"\ndaemon:\n  state_db: \"{(root / 'state.db').as_posix()}\"\n"
        "retrieval:\n  mode: hybrid\n", encoding="utf-8")
    store = StateStore(str(root / "state.db"))
    if answers:
        store.put_answer("k1", "Tell us about a service you built", "I built an order tracking service in Python and PostgreSQL that handles shipments", "llm")
    if pending:
        store.add_pending_questions("j1", [{"label": "Have you coached other developers?", "reason": "x",
                                            "proposal": {"text": "t", "basis": 'me/resumes/resume.md: "Mentored four junior engineers"', "verified": True}}])
    store.conn.close()
    return root / "config.yaml"


def run_cli(*args):
    out = io.StringIO()
    with redirect_stdout(out):
        code = rag_eval.main(list(args))
    return code, out.getvalue()


def test_the_command_compares_both_searches_on_your_history_and_says_when_the_model_is_away():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        config = project(Path(d), pending=True)
        retrieval.embed_fn = Embedder()
        code, text = run_cli("--config", str(config), "--k", "3", "--misses")
        assert code == 0 and "2 question(s) from your own history" in text and "keyword" in text and "hybrid" in text and "Not found by" in text
        retrieval.reset()
        shutil.rmtree(Path(d) / "s", ignore_errors=True)                                              # no index on disk: the model is needed
        retrieval.embed_fn = Embedder(fail=True)
        code, text = run_cli("--config", str(config))
        assert code == 0 and "not answering" in text and "hybrid  " not in text
    retrieval.reset()


def test_the_command_explains_an_empty_history_and_missing_sources():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        code, text = run_cli("--config", str(project(Path(d), answers=False)))
        assert code == 0 and "Nothing to measure yet" in text
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        code, text = run_cli("--config", str(project(Path(d), resume=False)))
        assert code == 1 and "No resumes or notes found" in text
    retrieval.reset()


def test_the_draft_check_reports_what_the_full_pipeline_did_with_each_question():
    passages = PASSAGES
    cases = [rag_eval.Case("Q one", frozenset({MENTOR}), "saved"), rag_eval.Case("Q two", frozenset({MENTOR}), "saved"),
             rag_eval.Case("Q three", frozenset({MENTOR}), "saved"), rag_eval.Case("Q four", frozenset({MENTOR}), "saved"),
             rag_eval.Case("Q five", frozenset({MENTOR}), "saved")]
    results = iter([
        evidence.Drafted("t", ["Mentored four junior engineers"], ["resume.md"], True, "checked"),
        evidence.Drafted("t", ["Built an order tracking service"], ["resume.md"], False, "no"),
        NoEvidence("the sources are silent about this"), LLMError("down"),
    ])

    def fake(*a, **kw):
        r = next(results)
        if isinstance(r, Exception):
            raise r
        return r

    with patch("evidence.draft_answer", fake):
        lines = rag_eval.draft_check(cases, passages, {"answers": {"query_rewrites": 0}}, limit=4)
    assert len(lines) == 4
    assert lines[0].startswith("verified") and "cites the right line" in lines[0] and "Q one" in lines[0]
    assert lines[1].startswith("unverified") and "cites another line" in lines[1]
    assert lines[2].startswith("no answer") and "the sources are silent" in lines[2]
    assert lines[3].startswith("model down")


def test_the_command_is_documented():
    root = Path(__file__).resolve().parent.parent
    assert "python rag_eval.py" in (root / "SETUP.md").read_text(encoding="utf-8")
