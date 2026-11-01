"""Several local models: named roles, which role does which job, the fallback to the plain `llm` block, and the startup probe.

Everything here is optional: without a `models:` block every component behaves exactly as before.
"""

from __future__ import annotations

import contextlib
import io
from unittest.mock import patch

import llm_client
from agent import AgentStop, ApplyAgent
from answerer import AnswerContext, QuestionSolver, _ask_model, _generate_open_ended
from fit import FitScorer
from form_questions import Question
from llm_client import LLMError, build_llm_cfg, chat_json, context_budget, for_task, probe_roles, role_names
from stage3_apply.run import stage3_run
from state import StateStore
from tests.test_p0_safety import PROFILE as RUN_PROFILE, cfg_for, fake_playwright, save_job, tmp_cwd
from tests.test_p2_fit import FACTS, JOB, POSTING, PROFILE, model_says
from tests.test_p2_flow import DynPage
from tests.test_p3_agent import ScriptedModel, StubSession, snap_with

ENDPOINT = "http://127.0.0.1:11434/v1"
BASE = {"endpoint": ENDPOINT, "model": "base", "context_tokens": 2048, "effective_context_tokens": 2048, "timeout_seconds": 60, "temperature": 0.1}


def config(roles=None, tasks=None) -> dict:
    cfg = {"llm": dict(BASE)}
    if roles is not None:
        cfg["models"] = {"roles": roles, "tasks": tasks or {}}
    return cfg


def plain(cfg: dict) -> dict:
    """A resolved config without the circuit-breaker setting that build_llm_cfg adds to it."""
    return {k: v for k, v in cfg.items() if k != "breaker"}


def ok_reply(model_seen: list[str], **by_model):
    """A stand-in for llm_client._post_chat: records the model asked and fails for the models named in ``by_model``."""

    def post(cfg, payload, timeout):
        model_seen.append(payload["model"])
        if by_model.get(payload["model"]) == "down":
            raise LLMError(f"{payload['model']} is down")
        return {"choices": [{"message": {"content": '{"ok": true}'}}], "usage": {"prompt_tokens": 20}}

    return post


# --------------------------------------------------------------------------- resolving a task to a model

def test_without_a_models_block_every_task_uses_the_llm_block_unchanged():
    assert plain(build_llm_cfg(config())) == BASE and plain(build_llm_cfg({})) == {}
    assert build_llm_cfg(config())["breaker"] == llm_client.DEFAULT_BREAKER              # the one thing added: the circuit breaker's defaults
    for cfg in (config(), config(roles={"pilot": {"model": "coder"}}), {}):                   # no block / roles but no tasks / nothing at all
        llm = build_llm_cfg(cfg)
        for task in ("fit", "questions", "essay", "agent"):
            assert plain(for_task(llm, task)) == (cfg.get("llm") or {})
            assert plain(for_task(llm, task, vote=3)) == (cfg.get("llm") or {})
    assert role_names(build_llm_cfg(config()), "fit") == []


def test_a_role_is_laid_over_the_llm_block_for_the_tasks_it_serves():
    cfg = config({"pilot": {"model": "coder", "context_tokens": 4096, "timeout_seconds": 30, "available": True, "label": "reads pages"}}, {"questions": "pilot"})
    llm = build_llm_cfg(cfg)
    q = for_task(llm, "questions")
    assert "available" not in q and "label" not in q                                      # bookkeeping on a role is not a request setting
    assert (q["model"], q["endpoint"], q["context_tokens"], q["timeout_seconds"], q["temperature"]) == ("coder", ENDPOINT, 4096, 30, 0.1)
    assert q["role"] == "pilot"
    assert "effective_context_tokens" not in q and context_budget(q) == 4096              # the base model's measured window says nothing about this one
    assert q["fallback"]["model"] == "base" and q["fallback"]["effective_context_tokens"] == 2048 and "fallback" not in q["fallback"]
    other = for_task(llm, "fit")                                                         # a task nobody claimed
    assert plain(other) == BASE and context_budget(other) == 2048

    cfg["models"]["roles"]["pilot"]["effective_context_tokens"] = 3000                   # measured for that role at startup
    assert context_budget(for_task(build_llm_cfg(cfg), "questions")) == 3000

    same = for_task(build_llm_cfg(config({"judge": {"model": "base"}}, {"fit": "judge"})), "fit")
    assert same["effective_context_tokens"] == 2048 and "fallback" not in same            # same model: nothing to fall back from
    assert build_llm_cfg(config())["model"] == "base" and config()["llm"] == BASE         # building never mutates the config


