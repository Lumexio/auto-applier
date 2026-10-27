"""The LLM client (budgeting, JSON mode, truncation detection), model-based fit scoring, and the daemon's LLM probe."""

from __future__ import annotations

import contextlib
import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import llm_client
from fit import FitScorer, build_fit_gate, condense, quote_in_text
from llm_client import LLMError, chat_json, detect_context_tokens, estimate_tokens, parse_json_object
from stage3_apply.ats_handlers.base import NotAFit
from state import StateStore
from tests.test_p0_safety import tmp_cwd

PROFILE = {"name": "T", "seniority": "senior", "skills": ["Python", "Docker", "PostgreSQL"], "min_salary": 100000, "remote_ok": True,
           "work_authorization": ["MX", "US"], "exclusions": {"keywords": ["unpaid"]}, "location": "Toronto, Canada"}
FACTS = {"experience": {"total_years": 9}, "work_authorization": {"authorized_countries": ["MX", "US"], "requires_sponsorship": False}}

POSTING = (
    "Senior Backend Engineer at Acme. We are building a payments platform and need an engineer to own our APIs. "
    "Day to day you will design services, review code and mentor teammates in a fully remote team across the Americas. "
    "Requirements: 7+ years of Python, strong PostgreSQL skills, experience running Docker in production. "
    "Candidates must be a US citizen with an active TS/SCI clearance. Benefits include health insurance and a learning budget."
)


def http_reply(content, prompt_tokens=300):
    m = MagicMock()
    m.read.return_value = json.dumps({"choices": [{"message": {"content": content}}], "usage": {"prompt_tokens": prompt_tokens}}).encode()
    m.__enter__.return_value = m
    return m


# --------------------------------------------------------------------------- llm_client

def test_chat_json_asks_for_json_mode_and_parses_fenced_or_chatty_replies():
    with patch("urllib.request.urlopen", return_value=http_reply('{"a": 1}')) as urlopen:
        assert chat_json({"model": "m"}, "sys", "usr", max_tokens=50) == {"a": 1}
    sent = json.loads(urlopen.call_args.args[0].data)
    assert sent["response_format"] == {"type": "json_object"} and sent["temperature"] == 0 and sent["max_tokens"] == 50 and sent["model"] == "m"
    with patch("urllib.request.urlopen", return_value=http_reply('```json\n{"a": 2}\n```')):
        assert chat_json({}, "s", "u") == {"a": 2}
    with patch("urllib.request.urlopen", return_value=http_reply('Sure! Here you go: {"a": 3} hope that helps')):
        assert chat_json({}, "s", "u") == {"a": 3}
    assert parse_json_object('{"x": [1]}') == {"x": [1]}
    for junk in ("", "no json here", "[1, 2]", "{broken"):
        try:
            parse_json_object(junk)
            raise AssertionError(f"accepted {junk!r}")
        except LLMError:
            pass


def test_chat_json_retries_bad_json_once_then_gives_up_with_one_error_type():
    with patch("urllib.request.urlopen", side_effect=[http_reply("not json"), http_reply('{"ok": true}')]) as urlopen:
        assert chat_json({}, "s", "u") == {"ok": True} and urlopen.call_count == 2
    with patch("urllib.request.urlopen", side_effect=[http_reply("nope"), http_reply("still nope")]) as urlopen:
        try:
            chat_json({}, "s", "u")
            raise AssertionError("should have raised")
        except LLMError:
            assert urlopen.call_count == 2
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
        try:
            chat_json({}, "s", "u")
            raise AssertionError("should have raised")
        except LLMError as e:
            assert "URLError" in str(e)


