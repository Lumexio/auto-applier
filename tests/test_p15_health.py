"""What only the running daemon knows, shown where a person looks: models it has stopped asking, and a config edited after it loaded.

The daemon writes status.json (daemon_status.py); the terminal dashboard and the web desk read it. Nothing here needs Ollama or a network.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import daemon_status
import llm_client
from config_loader import changed_sections, load_config, section_fingerprints
from daemon import Daemon
from llm_client import LLMError, chat_json
from tests.test_p0_safety import tmp_cwd
from tests.test_p4_outages import Clock, breaker_cfg, flaky, must_fail
from tests.test_tui_data import NOW, iso, project
from tui import data, render

CFG = ("llm: {model: 'qwen2.5:7b'}\nstage3: {mode: dry_run}\nmodels:\n  roles:\n    judge: {model: 'qwen2.5:7b'}\n    heavy: {model: 'deepseek-coder-v2-lite-local'}\n"
       "  tasks:\n    fit: judge\n    agent: [heavy, judge]\n")
BREAKER = {"endpoint": "http://127.0.0.1:11434/v1", "model": "deepseek-coder-v2-lite-local", "failures": 2, "open_for": 90.0}


# --------------------------------------------------------------------------- the config fingerprint

def test_the_config_fingerprint_names_the_sections_that_changed_and_leaks_no_values():
    loaded = {"llm": {"model": "x", "n": 1}, "stage3": {"mode": "dry_run", "rate_limit": {"global_per_day": 25}}, "daemon": {"interval_seconds": 3600}}
    before = section_fingerprints(loaded)
    reordered = {"daemon": {"interval_seconds": 3600}, "stage3": {"rate_limit": {"global_per_day": 25}, "mode": "dry_run"}, "llm": {"n": 1, "model": "x"}}
    assert changed_sections(before, reordered) == [], "key order and formatting are not an edit"
    assert changed_sections(before, {**reordered, "stage3": {**reordered["stage3"], "mode": "autonomous"}}) == ["stage3"]
    assert changed_sections(before, {**reordered, "stage3": {"mode": "dry_run", "rate_limit": {"global_per_day": 26}}}) == ["stage3"], "a nested value counts"
    assert changed_sections(before, {**reordered, "models": {"roles": {}}}) == ["models"], "a section added"
    assert changed_sections(before, {k: v for k, v in reordered.items() if k != "llm"}) == ["llm"], "a section removed"
    assert changed_sections(before, {**reordered, "llm": {"model": "y", "n": 1}, "daemon": {"interval_seconds": 60}}) == ["daemon", "llm"], "sorted"
    assert changed_sections({}, {}) == [] and changed_sections({}, {"a": 1}) == ["a"]
    assert all(len(v) == 12 for v in before.values()) and "dry_run" not in json.dumps(before) and "3600" not in json.dumps(before)
    assert section_fingerprints({"a": {"when": object}}), "a value that is not JSON must not raise"


# --------------------------------------------------------------------------- the breaker report and listener

def test_the_breaker_report_lists_the_models_that_failed_with_the_cooldown_left():
    llm_client.reset_breakers()
    clock, seen, state = Clock(), [], {"up": False}
    out = io.StringIO()
    try:
        with patch.object(llm_client, "_post_chat", flaky(seen, state)), patch.object(llm_client, "_now", clock), contextlib.redirect_stdout(out):
            assert llm_client.breaker_report() == []
            deep, shaky = breaker_cfg("deep"), breaker_cfg("shaky")
            must_fail(shaky)                                                       # one failure of the two it takes: not open, but worth knowing
            must_fail(deep)
            must_fail(deep)
            assert llm_client.breaker_report() == [
                {"endpoint": "http://127.0.0.1:11434/v1", "model": "deep", "failures": 2, "open_for": 60.0},
                {"endpoint": "http://127.0.0.1:11434/v1", "model": "shaky", "failures": 1, "open_for": 0.0},
            ]
            json.dumps(llm_client.breaker_report())                                # it goes into a file
            clock.t += 20.04
            assert llm_client.breaker_report()[0]["open_for"] == 40.0
            clock.t += 100
            assert llm_client.breaker_report()[0] == {"endpoint": "http://127.0.0.1:11434/v1", "model": "deep", "failures": 2, "open_for": 0.0}
            state["up"] = True
            assert chat_json(deep, "s", "u") == {"ok": True}                      # the trial succeeds: it is no longer listed
            assert [r["model"] for r in llm_client.breaker_report()] == ["shaky"]
    finally:
        llm_client.reset_breakers()


def test_a_listener_hears_every_change_of_a_breaker_and_nothing_else_and_cannot_break_a_call():
    llm_client.reset_breakers()
    clock, seen, state, heard = Clock(), [], {"up": False}, []
    cfg = breaker_cfg("deep")
    try:
        with patch.object(llm_client, "_post_chat", flaky(seen, state)), patch.object(llm_client, "_now", clock), contextlib.redirect_stdout(io.StringIO()):
            llm_client.breaker_listener = lambda: heard.append([(r["model"], r["failures"]) for r in llm_client.breaker_report()])
            must_fail(cfg)
            must_fail(cfg)
            assert heard == [[("deep", 1)], [("deep", 2)]]
            clock.t += 100
            state["up"] = True
            chat_json(cfg, "s", "u")
            assert heard[-1] == [] and len(heard) == 3, "told when it closes"
            chat_json(cfg, "s", "u")
            chat_json(cfg, "s", "u")
            assert len(heard) == 3, "a model that keeps answering is not news"

            def broken():
                raise RuntimeError("the status file cannot be written")

            llm_client.breaker_listener = broken
            state["up"] = False
            must_fail(cfg, "refused")                                              # still the model's error, not the listener's
            must_fail(cfg, "refused")
            state["up"] = True
            clock.t += 100
            assert chat_json(cfg, "s", "u") == {"ok": True}
    finally:
        llm_client.breaker_listener = None
        llm_client.reset_breakers()


# --------------------------------------------------------------------------- writing status.json

def test_the_status_file_is_replaced_whole_and_a_failed_write_changes_nothing_and_raises_nothing():
    with tmp_cwd() as tmp:
        path = tmp / "status.json"
        assert daemon_status.write(path, {"pid": 1, "n": 1}) is True and json.loads(path.read_text(encoding="utf-8")) == {"pid": 1, "n": 1}
        assert [p.name for p in tmp.iterdir()] == ["status.json"], "no temporary file left behind"

        assert daemon_status.write(tmp / "missing" / "status.json", {"pid": 1}) is False            # no such folder: reported, not raised
        with patch.object(daemon_status.os, "replace", side_effect=PermissionError("in use")), patch.object(daemon_status.time, "sleep") as nap:
            assert daemon_status.write(path, {"pid": 1, "n": 2}) is False
            assert nap.call_count == 4, "a reader holding the file for a moment is waited out"
        assert json.loads(path.read_text(encoding="utf-8"))["n"] == 1, "the old file stays whole"
        assert [p.name for p in tmp.iterdir()] == ["status.json"]

        real = os.replace
        calls = []

        def busy_twice(src, dst):
            calls.append(1)
            if len(calls) <= 2:
                raise PermissionError("in use")
            return real(src, dst)

        with patch.object(daemon_status.os, "replace", busy_twice), patch.object(daemon_status.time, "sleep"):
            assert daemon_status.write(path, {"pid": 1, "n": 3}) is True
        assert json.loads(path.read_text(encoding="utf-8"))["n"] == 3 and len(calls) == 3

        assert daemon_status.status_path({"daemon": {"state_db": "some/where/state.db"}}) == Path("some/where/status.json")
        assert daemon_status.status_path({}) == Path("status.json") and daemon_status.status_path(None) == Path("status.json")


def test_only_the_daemon_that_wrote_the_status_file_takes_it_away():
    with tmp_cwd() as tmp:
        path = tmp / "status.json"
        daemon_status.write(path, {"pid": 1234})
        daemon_status.remove(path, pid=999)
        assert path.exists(), "a newer daemon's file is not ours to delete"
        daemon_status.remove(path, pid=1234)
        assert not path.exists()
        daemon_status.remove(path, pid=1234)                                       # already gone
        path.write_text("not json", encoding="utf-8")
        daemon_status.remove(path, pid=1234)
        path.write_text("[1, 2]", encoding="utf-8")
        daemon_status.remove(path, pid=1234)
        assert path.exists(), "something that is not ours stays"


def daemon_in(tmp: Path) -> Daemon:
    cfg = tmp / "config.yaml"
    cfg.write_text(f'daemon:\n  state_db: "{(tmp / "d.db").as_posix()}"\nllm: {{model: "m"}}\nstage3: {{mode: dry_run}}\n', encoding="utf-8")
    return Daemon(config_path=str(cfg), profile_path=str(tmp / "profile.json"))


def test_the_daemon_writes_its_status_only_once_it_holds_the_lock_and_at_most_every_few_seconds():
    import time

    with tmp_cwd() as tmp:
        d = daemon_in(tmp)
        target = tmp / "status.json"
        assert d.status_file == target
        d._beat()
        assert not target.exists(), "an instance that has not taken the lock must not write (the real daemon's file is there)"

        d._publishing = True
        d._beat()
        first = json.loads(target.read_text(encoding="utf-8"))
        assert first["pid"] == os.getpid() and first["breakers"] == [] and first["started_at"] == d.started_at
        assert first["config_path"] == str((tmp / "config.yaml").resolve()) and first["config_sections"] == section_fingerprints(load_config(tmp / "config.yaml"))

        d._beat()                                                                  # a moment later: the file is left alone
        assert json.loads(target.read_text(encoding="utf-8"))["updated_at"] == first["updated_at"]
        d._status_at = time.monotonic() - daemon_status.WRITE_EVERY_S - 1
        d._beat()
        assert json.loads(target.read_text(encoding="utf-8"))["updated_at"] != first["updated_at"]
        d._publish_status(force=True)                                              # forced: now, whatever the clock says
        forced = json.loads(target.read_text(encoding="utf-8"))["updated_at"]
        d._publish_status(force=True)
        assert json.loads(target.read_text(encoding="utf-8"))["updated_at"] != forced

        d.config["llm"]["effective_context_tokens"] = 4096                        # what the startup probe adds to the config in memory
        d.config["models"] = {"roles": {"x": {"model": "m", "available": False}}}
        d._publish_status(force=True)
        recorded = json.loads(target.read_text(encoding="utf-8"))["config_sections"]
        assert changed_sections(recorded, load_config(tmp / "config.yaml")) == [], "the fingerprint is of the file as loaded, not of the probed config"
        d.state.close()


def test_start_publishes_while_it_runs_hears_breakers_at_once_and_takes_the_file_away_when_it_stops():
    with tmp_cwd() as tmp:
        d = daemon_in(tmp)
        target = tmp / "status.json"
        seen: dict = {}
        cfg = {"endpoint": "http://127.0.0.1:11434/v1", "model": "deep", "breaker": {"failures": 1, "cooldown_seconds": 60}}

        def cycle(dry_run=False):
            seen["on_start"] = json.loads(target.read_text(encoding="utf-8"))
            with contextlib.redirect_stdout(io.StringIO()):
                llm_client._breaker_failed(cfg)                                    # no heartbeat in between: the listener must write it now
            seen["after_failure"] = json.loads(target.read_text(encoding="utf-8"))["breakers"]
            llm_client._breaker_ok(cfg)
            seen["after_recovery"] = json.loads(target.read_text(encoding="utf-8"))["breakers"]

        saved = sys.stdout, sys.stderr
        llm_client.reset_breakers()
        try:
            with patch.object(d, "probe_llm"), patch.object(d, "install_signals"), patch.object(d, "run_cycle", cycle), patch("daemon.LoggerTee", lambda name: io.StringIO()):
                d.start(once=True)
        finally:
            sys.stdout, sys.stderr = saved
            llm_client.reset_breakers()
        assert seen["on_start"]["pid"] == os.getpid() and seen["on_start"]["breakers"] == []
        assert [b["model"] for b in seen["after_failure"]] == ["deep"] and seen["after_recovery"] == []
        assert not target.exists(), "a stopped daemon leaves no status behind"
        assert llm_client.breaker_listener is None and d._publishing is False


def test_a_second_instance_that_does_not_get_the_lock_leaves_the_running_daemons_file_alone():
    with tmp_cwd() as tmp:
        d = daemon_in(tmp)
        other = Daemon(config_path=str(tmp / "config.yaml"), profile_path=str(tmp / "profile.json"))
        assert other.state.acquire_lock(pid=999_999, host="some-other-host") is True            # the real daemon, elsewhere
        daemon_status.write(tmp / "status.json", {"pid": 999_999, "breakers": []})
        saved = sys.stdout, sys.stderr
        try:
            with patch("daemon.LoggerTee", lambda name: io.StringIO()), patch.object(d, "probe_llm") as probe:
                try:
                    d.start(once=True)
                    raise AssertionError("it must refuse to start")
                except SystemExit as e:
                    assert e.code == 1
        finally:
            sys.stdout, sys.stderr = saved
        assert not probe.called and d._publishing is False
        assert json.loads((tmp / "status.json").read_text(encoding="utf-8"))["pid"] == 999_999
        d.state.close()
        other.state.close()


# --------------------------------------------------------------------------- reading it in the dashboard

def hold_lock(store, pid, heartbeat=timedelta(seconds=-10)):
    store.conn.execute("DELETE FROM daemon_lock")
    store.conn.execute("INSERT INTO daemon_lock(id, holder_pid, holder_host, acquired_at, heartbeat_at) VALUES (1, ?, ?, ?, ?)",
                       (pid, socket.gethostname(), iso(timedelta(hours=-1)), iso(heartbeat)))
    store.conn.commit()


def put_status(tmp, *, pid=None, breakers=(), age=30, config_path=None, sections=None, raw=None, clock=None):
    payload = {"pid": os.getpid() if pid is None else pid, "started_at": iso(timedelta(hours=-1)), "updated_at": iso(timedelta(seconds=-age)) if clock is None else (clock - timedelta(seconds=age)).isoformat(),
               "config_path": str(config_path or tmp / "config.yaml"), "config_sections": section_fingerprints(load_config(tmp / "config.yaml")) if sections is None else sections,
               "breakers": list(breakers)}
    (tmp / "status.json").write_text(raw if raw is not None else json.dumps(payload), encoding="utf-8")


def snap_of(tmp):
    return data.snapshot(tmp, now=NOW, with_ollama=False)


def test_a_model_the_daemon_has_stopped_asking_is_marked_and_its_cooldown_counts_down():
    with tmp_cwd() as tmp:
        store = project(tmp, CFG)
        hold_lock(store, os.getpid())
        put_status(tmp, breakers=[BREAKER])
        files = sorted(p.name for p in tmp.iterdir())
        status_bytes = (tmp / "status.json").read_bytes()

        snap = snap_of(tmp)
        assert snap.daemon.state == "running" and snap.config_changed == []
        assert [(b.model, b.failures, b.retry_in) for b in snap.breakers] == [("deepseek-coder-v2-lite-local", 2, 60.0)], "90 s left when it was written, 30 s ago"
        roles = {r.role: r for r in snap.roles}
        assert roles["heavy"].breaker.retry_in == 60.0 and roles["judge"].breaker is None
        models = render.render_models(snap).plain
        assert "heavy" in models and "⚠ not answering (2 requests in a row), asked again in 60s" in models
        warnings = render.render_warnings(snap).plain
        assert warnings.startswith("⚠ model deepseek-coder-v2-lite-local is not answering") and "base model" in warnings and "config edited" not in warnings
        assert sorted(p.name for p in tmp.iterdir()) == files and (tmp / "status.json").read_bytes() == status_bytes, "reading it writes nothing"

        put_status(tmp, breakers=[BREAKER], age=200)                              # written long ago: the cool-down has run out since
        later = snap_of(tmp)
        assert later.breakers[0].retry_in == 0.0
        assert "the last 2 requests got no answer" in render.render_models(later).plain and render.render_warnings(later).plain == "", "it will be asked again: nothing to do"

        put_status(tmp, breakers=[{**BREAKER, "failures": 1, "open_for": 0.0}])
        assert "the last 1 request got no answer" in render.render_models(snap_of(tmp)).plain
        store.close()


def test_a_failing_base_model_is_shown_even_when_no_role_uses_it():
    with tmp_cwd() as tmp:
        store = project(tmp, "llm: {model: 'qwen2.5:7b'}\n")
        hold_lock(store, os.getpid())
        put_status(tmp, breakers=[{**BREAKER, "model": "qwen2.5:7b"}])
        snap = snap_of(tmp)
        assert snap.roles == [] and [b.model for b in snap.breakers] == ["qwen2.5:7b"]
        assert "⚠ qwen2.5:7b: not answering (2 requests in a row), asked again in 60s" in render.render_models(snap).plain
        store.close()


def test_a_config_edited_after_the_daemon_started_is_named_by_section_and_never_guessed():
    with tmp_cwd() as tmp:
        store = project(tmp, CFG)
        hold_lock(store, os.getpid())
        put_status(tmp)
        assert snap_of(tmp).config_changed == [] and render.render_warnings(snap_of(tmp)).plain == ""

        local = tmp / "config.local.yaml"
        local.write_text("stage3: {mode: autonomous}\nmodels: {tasks: {fit: heavy}}\n", encoding="utf-8")        # your settings over the defaults
        snap = snap_of(tmp)
        assert snap.config_changed == ["models", "stage3"]
        warnings = render.render_warnings(snap).plain
        assert "config edited since the daemon started (models, stage3)" in warnings and "./setup.bat -StopAll" in warnings and "-All -Background" in warnings

        local.unlink()
        assert snap_of(tmp).config_changed == [], "putting it back clears the warning: content is compared, not file times"
        (tmp / "config.yaml").write_text(CFG.replace("{mode: dry_run}", "{ mode: dry_run }"), encoding="utf-8")
        assert snap_of(tmp).config_changed == [], "formatting is not an edit"

        local.write_text("stage3: {mode: autonomous}\n", encoding="utf-8")
        put_status(tmp, config_path=tmp / "other.yaml", sections=section_fingerprints({"stage3": {}}))
        assert snap_of(tmp).config_changed == [], "started on another config file: nothing to compare with"
        put_status(tmp, sections=section_fingerprints({"stage3": {"mode": "dry_run"}}))
        assert snap_of(tmp).config_changed == ["llm", "models", "stage3"], "sections the daemon never recorded count as changed too"
        status = json.loads((tmp / "status.json").read_text(encoding="utf-8"))
        del status["config_sections"]
        (tmp / "status.json").write_text(json.dumps(status), encoding="utf-8")
        assert snap_of(tmp).config_changed == [], "a status file from before this feature says nothing about the config"

        put_status(tmp, breakers=[BREAKER])
        (tmp / "config.yaml").write_text("llm: [unclosed", encoding="utf-8")                          # caught mid-edit
        broken = snap_of(tmp)
        assert broken.config_changed == [] and [b.model for b in broken.breakers] == ["deepseek-coder-v2-lite-local"], "no false alarm, and the rest still shows"
        store.close()


def test_a_status_file_is_only_trusted_while_the_daemon_that_wrote_it_is_the_one_running():
    with tmp_cwd() as tmp:
        store = project(tmp, CFG)
        hold_lock(store, os.getpid())
        (tmp / "config.local.yaml").write_text("stage3: {mode: autonomous}\n", encoding="utf-8")
        put_status(tmp, breakers=[BREAKER], sections=section_fingerprints({"stage3": {"mode": "dry_run"}}))
        mine = snap_of(tmp)
        assert mine.breakers and mine.config_changed, "control: this one is believed"

        put_status(tmp, pid=os.getpid() + 1, breakers=[BREAKER], sections=section_fingerprints({"stage3": {"mode": "dry_run"}}))
        other = snap_of(tmp)
        assert other.breakers == [] and other.config_changed == [] and render.render_warnings(other).plain == "", "left by an earlier daemon"

        dead = 2147483000
        hold_lock(store, dead)
        put_status(tmp, pid=dead, breakers=[BREAKER], sections=section_fingerprints({"stage3": {"mode": "dry_run"}}))
        gone = snap_of(tmp)
        assert gone.daemon.state == "stopped" and gone.breakers == [] and gone.config_changed == [], "its process is gone: so are its breakers"

        store.conn.execute("DELETE FROM daemon_lock")
        store.conn.commit()
        assert snap_of(tmp).breakers == []
        store.close()


def test_a_damaged_or_odd_status_file_never_breaks_the_dashboard():
    with tmp_cwd() as tmp:
        store = project(tmp, CFG)
        hold_lock(store, os.getpid())
        for raw in ("", "not json", "[]", "null", '{"pid": ', json.dumps({"pid": os.getpid()}), json.dumps({"pid": os.getpid(), "breakers": 5, "config_sections": [1]})):
            put_status(tmp, raw=raw)
            snap = snap_of(tmp)
            assert snap.breakers == [] and snap.config_changed == [] and isinstance(render.render_models(snap).plain, str), raw
        (tmp / "status.json").unlink()
        assert snap_of(tmp).breakers == []

        good = {**BREAKER, "model": "good-model"}
        put_status(tmp, breakers=[{"model": "no-failures-key"}, {**BREAKER, "failures": "two"}, "junk", None, {**BREAKER, "failures": 0}, {**BREAKER, "failures": -1}, {"failures": 1}, good])
        assert [b.model for b in snap_of(tmp).breakers] == ["good-model"], "bad entries are skipped, not fatal, and a model with no failures is not listed"
        store.close()


# --------------------------------------------------------------------------- the web desk

def test_the_web_desk_shows_the_same_two_warnings_as_plain_text():
    from gui.service import Desk

    with tmp_cwd() as tmp:
        store = project(tmp, CFG)
        hold_lock(store, os.getpid())
        desk = Desk(tmp)
        clock = datetime.now(timezone.utc)                                          # the desk reads the real clock
        put_status(tmp, clock=clock)
        assert desk.overview()["alerts"] == []
        (tmp / "config.local.yaml").write_text("stage3: {mode: autonomous}\n", encoding="utf-8")
        started_with = {**load_config(tmp / "config.yaml"), "stage3": {"mode": "dry_run"}}
        started_with = {k: v for k, v in started_with.items()}
        put_status(tmp, breakers=[BREAKER], sections=section_fingerprints(started_with), clock=clock)
        alerts = desk.overview()["alerts"]
        assert len(alerts) == 2 and "edited after the daemon started (stage3)" in alerts[0] and "./setup.bat -StopAll" in alerts[0]
        assert "deepseek-coder-v2-lite-local is not answering (2 requests in a row)" in alerts[1] and ("59 seconds" in alerts[1] or "60 seconds" in alerts[1])
        put_status(tmp, breakers=[BREAKER], age=500, clock=clock)                              # cooled down, and the file records the config as it is now
        assert desk.overview()["alerts"] == []
        store.close()

    page = (Path(__file__).resolve().parent.parent / "gui" / "static" / "index.html").read_text(encoding="utf-8")
    assert "drawAlerts();" in page and 'id="alerts"' in page and "state.overview.alerts" in page
    assert "innerHTML" not in page, "text only: a model name or a section is never markup"