def test_unknown_or_unavailable_roles_fall_back_to_the_llm_block():
    llm = build_llm_cfg(config({"heavy": {"model": "big", "available": False}}, {"fit": "ghost", "essay": "heavy"}))
    assert plain(for_task(llm, "fit")) == BASE and plain(for_task(llm, "essay")) == BASE
    assert plain(for_task(build_llm_cfg(config({"heavy": "not a mapping"}, {"essay": "heavy"})), "essay")) == BASE


def test_several_roles_for_one_task_take_turns():
    llm = build_llm_cfg(config({"quick": {"model": "small"}, "pilot": {"model": "coder"}}, {"agent": ["quick", "pilot"], "fit": "pilot"}))
    assert role_names(llm, "agent") == ["quick", "pilot"] and role_names(llm, "fit") == ["pilot"]
    assert [for_task(llm, "agent", vote=k)["model"] for k in range(4)] == ["small", "coder", "small", "coder"]
    assert for_task(llm, "fit", vote=5)["model"] == "coder"

    llm = build_llm_cfg(config({"quick": {"model": "small", "available": False}, "pilot": {"model": "coder"}}, {"agent": ["quick", "pilot"]}))
    assert [for_task(llm, "agent", vote=k)["model"] for k in range(2)] == ["base", "coder"]   # a dead voter is replaced, not skipped


# --------------------------------------------------------------------------- asking the model

def test_chat_json_sends_the_roles_model_and_asks_the_base_model_when_the_role_fails():
    llm_client.reset_breakers()
    heavy = for_task(build_llm_cfg(config({"heavy": {"model": "big"}}, {"essay": "heavy"})), "essay")
    seen: list[str] = []
    with patch.object(llm_client, "_post_chat", ok_reply(seen)):
        assert chat_json(heavy, "s", "u") == {"ok": True} and seen == ["big"]

    seen.clear()
    with patch.object(llm_client, "_post_chat", ok_reply(seen, big="down")):
        assert chat_json(heavy, "s", "u") == {"ok": True}
    assert seen == ["big", "base"]                                                       # the role failed, the plain model answered

    seen.clear()
    with patch.object(llm_client, "_post_chat", ok_reply(seen, big="down", base="down")):
        try:
            chat_json(heavy, "s", "u")
            raise AssertionError("both models are down: that must be an error")
        except LLMError as e:
            assert "big is down" in str(e)                                                 # the role's error is the one reported
    assert seen == ["big", "base"]

    seen.clear()
    with patch.object(llm_client, "_post_chat", ok_reply(seen, base="down")):             # no role in play: one attempt, as before
        try:
            chat_json(dict(BASE), "s", "u")
            raise AssertionError
        except LLMError:
            pass
    assert seen == ["base"]


def test_a_prompt_too_big_for_the_roles_window_is_not_a_reason_to_skip_the_base_model():
    small = for_task(build_llm_cfg(config({"tiny": {"model": "t", "context_tokens": 300}}, {"fit": "tiny"})), "fit")
    seen: list[str] = []
    with patch.object(llm_client, "_post_chat", ok_reply(seen)):
        assert chat_json(small, "s", "word " * 400, max_tokens=50) == {"ok": True}       # ~600 tokens: too big for 300, fine for the base's 2048
    assert seen == ["base"]


# --------------------------------------------------------------------------- the startup probe

def test_probe_roles_marks_missing_models_and_leaves_unused_roles_alone():
    cfg = config(
        {"judge": {"model": "base"}, "heavy": {"model": "big"}, "spare": {"model": "never-used"}},
        {"fit": "judge", "essay": "heavy"},
    )
    with patch.object(llm_client, "list_models", return_value=["base:latest", "other"]) as listed:
        lines = probe_roles(cfg)
    roles = cfg["models"]["roles"]
    assert roles["judge"]["available"] is True and roles["heavy"]["available"] is False and "available" not in roles["spare"]
    text = "\n".join(lines)
    assert "judge" in text and "heavy" in text and "big" in text and "spare" not in text
    assert "falling back" in text and listed.call_count == 2                              # one look per used role, none for the spare

    with patch.object(llm_client, "list_models", return_value=["base", "big:latest"]):    # the model was pulled meanwhile
        probe_roles(cfg)
    assert roles["heavy"]["available"] is True

    with patch.object(llm_client, "list_models", side_effect=OSError("no server")):
        probe_roles(cfg)
    assert roles["judge"]["available"] is False and roles["heavy"]["available"] is False
    assert probe_roles(config()) == [] and probe_roles({}) == []


