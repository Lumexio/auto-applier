"""When a model is not answering or the machine is short of memory: the circuit breaker and the RAM guard.

A model that just failed must not be asked again and again (each ask can cost a full timeout); a role that needs more RAM than
there is must give way to the main model. Neither may ever make a task worse off than it was with a single model.
"""

from __future__ import annotations

import contextlib
import io
import json
from unittest.mock import patch

import llm_client
from llm_client import LLMError, build_llm_cfg, chat_json, for_task
from state import StateStore
from tests.test_p0_safety import app_row, save_job, tmp_cwd
from tests.test_p4_models import ENDPOINT, config, ok_reply


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def breaker_cfg(model="m", **spec):
    return {"endpoint": ENDPOINT, "model": model, "context_tokens": 4096, "breaker": {"failures": 2, "cooldown_seconds": 60, **spec}}


def flaky(seen: list[str], state: dict):
    """A stand-in for _post_chat that raises while state['up'] is False."""

    def post(cfg, payload, timeout):
        seen.append(payload["model"])
        if not state["up"]:
            raise LLMError("ConnectionError: refused")
        return {"choices": [{"message": {"content": '{"ok": true}'}}], "usage": {"prompt_tokens": 20}}

    return post


def must_fail(cfg, fragment=""):
    try:
        chat_json(cfg, "s", "u")
    except LLMError as e:
        assert fragment in str(e), str(e)
        return
    raise AssertionError("expected an LLMError")


# --------------------------------------------------------------------------- a model that is not answering

def test_a_model_that_keeps_failing_is_left_alone_for_a_while_and_then_tried_once():
    llm_client.reset_breakers()
    clock, seen, state, cfg = Clock(), [], {"up": False}, breaker_cfg()
    out = io.StringIO()
    with patch.object(llm_client, "_post_chat", flaky(seen, state)), patch.object(llm_client, "_now", clock), contextlib.redirect_stdout(out):
        must_fail(cfg, "refused")
        must_fail(cfg, "refused")
        assert len(seen) == 2 and llm_client.breaker_status(cfg) == {"failures": 2, "open_for": 60.0}
        must_fail(cfg, "did not answer 2 requests in a row")
        assert len(seen) == 2, "while cooling down no request may be sent"
        clock.t += 30
        must_fail(cfg, "not asking it again")
        assert len(seen) == 2 and llm_client.breaker_status(cfg)["open_for"] == 30.0

        clock.t += 31                                                                   # cool-down over: ONE trial request goes through
        must_fail(cfg, "refused")
        assert len(seen) == 3
        must_fail(cfg, "did not answer")                                                # and a failed trial reopens it at once
        assert len(seen) == 3 and llm_client.breaker_status(cfg)["open_for"] == 60.0

        clock.t += 61
        state["up"] = True                                                              # the model is back: the trial succeeds and closes it
        assert chat_json(cfg, "s", "u") == {"ok": True}
        assert llm_client.breaker_status(cfg) == {"failures": 0, "open_for": 0.0}
        assert chat_json(cfg, "s", "u") == {"ok": True} and len(seen) == 5
    text = out.getvalue()
    assert text.count("leaving it alone") == 1 and text.count("is answering again") == 1   # said once per outage, not on every cool-down
    llm_client.reset_breakers()


def test_only_requests_that_got_no_answer_count_toward_the_breaker():
    llm_client.reset_breakers()
    cfg = breaker_cfg()
    junk = {"choices": [{"message": {"content": "not json at all"}}], "usage": {"prompt_tokens": 20}}
    calls = []
    with patch.object(llm_client, "_post_chat", lambda c, p, t: calls.append(1) or junk):
        for _ in range(4):
            must_fail(cfg, "JSON")                                                       # bad JSON: the model answered, badly
    assert len(calls) == 8 and llm_client.breaker_status(cfg)["failures"] == 0
    with patch.object(llm_client, "_post_chat", side_effect=AssertionError("must not be called")):
        for _ in range(3):
            try:
                chat_json(cfg, "s", "word " * 5000)                                       # too big for the window: refused before any request
            except LLMError as e:
                assert "does not fit" in str(e)
    assert llm_client.breaker_status(cfg)["failures"] == 0

    seen, state = [], {"up": False}
    with patch.object(llm_client, "_post_chat", flaky(seen, state)):
        must_fail(cfg, "refused")
        state["up"] = True
        assert chat_json(cfg, "s", "u") == {"ok": True}                                   # a success in between starts the count again
        state["up"] = False
        must_fail(cfg, "refused")
        assert llm_client.breaker_status(cfg) == {"failures": 1, "open_for": 0.0}
    llm_client.reset_breakers()


def test_the_breaker_is_per_model_and_can_be_switched_off():
    llm_client.reset_breakers()
    seen, state = [], {"up": False}
    clock = Clock()
    with patch.object(llm_client, "_post_chat", flaky(seen, state)), patch.object(llm_client, "_now", clock):
        a, b = breaker_cfg("a"), breaker_cfg("b")
        must_fail(a, "refused")
        must_fail(a, "refused")
        must_fail(a, "did not answer")
        state["up"] = True
        assert chat_json(b, "s", "u") == {"ok": True}                                       # model b is unaffected by a's outage

        for off in ({"failures": 0}, False, None):
            cfg = {**breaker_cfg("c"), "breaker": off}
            if off is None:
                cfg.pop("breaker")                                                           # a bare dict (as most tests use) has no breaker at all
            state["up"] = False
            n = len(seen)
            for _ in range(4):
                must_fail(cfg, "refused")
            assert len(seen) == n + 4 and llm_client.breaker_status(cfg)["failures"] == 0     # asked every time
    assert build_llm_cfg({"llm": {"model": "x", "breaker": {"failures": 5}}})["breaker"] == {"failures": 5}      # the user's setting wins
    assert build_llm_cfg({"llm": {"model": "x", "breaker": False}})["breaker"] is False
    llm_client.reset_breakers()