def test_a_prompt_that_cannot_fit_never_leaves_the_machine_and_a_clipped_one_is_detected():
    big = "word " * 3000
    with patch("urllib.request.urlopen") as urlopen:
        for cfg in ({}, {"context_tokens": 8192, "effective_context_tokens": 1000}):
            try:
                chat_json(cfg, "s", big if not cfg else "x" * 3000)
                raise AssertionError("should have refused")
            except LLMError as e:
                assert "does not fit" in str(e)
        assert not urlopen.called                                              # refused before any network call
    with patch("urllib.request.urlopen", return_value=http_reply('{"a": 1}', prompt_tokens=2040)):
        try:
            chat_json({"context_tokens": 2048}, "s", "u")
            raise AssertionError("a reply to a clipped prompt must not be trusted")
        except LLMError as e:
            assert "truncated" in str(e)
    with patch("urllib.request.urlopen", return_value=http_reply('{"a": 1}', prompt_tokens=2040)):
        assert chat_json({"context_tokens": 8192}, "s", "u") == {"a": 1}      # 2040 tokens is fine in an 8192 window


def test_the_real_context_window_is_measured_from_how_far_a_long_probe_got():
    with patch("urllib.request.urlopen", return_value=http_reply("ok", prompt_tokens=2048)):
        assert detect_context_tokens({}) == 2048                                # saturated: that is the window
    with patch("urllib.request.urlopen", return_value=http_reply("ok", prompt_tokens=3040)):
        assert detect_context_tokens({}) is None                                # the whole probe went through: at least 3000
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
        assert detect_context_tokens({}) is None
    assert estimate_tokens("x" * 350) == 100 and llm_client.context_budget({"effective_context_tokens": 900, "context_tokens": 4096}) == 900
    assert llm_client.context_budget({}) == 2048