def test_the_daemon_reports_its_model_roles_at_startup():
    from daemon import Daemon

    with tmp_cwd() as tmp:
        cfg = tmp / "config.yaml"
        cfg.write_text(
            f'daemon:\n  state_db: "{(tmp / "d.db").as_posix()}"\nllm:\n  model: base\nmodels:\n  roles:\n    heavy: {{model: big}}\n  tasks:\n    essay: heavy\n',
            encoding="utf-8",
        )
        d = Daemon(config_path=str(cfg), profile_path=str(tmp / "p.json"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch("daemon.check_health", return_value=(True, "up")), patch("daemon.detect_context_tokens", return_value=None), \
                patch.object(llm_client, "list_models", return_value=["base"]):
            assert d.probe_llm() is True
        assert "heavy" in out.getvalue() and "falling back" in out.getvalue()
        assert d.config["models"]["roles"]["heavy"]["available"] is False
        d.state.close()


# --------------------------------------------------------------------------- the components use their roles

def test_fit_scoring_uses_the_fit_role_and_does_not_reuse_another_models_cached_verdict():
    cfg = config({"judge": {"model": "judge-m", "context_tokens": 4096}, "other": {"model": "other-m"}}, {"fit": "judge"})
    scorer = FitScorer(PROFILE, FACTS, build_llm_cfg(cfg), {}, None)
    seen = []
    with patch("fit.chat_json", side_effect=lambda c, *a, **k: seen.append((c["model"], context_budget(c))) or model_says(80)):
        assert scorer.score(JOB, POSTING).verdict == "apply"
    assert seen == [("judge-m", 4096)]

    cfg["models"]["tasks"] = {"fit": "other"}
    assert FitScorer(PROFILE, FACTS, build_llm_cfg(cfg), {}, None).cache_key != scorer.cache_key     # a different judge re-judges
    plain = FitScorer(PROFILE, FACTS, dict(BASE), {}, None)
    assert plain.llm_cfg["model"] == "base"                                                        # no routing: as before


def test_form_questions_and_essays_can_use_different_roles():
    cfg = config({"pilot": {"model": "coder"}, "heavy": {"model": "big"}}, {"questions": "pilot", "essay": "heavy"})
    solver = QuestionSolver(PROFILE, FACTS, build_llm_cfg(cfg), {})
    assert solver.llm_cfg["model"] == "coder" and solver.essay_cfg["model"] == "big"

    seen = []
    ctx = AnswerContext(PROFILE, {**FACTS, "narrative": {"summary": "Backend engineer."}}, solver.llm_cfg, {}, {"title": "Dev", "company": "Acme"}, None, essay_cfg=solver.essay_cfg)
    question = Question(1, "text", "Do you know Docker?", ["Yes", "No"], True, "", "text", None)
    with patch("answerer.chat_json", side_effect=lambda c, *a, **k: seen.append(c["model"]) or {"answers": [], "answer": "I build APIs."}):
        _ask_model([question], ctx)
        _generate_open_ended(Question(2, "textarea", "Tell us about yourself", [], True, "", "textarea", None), ctx)
    assert seen == ["coder", "big"]

    seen.clear()
    ctx.essay_cfg = {}                                                                          # a context built the old way
    with patch("answerer.chat_json", side_effect=lambda c, *a, **k: seen.append(c["model"]) or {"answer": "I build APIs."}):
        _generate_open_ended(Question(2, "textarea", "Tell us about yourself", [], True, "", "textarea", None), ctx)
    assert seen == ["coder"]

    # end to end: the solver reads a real-looking form and the essay is drafted by the essay model
    class Form:
        def __init__(self):
            self.filled = {}

        def extract_questions(self):
            return [{"id": 1, "kind": "textarea", "label": "Tell us about yourself", "options": [], "required": True, "value": "", "input_type": "textarea"}]

        def fill_answer(self, question, value):
            self.filled[question.id] = value

    seen.clear()
    form = Form()
    solver = QuestionSolver(PROFILE, {**FACTS, "narrative": {"summary": "Backend engineer."}}, build_llm_cfg(cfg), {"open_ended": "generate"})
    with patch("answerer.chat_json", side_effect=lambda c, *a, **k: seen.append(c["model"]) or {"answer": "I build APIs."}):
        report = solver.solve(form, {"title": "Dev", "company": "Acme"})
    assert form.filled == {1: "I build APIs."} and not report.blocking and seen == ["big"]


def test_the_agents_votes_come_from_different_models_and_must_still_agree():
    cfg = config({"quick": {"model": "small"}, "pilot": {"model": "coder"}}, {"agent": ["quick", "pilot"]})
    page = snap_with(("Meet the team", False), ("Let's go!", False))

    def agent():
        return ApplyAgent(StubSession(), {}, {"title": "Backend Engineer", "company": "Acme"}, solve=lambda: None, cfg={"consensus": 2}, llm_cfg=build_llm_cfg(cfg))

    seen = []

    def voters(**by_model):
        def call(c, system, user, **kw):
            seen.append(c["model"])
            return by_model[c["model"]](c, system, user, **kw)
        return call

    with patch("agent.chat_json", voters(small=ScriptedModel(wants="Let's go"), coder=ScriptedModel(wants="Let's go"))):
        d = agent()._decide(page, "v1")
    assert (d.kind, d.action.label) == ("click", "Let's go!") and seen == ["small", "coder"]

    seen.clear()
    with patch("agent.chat_json", voters(small=ScriptedModel(wants="Let's go"), coder=ScriptedModel(wants="Meet the team"))):
        try:
            agent()._decide(page, "v2")
            raise AssertionError("two models that disagree must not be acted on")
        except AgentStop as e:
            assert "guessing" in e.reason and seen == ["small", "coder"]

    seen.clear()
    with patch("agent.chat_json", voters(small=ScriptedModel(wants="Let's go"), coder=ScriptedModel(always=3))):    # the second model says "stop"
        try:
            agent()._decide(page, "v3")
            raise AssertionError
        except AgentStop:
            pass

    plain = ApplyAgent(StubSession(), {}, {}, solve=lambda: None, cfg={"consensus": 2}, llm_cfg=dict(BASE))
    seen.clear()
    with patch("agent.chat_json", voters(base=ScriptedModel(wants="Let's go"))):
        assert plain._decide(page, "v4").action.label == "Let's go!" and seen == ["base", "base"]    # unchanged without routing


def test_stage_3_hands_the_routed_config_to_the_fit_check_the_answers_and_the_agent():
    cfg = config({"judge": {"model": "judge-m"}}, {"fit": "judge"})
    with tmp_cwd() as tmp:
        store = StateStore(tmp / "s.db")
        run_id = store.start_run("stage3", "h", "p")
        job = {**save_job(store, run_id, "j1", "https://boards.greenhouse.io/acme/jobs/1"), "description": POSTING}      # enough text to judge
        run_cfg = {**cfg_for(tmp, "dry_run"), **cfg}
        with fake_playwright(DynPage()), patch("answerer.chat_json", side_effect=LLMError("model down")), patch("fit.chat_json", return_value=model_says(80)) as judge, \
                patch("stage3_apply.run.wait_for_confirmation", return_value=False), \
                patch("stage3_apply.run.FitScorer", wraps=FitScorer) as fit, patch("stage3_apply.run.QuestionSolver", wraps=QuestionSolver) as solver:
            stage3_run([job], dict(RUN_PROFILE, mode="dry_run"), state=store, run_id=run_id, config=run_cfg)
        for spy in (fit, solver):                                                              # both were handed the routed config ...
            routed = next(a for a in spy.call_args.args if isinstance(a, dict) and "model" in a)
            assert routed["tasks"] == {"fit": "judge"} and routed["roles"]["judge"]["model"] == "judge-m"
        assert judge.call_args.args[0]["model"] == "judge-m"                                    # ... and the fit check really asked the judge
        store.close()