def test_a_role_whose_model_is_cooling_down_goes_to_the_base_model_without_waiting():
    llm_client.reset_breakers()
    heavy = for_task(build_llm_cfg(config({"heavy": {"model": "big"}}, {"essay": "heavy"})), "essay")
    seen: list[str] = []
    clock = Clock()
    with patch.object(llm_client, "_post_chat", ok_reply(seen, big="down")), patch.object(llm_client, "_now", clock), contextlib.redirect_stdout(io.StringIO()):
        assert chat_json(heavy, "s", "u") == {"ok": True}
        assert chat_json(heavy, "s", "u") == {"ok": True}
        assert seen == ["big", "base", "big", "base"]                                     # two slow failures, each answered by the base model
        seen.clear()
        for _ in range(3):
            assert chat_json(heavy, "s", "u") == {"ok": True}
        assert seen == ["base", "base", "base"], "the dead role must not be asked at all while it cools down"
    llm_client.reset_breakers()


def test_refunding_an_attempt_gives_one_back_and_never_goes_below_zero():
    with tmp_cwd() as tmp:
        store = StateStore(tmp / "s.db")
        rid = store.start_run("stage3", "h", "p")
        save_job(store, rid, "j1", "https://boards.greenhouse.io/acme/jobs/1")
        with store.transaction("j1", rid, "autonomous") as tx:
            assert app_row(store, "j1")["attempts"] == 1                                    # starting an application is one attempt
            tx.mark("skipped", error="model down")
            tx.refund_attempt()
            assert app_row(store, "j1")["attempts"] == 0
            tx.refund_attempt()
            tx.refund_attempt()
            assert app_row(store, "j1")["attempts"] == 0
        store.close()


# --------------------------------------------------------------------------- memory

def test_a_role_that_needs_more_free_ram_than_there_is_gives_way_to_the_main_model():
    roles = {"heavy": {"model": "big", "min_free_ram_gb": 5.5}, "light": {"model": "small"}}
    llm = build_llm_cfg(config(roles, {"agent": ["heavy", "light"]}))
    clock = Clock()
    out = io.StringIO()
    with patch.object(llm_client, "loaded_models", return_value=set()), patch.object(llm_client, "_now", clock), contextlib.redirect_stdout(out):
        with patch.object(llm_client, "free_ram_gb", return_value=3.0):
            assert for_task(llm, "agent", 0)["model"] == "base"                           # 3 GB free, wants 5.5: not this time
            assert for_task(llm, "agent", 1)["model"] == "small"                           # a role with no RAM requirement is untouched
            for_task(llm, "agent", 0)
        with patch.object(llm_client, "free_ram_gb", return_value=8.0):
            heavy = for_task(llm, "agent", 0)
            assert heavy["model"] == "big" and "min_free_ram_gb" not in heavy              # enough RAM: used, and the setting is not a request field
        with patch.object(llm_client, "free_ram_gb", return_value=None):
            assert for_task(llm, "agent", 0)["model"] == "big"                             # RAM unreadable: do not block on a guess
        text = out.getvalue()
        assert text.count("role 'heavy' not used right now") == 1 and "3.0 GB" in text     # told once, not on every request
        clock.t += 301
        with patch.object(llm_client, "free_ram_gb", return_value=3.0):
            for_task(llm, "agent", 0)
        assert out.getvalue().count("role 'heavy' not used right now") == 2

    with patch.object(llm_client, "loaded_models", return_value={"big:latest"}), patch.object(llm_client, "free_ram_gb", return_value=1.0):
        assert for_task(llm, "agent", 0)["model"] == "big"                                 # already in memory: it costs no more RAM, so no eviction


def test_the_machines_free_ram_and_ollamas_loaded_models_can_be_read():
    free = llm_client.free_ram_gb()
    assert free is None or 0 < free < 100_000
    llm_client._loaded_cache.clear()
    resp = io.BytesIO(json.dumps({"models": [{"name": "qwen2.5:7b"}, {"name": "big:latest"}]}).encode())
    resp.__enter__ = lambda s=resp: s
    resp.__exit__ = lambda *a: False
    with patch("urllib.request.urlopen", return_value=resp) as urlopen:
        assert llm_client.loaded_models("http://127.0.0.1:11434/v1") == {"qwen2.5:7b", "big:latest"}
        assert llm_client.loaded_models("http://127.0.0.1:11434/v1") == {"qwen2.5:7b", "big:latest"} and urlopen.call_count == 1   # cached for a few seconds
        assert urlopen.call_args.args[0] == "http://127.0.0.1:11434/api/ps"
    llm_client._loaded_cache.clear()
    with patch("urllib.request.urlopen", side_effect=OSError("refused")):
        assert llm_client.loaded_models("http://127.0.0.1:11434/v1") is None                # not answering / not Ollama
    llm_client._loaded_cache.clear()