def test_daemon_measures_the_window_at_startup_and_budgets_prompts_to_it():
    from daemon import Daemon

    with tmp_cwd() as tmp:
        cfg = tmp / "config.yaml"
        cfg.write_text(f'daemon:\n  state_db: "{(tmp / "d.db").as_posix()}"\nllm:\n  context_tokens: 4096\n', encoding="utf-8")
        d = Daemon(config_path=str(cfg), profile_path=str(tmp / "p.json"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch("daemon.check_health", return_value=(True, "up")), patch("daemon.detect_context_tokens", return_value=2048):
            assert d.probe_llm() is True
        assert d.config["llm"]["effective_context_tokens"] == 2048             # the smaller of configured and measured
        assert "OLLAMA_CONTEXT_LENGTH" in out.getvalue()                       # and the user is told how to fix it

        with contextlib.redirect_stdout(io.StringIO()), patch("daemon.check_health", return_value=(True, "up")), patch("daemon.detect_context_tokens", return_value=None):
            d.probe_llm()
        assert d.config["llm"]["effective_context_tokens"] == 4096             # window is at least as big as the configured budget

        d.config["llm"].pop("effective_context_tokens")
        with contextlib.redirect_stdout(io.StringIO()), patch("daemon.check_health", return_value=(False, "down")), patch("daemon.detect_context_tokens") as probe:
            assert d.probe_llm() is False
        assert not probe.called and "effective_context_tokens" not in d.config["llm"]    # no point probing a dead server
        d.state.close()


# --------------------------------------------------------------------------- fit scoring

def model_says(fit, deal_breakers=None, **extra):
    return {"fit": fit, "seniority_match": "match", "missing_must_haves": [], "deal_breakers": deal_breakers or [], "summary": "ok", **extra}


def scorer(state=None, **fit_cfg):
    return FitScorer(PROFILE, FACTS, {"model": "m"}, fit_cfg, state)


JOB = {"id": "j1", "title": "Senior Backend Engineer", "company": "Acme"}


def test_fit_applies_when_the_model_likes_the_posting_and_skips_when_it_does_not():
    with patch("fit.chat_json", return_value=model_says(82)):
        good = scorer().score(JOB, POSTING)
    assert (good.verdict, good.score) == ("apply", 0.82)
    with patch("fit.chat_json", return_value=model_says(30, summary="mostly frontend")):
        bad = scorer().score(JOB, POSTING)
    assert bad.verdict == "skip" and bad.score == 0.3 and "mostly frontend" in bad.summary
    with patch("fit.chat_json", return_value=model_says(50)):
        assert scorer().score(JOB, POSTING).verdict == "apply"                  # the threshold itself is not "below" it


def test_a_deal_breaker_counts_only_with_a_quote_that_is_really_in_the_posting():
    real = {"issue": "needs clearance", "quote": "US citizen with an active TS/SCI clearance"}
    invented = {"issue": "needs a PhD", "quote": "a doctorate in physics is required for this role"}
    too_short = {"issue": "x", "quote": "remote"}
    with patch("fit.chat_json", return_value=model_says(90, [real])):
        r = scorer().score(JOB, POSTING)
    assert r.verdict == "skip" and r.deal_breakers[0]["issue"] == "needs clearance" and "deal-breaker" in r.summary    # vetoes despite fit 90
    with patch("fit.chat_json", return_value=model_says(90, [invented, too_short])):
        r = scorer().score(JOB, POSTING)
    assert r.verdict == "apply" and r.deal_breakers == []                                                             # a small model cannot invent a veto
    with patch("fit.chat_json", return_value=model_says(90, [real])):
        assert scorer(deal_breakers_veto=False).score(JOB, POSTING).verdict == "apply"
    assert quote_in_text("Active  TS/SCI clearance!", "candidates must be a us citizen with an active ts sci clearance") is True


def test_missing_text_and_unusable_replies_never_produce_a_verdict():
    with patch("fit.chat_json", side_effect=AssertionError("no model call without posting text")):
        r = scorer().score(JOB, "too short to judge")
    assert (r.verdict, r.error) == ("unknown", "insufficient_text")
    for bad in (model_says("high"), model_says(150), model_says(-5), model_says(True), {"summary": "no fit field"}):
        with patch("fit.chat_json", return_value=bad):
            r = scorer().score(JOB, POSTING)
        assert (r.verdict, r.error) == ("unknown", "bad_reply"), bad


def test_an_unreachable_model_follows_the_configured_policy():
    gate_text = {"id": "j", "title": "t", "company": "c", "description": POSTING}
    with patch("fit.chat_json", side_effect=LLMError("down")):
        s = scorer()
        assert s.score(JOB, POSTING).error == "llm_error"
        build_fit_gate(s)(object(), gate_text)                                   # default "allow": carry on with the keyword gate's verdict
        strict = scorer(on_llm_error="skip")
        try:
            build_fit_gate(strict)(object(), gate_text)
            raise AssertionError("on_llm_error: skip must veto")
        except NotAFit as e:
            assert "on_llm_error" in e.summary


def test_assessments_are_cached_and_the_verdict_follows_the_current_threshold():
    with tmp_cwd() as tmp:
        store = StateStore(tmp / "s.db")
        with patch("fit.chat_json", return_value=model_says(70)) as model:
            first = scorer(store).score(JOB, POSTING)
            again = scorer(store).score(JOB, POSTING)
            assert (first.cached, again.cached, model.call_count) == (False, True, 1)
            stricter = scorer(store, min_score=0.9).score(JOB, POSTING)
            assert (stricter.verdict, stricter.cached, model.call_count) == ("skip", True, 1)       # re-decided without a new model call
            other_profile = FitScorer({**PROFILE, "skills": ["Go"]}, FACTS, {"model": "m"}, {}, store)
            assert other_profile.score(JOB, POSTING).cached is False and model.call_count == 2    # a different candidate is a different question
            assert FitScorer(PROFILE, FACTS, {"model": "other"}, {}, store).score(JOB, POSTING).cached is False
        store.close()


def test_the_fit_prompt_fits_the_window_and_keeps_the_requirements_that_sit_deep_in_a_long_posting():
    filler = "We are a fast growing company with a great culture and many perks. " * 400            # ~27k chars
    long_posting = "Senior Backend Engineer\n" + filler + "\nRequirements: 7+ years of Python and PostgreSQL. Must hold an active clearance.\n" + filler
    seen = {}

    def capture(cfg, system, user, **kw):
        seen.update(system=system, user=user, max_tokens=kw["max_tokens"])
        return model_says(70)

    with patch("fit.chat_json", side_effect=capture):
        scorer().score(JOB, long_posting)
    assert estimate_tokens(seen["system"]) + estimate_tokens(seen["user"]) + seen["max_tokens"] <= 2048
    assert "Requirements: 7+ years of Python" in seen["user"] and "senior" in seen["user"] and "Python, Docker" in seen["user"]
    assert "[...]" in seen["user"]                                                                   # visibly condensed, not silently cut

    with patch("fit.chat_json", side_effect=AssertionError("window too small to attempt")):
        tiny = FitScorer(PROFILE, FACTS, {"effective_context_tokens": 700}, {}, None).score(JOB, POSTING)
    assert (tiny.verdict, tiny.error) == ("unknown", "context_too_small")
    assert condense("short", 100) == "short" and condense("a" * 300 + " Requirements: x " + "b" * 400, 200).count("[...]") == 1 and "Requirements: x" in condense("a" * 300 + " Requirements: x " + "b" * 400, 200)


class PostingPage:
    def __init__(self, text):
        self.text = text

    def page_text(self, max_chars=9000):
        return self.text[:max_chars]


def test_the_gate_reads_card_only_jobs_from_the_page_and_leaves_an_audit_file():
    card_only = {"id": "li9", "title": "Backend Engineer", "company": "Acme"}              # a LinkedIn card: no description
    with tmp_cwd() as tmp:
        gate = build_fit_gate(scorer(), lambda job: tmp / "apps" / job["id"])
        with patch("fit.chat_json", return_value=model_says(85)) as model:
            gate(PostingPage(POSTING), card_only)
        assert model.called and json.loads((tmp / "apps" / "li9" / "fit.json").read_text(encoding="utf-8"))["verdict"] == "apply"

        with patch("fit.chat_json", return_value=model_says(15, summary="wrong stack")):
            try:
                gate(PostingPage(POSTING), {**card_only, "id": "li10"})
                raise AssertionError("expected NotAFit")
            except NotAFit as e:
                assert "15%" in e.summary and "wrong stack" in e.summary and e.details["verdict"] == "skip"
        assert json.loads((tmp / "apps" / "li10" / "fit.json").read_text(encoding="utf-8"))["verdict"] == "skip"

        with patch("fit.chat_json", side_effect=AssertionError("nothing to judge, so no call")):
            gate(object(), card_only)                                                        # no page_text and no description: carry on


def test_a_verbatim_quote_that_states_no_requirement_cannot_veto_a_job():
    posting = POSTING + " Remote within the US time zones. Our team meets for a daily stand-up."
    irrelevant = {"issue": "residence", "quote": "Remote within the US time zones."}               # really in the posting, but says nothing is required
    stand_up = {"issue": "meetings", "quote": "Our team meets for a daily stand-up."}
    with patch("fit.chat_json", return_value=model_says(65, [irrelevant, stand_up])):
        r = scorer().score(JOB, posting)
    assert r.verdict == "apply" and r.deal_breakers == []                                           # seen with the real model on a React posting
    real = {"issue": "needs clearance", "quote": "Candidates must be a US citizen with an active TS/SCI clearance."}
    with patch("fit.chat_json", return_value=model_says(65, [irrelevant, real])):
        r = scorer().score(JOB, posting)
    assert r.verdict == "skip" and [d["issue"] for d in r.deal_breakers] == ["needs clearance"]


def test_the_startup_probe_is_larger_than_the_budget_it_verifies():
    from daemon import Daemon

    with tmp_cwd() as tmp:
        cfg = tmp / "config.yaml"
        cfg.write_text(f'daemon:\n  state_db: "{(tmp / "d.db").as_posix()}"\nllm:\n  context_tokens: 4096\n', encoding="utf-8")
        d = Daemon(config_path=str(cfg), profile_path=str(tmp / "p.json"))
        with contextlib.redirect_stdout(io.StringIO()), patch("daemon.check_health", return_value=(True, "up")), patch("daemon.detect_context_tokens", return_value=None) as probe:
            d.probe_llm()
        assert probe.call_args.kwargs["probe_tokens"] == 6144                                       # 1.5x the configured budget: a 2048 server is exposed
        d.state.close()
