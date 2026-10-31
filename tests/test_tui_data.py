"""The terminal dashboard's data and rendering layers: right numbers, read-only, and robust to a half-missing project.

The Textual app on top of them is tested separately (tests/test_tui_app.py, which needs Textual).
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from state import StateStore
from tests.test_p0_safety import save_job, tmp_cwd
from tui import data, render

NOW = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0).astimezone(timezone.utc)   # local noon: never near a day boundary


def iso(delta: timedelta) -> str:
    return (NOW + delta).isoformat()


def ollama_reply(*models):
    resp = MagicMock()
    resp.read.return_value = json.dumps({"models": list(models)}).encode()
    resp.__enter__.return_value = resp
    return resp


QWEN = {"name": "qwen2.5:7b", "size": 4_800_000_000, "size_vram": 4_800_000_000, "expires_at": "2026-10-30T03:10:05.571405-06:00"}


def add_app(store, run_id, job_id, status, when, *, site="greenhouse", title="Backend Engineer", company="Acme", code=None, message=None, attempts=1):
    save_job(store, run_id, job_id, f"https://boards.greenhouse.io/{company.lower()}/jobs/{job_id}", company=company, title=title)
    finished = None if status in ("in_progress", "needs_review", "unconfirmed") else iso(when)
    store.conn.execute(
        "INSERT INTO applications(job_id, run_id, status, mode, ats, started_at, submitted_at, finished_at, error_code, error_message, attempts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (job_id, run_id, status, "autonomous", site, iso(when - timedelta(minutes=1)), iso(when) if status == "submitted" else None, finished, code, message, attempts),
    )
    store.conn.commit()


def project(tmp: Path, config: str = "") -> StateStore:
    (tmp / "config.yaml").write_text(config or "stage3:\n  rate_limit: {global_per_day: 12}\ndaemon: {interval_seconds: 3600}\n", encoding="utf-8")
    return StateStore(tmp / "state.db")


def runs(store):
    old = store.start_run("stage3", "h", "p")
    new = store.start_run("stage3", "h", "p")
    store.conn.execute("UPDATE runs SET started_at=?, finished_at=?, status='completed' WHERE id=?", (iso(timedelta(hours=-30)), iso(timedelta(hours=-29)), old))
    store.conn.execute("UPDATE runs SET started_at=?, finished_at=?, status='completed' WHERE id=?", (iso(timedelta(minutes=-50)), iso(timedelta(minutes=-40)), new))
    store.conn.commit()
    return old, new


# --------------------------------------------------------------------------- the numbers

def test_the_numbers_count_today_the_rolling_cap_the_funnel_and_the_latest_applications():
    with tmp_cwd() as tmp:
        store = project(tmp)
        old, new = runs(store)
        add_app(store, new, "a1", "submitted", timedelta(hours=-3))
        add_app(store, new, "a2", "skipped", timedelta(hours=-2), code="not_a_fit", message="not a fit (20%)")
        add_app(store, new, "a3", "failed", timedelta(hours=-1), message="no apply button found")
        add_app(store, old, "b1", "submitted", timedelta(hours=-30))                 # yesterday: all time, not today
        add_app(store, old, "b2", "unconfirmed", timedelta(hours=-31))
        store.conn.executemany("INSERT INTO rate_events(domain, job_id, event_at, event_type) VALUES (?,?,?,?)", [
            ("greenhouse", "a1", iso(timedelta(hours=-3)), "submit"), ("greenhouse", "b1", iso(timedelta(hours=-25)), "submit"),
            ("greenhouse", "a3", iso(timedelta(hours=-1)), "attempt"),
        ])
        store.conn.execute("INSERT INTO run_diagnostics(run_id, total_scraped, removed_duplicates, removed_scam) VALUES (?,?,?,?)", (new, 14, 6, 1))
        store.conn.commit()

        snap = data.snapshot(tmp, now=NOW, with_ollama=False)

        assert snap.db_ok and snap.cap_limit == 12
        assert snap.today == {"submitted": 1, "skipped": 1, "failed": 1}
        assert snap.all_time == {"submitted": 2, "skipped": 1, "failed": 1, "unconfirmed": 1}
        assert snap.cap_used == 1                                                    # the 25-hour-old submit and the mere attempt do not count
        assert snap.run["id"] == new and snap.funnel["total_scraped"] == 14 and snap.funnel["removed_duplicates"] == 6
        assert snap.stage3 == {"submitted": 1, "skipped": 1, "failed": 1}
        assert [r.job_id for r in snap.recent[:3]] == ["a3", "a2", "a1"]             # newest first
        assert snap.recent[1].title == "Backend Engineer" and snap.recent[1].error_code == "not_a_fit" and snap.recent[1].detail.startswith("not a fit")
        assert [w[0] for w in snap.week][-1] == NOW.astimezone().strftime("%m-%d") and snap.week[-1][1:] == (1, 2) and len(snap.week) == 7
        store.close()


def test_the_job_in_progress_comes_with_its_agent_steps_and_a_stale_one_is_not_shown_as_live():
    with tmp_cwd() as tmp:
        store = project(tmp)
        _, new = runs(store)
        add_app(store, new, "live1", "in_progress", timedelta(minutes=-1), title="Platform Engineer", company="Globex")
        folder = tmp / "applications" / "live1"
        (folder / "trace").mkdir(parents=True)
        steps = [
            {"step": 1, "page": "job_page", "decision": "click", "target": "Apply now", "why": "the one way forward", "source": "policy"},
            {"step": 2, "page": "form", "decision": "fill", "target": None, "why": "answer the visible questions", "source": "policy"},
            {"step": 3, "page": "unknown", "decision": "click", "target": "Let's go!", "why": "the model chose this in every menu order", "source": "llm"},
        ]
        (folder / "trace" / "steps.jsonl").write_text("\n".join(json.dumps(s) for s in steps) + "\nnot json at all\n", encoding="utf-8")
        (folder / "fit.json").write_text(json.dumps({"verdict": "apply", "score": 0.82, "model": "qwen2.5:7b", "summary": "good match"}), encoding="utf-8")

        snap = data.snapshot(tmp, now=NOW, with_ollama=False)
        assert snap.now and snap.now.live and snap.now.app.title == "Platform Engineer" and len(snap.now.steps) == 3     # the torn line is skipped
        assert snap.now.fit["score"] == 0.82
        chain = render.flow_chain(snap.now.steps, snap.now.fit).plain
        assert chain == "open ▸ fit 82% ✓ ▸ click “Apply now” ▸ fill form ▸ click “Let's go!” ◆"
        assert "Platform Engineer" in render.render_now(snap).plain and "via greenhouse" in render.render_now(snap).plain

        store.conn.execute("UPDATE applications SET started_at=? WHERE job_id='live1'", (iso(timedelta(hours=-3)),))
        store.conn.commit()
        stale = data.snapshot(tmp, now=NOW, with_ollama=False)
        assert stale.now and not stale.now.live                                       # a leftover from a crash is not "working now"
        assert "Platform Engineer" not in render.render_now(stale).plain
        store.close()


def test_questions_sites_and_probation_progress_are_summarised():
    with tmp_cwd() as tmp:
        store = project(tmp, "agent:\n  probation: {enabled: true, clean_runs_to_trust: 3}\n")
        runs(store)
        store.add_pending_questions("j1", [{"label": "How many years of PHP do you have?", "kind": "text", "options": [], "reason": "no fact"}])
        store.add_pending_questions("j2", [{"label": "How many years of PHP do you have?", "kind": "text", "options": [], "reason": "no fact"}])
        store.approve_site("greenhouse")
        store.record_site_submission("linkedin")
        store.record_clean_run("lever", "x1", 3)
        snap = data.snapshot(tmp, now=NOW, with_ollama=False)

        assert len(snap.pending) == 1 and snap.pending[0]["jobs"] == 2                 # one question, asked by two jobs
        by_site = {s["site"]: s for s in snap.sites}
        assert by_site["greenhouse"]["trusted"] and by_site["linkedin"]["trusted"]
        assert not by_site["lever"]["trusted"] and (by_site["lever"]["clean"], by_site["lever"]["needed"]) == (1, 3)
        text = render.render_needs(snap).plain
        assert "1 question need your answer" in text.replace("questions", "question") or "need your answer" in text
        assert "PHP" in text and "greenhouse ✓" in text and "lever 1/3" in text
        store.close()


# --------------------------------------------------------------------------- the daemon and the models

def test_daemon_state_comes_from_the_lock_the_process_and_the_log_never_from_signalling_it():
    with tmp_cwd() as tmp:
        store = project(tmp)
        runs(store)
        (tmp / "daemon.log").write_text("[Stage 1] Ingestion complete.\n[Stage 3] Processing: Backend Engineer @ Acme via greenhouse\n", encoding="utf-8")
        assert store.acquire_lock(pid=os.getpid())
        snap = data.snapshot(tmp, now=NOW, with_ollama=False)
        d = snap.daemon
        assert d.state == "running" and d.pid == os.getpid()                          # the pid is alive, whatever the heartbeat age
        assert d.stage == "Stage 3" and d.activity.startswith("[Stage 3] Processing")
        assert d.next_cycle == NOW + timedelta(minutes=20)                            # the latest run finished 40 min ago; the interval is 1 h

        store.conn.execute("DELETE FROM daemon_lock")
        store.conn.execute("INSERT INTO daemon_lock(id, holder_pid, holder_host, acquired_at, heartbeat_at) VALUES (1, 2147483000, ?, ?, ?)",
                           (__import__("socket").gethostname(), iso(timedelta(hours=-1)), iso(timedelta(minutes=-30))))
        store.conn.commit()
        assert data.snapshot(tmp, now=NOW, with_ollama=False).daemon.state == "stopped"          # that pid does not exist
        assert "daemon stopped" in render.render_header(data.snapshot(tmp, now=NOW, with_ollama=False)).plain
        store.close()

    source = Path(data.__file__).read_text(encoding="utf-8")
    assert "os.kill(" not in source and "TerminateProcess" not in source, "on Windows os.kill TERMINATES the process: never use it here"
    assert data.pid_alive(os.getpid()) in (True, None) and data.pid_alive(2147483000) in (False, None) and data.pid_alive(0) is False


def test_models_come_from_ollamas_api_and_the_configured_roles_show_whether_they_are_loaded():
    cfg = "llm: {model: 'qwen2.5:7b'}\nmodels:\n  roles:\n    judge: {model: 'qwen2.5:7b'}\n    heavy: {model: 'deepseek-coder-v2-lite-local'}\n  tasks:\n    fit: judge\n    agent: [heavy, judge]\n"
    with tmp_cwd() as tmp:
        project(tmp, cfg).close()
        with patch("tui.data.urllib.request.urlopen", return_value=ollama_reply(QWEN)):
            snap = data.snapshot(tmp, now=NOW)
        assert [m.name for m in snap.models] == ["qwen2.5:7b"] and round(snap.models[0].vram_gb, 1) == 4.8
        roles = {r.role: r for r in snap.roles}
        assert roles["judge"].loaded and roles["judge"].tasks == ["fit", "agent"] and not roles["heavy"].loaded and roles["heavy"].tasks == ["agent"]
        text = render.render_models(snap).plain
        assert "qwen2.5:7b" in text and "● judge" in text and "○ heavy" in text
        assert "Ollama" in render.render_header(snap).plain and "4.8 GB" in render.render_header(snap).plain

        with patch("tui.data.urllib.request.urlopen", side_effect=OSError("refused")):
            down = data.snapshot(tmp, now=NOW)
        assert down.models is None and "not answering" in render.render_header(down).plain
        with patch("tui.data.urllib.request.urlopen", return_value=ollama_reply()):
            assert data.snapshot(tmp, now=NOW).models == []
            assert "idle" in render.render_header(data.snapshot(tmp, now=NOW)).plain


# --------------------------------------------------------------------------- safety and robustness

def test_the_dashboard_never_writes_and_its_connection_cannot():
    with tmp_cwd() as tmp:
        store = project(tmp)
        _, new = runs(store)
        add_app(store, new, "a1", "submitted", timedelta(hours=-1))
        before = hashlib.sha256((tmp / "state.db").read_bytes()).hexdigest()
        files_before = sorted(p.name for p in tmp.rglob("*"))
        data.snapshot(tmp, now=NOW, with_ollama=False)
        data.job_detail(tmp, "a1")
        assert hashlib.sha256((tmp / "state.db").read_bytes()).hexdigest() == before
        assert sorted(p.name for p in tmp.rglob("*")) == files_before                 # no new files either
        conn = data._connect(tmp / "state.db")
        try:
            conn.execute("DELETE FROM applications")
            raise AssertionError("the dashboard's database connection must be read-only")
        except sqlite3.Error:
            pass
        finally:
            conn.close()
        store.close()


def test_a_missing_or_broken_project_still_gives_a_snapshot():
    with tmp_cwd() as tmp:
        snap = data.snapshot(tmp, now=NOW, with_ollama=False)                         # empty folder: no db, no log, no config
        assert not snap.db_ok and snap.recent == [] and snap.now is None and snap.cap_limit == 25
        for fn in (render.render_header, render.render_today, render.render_funnel, render.render_models, render.render_now, render.render_needs):
            assert isinstance(fn(snap).plain, str)
        (tmp / "state.db").write_bytes(b"this is not a database")
        (tmp / "config.yaml").write_text("llm: [unclosed", encoding="utf-8")
        assert not data.snapshot(tmp, now=NOW, with_ollama=False).db_ok               # unreadable database: reported, not raised
        assert data.job_detail(tmp, "nope")["app"] is None


def test_job_detail_gathers_the_files_and_titles_with_brackets_cannot_break_rendering():
    with tmp_cwd() as tmp:
        store = project(tmp)
        _, new = runs(store)
        add_app(store, new, "j9", "needs_review", timedelta(hours=-1), title="Dev [Remote] (urgent)", code="needs_answers", message="2 required question(s) need your answer")
        folder = tmp / "applications" / "j9"
        (folder / "trace").mkdir(parents=True)
        (folder / "trace" / "step_01.png").write_bytes(b"png")
        (folder / "answers.json").write_text(json.dumps([{"answered": {"question": "Email", "answer": "x", "source": "profile"}}, {"answered": {"source": "facts"}}, {"unanswered": {"label": "Years of PHP?"}}]), encoding="utf-8")
        (folder / "fit.json").write_text(json.dumps({"verdict": "apply", "score": 0.7, "summary": "ok", "deal_breakers": [{"issue": "x", "quote": "y"}]}), encoding="utf-8")
        detail = data.job_detail(tmp, "j9")
        assert detail["app"]["status"] == "needs_review" and detail["job"]["title"] == "Dev [Remote] (urgent)" and len(detail["shots"]) == 1
        text = render.render_job(detail).plain
        assert "Dev [Remote] (urgent)" in text and "2 filled, 1 not answered" in text and "profile 1" in text and "Years of PHP?" in text and "deal-breaker" in text
        snap = data.snapshot(tmp, now=NOW, with_ollama=False)
        assert "Dev [Remote] (urgent)" in render.app_cells(snap.recent[0], NOW)[1].plain
        store.close()


def test_the_data_layer_needs_nothing_but_the_standard_library():
    """The dashboard runs in its own small environment (Textual + PyYAML), so data.py must not import project modules."""
    tree = ast.parse(Path(data.__file__).read_text(encoding="utf-8"))
    imported = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)} | {
        n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module and n.level == 0
    }
    assert imported <= set(sys.stdlib_module_names) | {"yaml"}, imported - set(sys.stdlib_module_names)


def test_small_formatting_helpers():
    assert render.fmt_age(5) == "5s" and render.fmt_age(300) == "5m" and render.fmt_age(3 * 3600 + 600) == "3h 10m" and render.fmt_age(None) == "?"
    assert render.spark([0, 0]) == "▁▁" and render.spark([1, 4, 8])[-1] == "█" and len(render.spark([])) == 0
    assert render.clip("a  b\nc" * 30, 20).endswith("…") and len(render.clip("x" * 100, 20)) == 20
    assert render.bar(3, 12, 12).plain == "███░░░░░░░░░" and render.bar(99, 12, 4).plain == "████"
    assert data.safe_dirname("https://x.test/a?b=1") == "https_x.test_a_b_1" and data.safe_dirname("...") == "job"


# --------------------------------------------------------------------------- fit decisions

def seed_fit_decisions(store, new):
    veto = {"issue": "clearance", "quote": "must be a US citizen with an active TS/SCI clearance"}
    base = {"seniority_match": "match", "missing_must_haves": [], "summary": "good match"}
    review = lambda agreement, verdict, score: {"agreement": agreement, "model": "review-m", "verdict": verdict, "score": score}      # noqa: E731
    rows = (  # job, status, error_code, message, hours ago, payload
        ("d1", "needs_review", "fit_disagreement", "the fit models disagree: judge-m vetoed it (80%)", -1, {**base, "fit": 0.8, "deal_breakers": [veto], "review": review("disputed", "apply", 0.85)}),
        ("l1", "skipped", "not_a_fit", "not a fit (30%)", -0.5, {**base, "fit": 0.3, "deal_breakers": [], "summary": "needs 10 years of Java"}),
        ("c2", "skipped", "not_a_fit", "not a fit (60%) | confirmed by review-m", -1.5, {**base, "fit": 0.6, "deal_breakers": [veto], "review": review("confirmed", "skip", 0.2)}),
        ("c1", "skipped", "not_a_fit", "you rejected this posting", -2, {**base, "fit": 0.6, "deal_breakers": [veto], "review": review("confirmed", "skip", 0.2)}),
        ("w1", "skipped", None, "fit veto not confirmed (no second opinion yet: refused); will try again", -3, {**base, "fit": 0.8, "deal_breakers": [veto]}),
        ("s1", "skipped", "not_a_fit", "not a fit (60%)", -4, {**base, "fit": 0.6, "deal_breakers": [veto]}),
    )
    for job_id, status, code, message, hours, payload in rows:
        add_app(store, new, job_id, status, timedelta(hours=hours), title=f"Role {job_id}", code=code, message=message)
        store.put_fit(job_id, "k", payload["fit"], "skip", json.dumps(payload), "judge-m")
    add_app(store, new, "ok1", "submitted", timedelta(hours=-1))
    add_app(store, new, "cap1", "skipped", timedelta(hours=-1), message="CAPTCHA encountered")             # a skip that is not about fit
    store.set_fit_override("c1", "skip")


def test_fit_decisions_are_listed_needing_you_first_and_shown_on_the_dashboard():
    with tmp_cwd() as tmp:
        store = project(tmp)
        _, new = runs(store)
        seed_fit_decisions(store, new)
        snap = data.snapshot(tmp, now=NOW, with_ollama=False)
        assert [(v.job_id, v.where) for v in snap.vetoes] == [("d1", "disputed"), ("l1", "low"), ("c2", "confirmed"), ("c1", "rejected"), ("w1", "waiting"), ("s1", "single")]
        d1 = snap.vetoes[0]
        assert d1.title == "Role d1" and d1.score == 0.8 and d1.reviewer == "review-m" and "clearance" in d1.reason and "TS/SCI" in d1.reason

        needs = render.render_needs(snap).plain
        assert "1 job where the fit models disagree" in needs and "Role d1" in needs and "press v" in needs
        assert "1 veto waiting for a second opinion" in needs
        cells = [c.plain for c in render.veto_cells(d1, NOW)]
        assert cells[1] == "Role d1" and cells[3] == "80%" and cells[4] == "models disagree: your call"
        assert [render.VETO_LABEL[v.where] for v in snap.vetoes][-3:] == ["you rejected it", "waiting for a 2nd opinion", "vetoed by one model"]

        store.conn.execute("DROP TABLE fit_overrides")                                                    # a database from before overrides existed
        store.conn.commit()
        old = data.snapshot(tmp, now=NOW, with_ollama=False)
        assert len(old.vetoes) == 6 and old.vetoes[0].where == "disputed"                                  # still listed; just no 'rejected' label
        store.close()

        empty = data.snapshot(tmp / "nowhere", now=NOW, with_ollama=False)
        assert empty.vetoes == [] and "disagree" not in render.render_needs(empty).plain


def test_a_job_shows_the_second_opinion_the_facts_that_waived_a_veto_and_how_to_decide():
    with tmp_cwd() as tmp:
        store = project(tmp)
        _, new = runs(store)
        add_app(store, new, "d1", "needs_review", timedelta(hours=-1), title="Dev [Remote]", code="fit_disagreement", message="the fit models disagree")
        folder = tmp / "applications" / "d1"
        folder.mkdir(parents=True)
        (folder / "fit.json").write_text(json.dumps({
            "verdict": "skip", "score": 0.8, "model": "judge-m", "summary": "good match",
            "deal_breakers": [{"issue": "residence", "quote": "must reside in the region"}],
            "vetoes_met": [{"issue": "residence", "quote": "authorized to work in the United States", "why": "you are authorized to work in US"}],
            "review": {"agreement": "disputed", "model": "review-m", "verdict": "apply", "score": 0.85},
        }), encoding="utf-8")
        text = render.render_job(data.job_detail(tmp, "d1")).plain
        assert "deal-breaker: residence" in text and "waived by your facts: authorized to work in the United States" in text
        assert "second opinion (review-m): would apply (85%)" in text and "decide: python fit_review.py approve|reject d1" in text
        store.close()
