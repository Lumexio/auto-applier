"""A made-up project to look at the dashboard (``python -m tui --demo``) and to test it. Fictional companies, a temporary folder;
nothing here touches the real project. Standard library only."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import data

CONFIG = """\
daemon: {interval_seconds: 3600}
stage3: {mode: autonomous, rate_limit: {global_per_day: 25}}
llm: {endpoint: "http://127.0.0.1:11434/v1", model: "qwen2.5:7b"}
agent: {probation: {enabled: true, clean_runs_to_trust: 3}}
models:
  roles:
    judge: {model: "qwen2.5:7b"}
    pilot: {model: "qwen2.5-coder-7b-local"}
    heavy: {model: "deepseek-coder-v2-lite-local"}
  tasks: {fit: judge, questions: judge, agent: [pilot, heavy]}
"""

LOG = """\
[Daemon] Starting cycle (Run ID: 96) at {t0}
[Stage 1] Ingestion complete. Scraped 14 total listings.
[Stage 2] Filtering complete. 3 qualified jobs (filtered: 0 exclusions, 6 duplicates, 0 scams).
[Stage 3] Processing: Platform Engineer @ Globex via greenhouse
[Stage 3] Skipping Staff Backend Engineer @ Initech: not a fit (35%): needs 10+ years of Java
[Stage 3] Processing: Senior Backend Engineer @ Northwind via lever
"""


def _iso(now: datetime, **delta) -> str:
    return (now - timedelta(**delta)).isoformat()


def build(dest: Path | None = None) -> Path:
    now = datetime.now(timezone.utc)
    root = Path(dest) if dest else Path(tempfile.mkdtemp(prefix="auto-applier-demo-"))
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text(CONFIG, encoding="utf-8")
    (root / "daemon.log").write_text(LOG.format(t0=now.astimezone().strftime("%Y-%m-%d %H:%M:%S")), encoding="utf-8")

    conn = sqlite3.connect(root / "state.db")
    conn.executescript((Path(__file__).resolve().parent.parent / "schema.sql").read_text(encoding="utf-8"))
    runs = []
    for i, (started, finished, status) in enumerate([(dict(hours=50), dict(hours=49), "completed"), (dict(hours=26), dict(hours=25), "completed"), (dict(minutes=4), None, "running")], 94):
        conn.execute("INSERT INTO runs(id, started_at, finished_at, status, stage, config_hash) VALUES (?,?,?,?,?,?)",
                     (i, _iso(now, **started), _iso(now, **finished) if finished else None, status, "stage3", "h"))
        runs.append(i)
    conn.execute("INSERT INTO run_diagnostics(run_id, total_scraped, removed_duplicates, removed_scam, removed_relevance, removed_salary) VALUES (96, 14, 6, 0, 3, 1)")

    jobs = [  # id, title, company, site, status, when (minutes ago), error_code, message
        ("n1", "Senior Backend Engineer", "Northwind", "lever", "in_progress", 3, None, None),
        ("g1", "Platform Engineer", "Globex", "greenhouse", "submitted", 12, None, None),
        ("i1", "Staff Backend Engineer", "Initech", "greenhouse", "skipped", 9, "not_a_fit", "not a fit (35%): needs 10+ years of Java"),
        ("h1", "Python Developer", "Hooli", "linkedin", "needs_review", 55, "needs_answers", "1 required question(s) need your answer: How many years of PHP do you have?"),
        ("u1", "Backend Engineer", "Umbrella Labs", "linkedin", "failed", 70, None, "linkedin: no apply button found on job posting"),
        ("w1", "Software Engineer II", "Wayne Systems", "greenhouse", "submitted", 150, None, None),
        ("c1", "API Engineer", "Contoso", "workday", "skipped", 200, "not_a_fit", "not a fit (42%): requires on-site work in Singapore"),
        ("f1", "Backend Developer", "Fabrikam", "lever", "unconfirmed", 60 * 26, None, "clicked submit, no confirmation seen"),
        ("t1", "Senior Software Engineer", "Tailspin", "greenhouse", "submitted", 60 * 27, None, None),
        ("a1", "Data Engineer", "Acme Corp", "linkedin", "submitted", 60 * 30, None, None),
        ("a2", "Python Engineer", "Adatum", "agent", "needs_review", 60 * 31, "probation", "held on probation: agent site not yet trusted"),
        ("v1", "Senior Python Developer", "Hooli", "greenhouse", "needs_review", 40, "fit_disagreement",
         "the fit models disagree: qwen2.5:7b vetoed it (80%: good match | deal-breaker: residence); deepseek-coder-v2-lite-local would apply (85%)"),
        ("v2", "Backend Engineer", "Defense Systems", "lever", "skipped", 95, "not_a_fit", "not a fit (60%): good match | deal-breaker: clearance | confirmed by deepseek-coder-v2-lite-local"),
        ("v3", "Platform Engineer", "Contoso", "greenhouse", "skipped", 15, None, "fit veto not confirmed (no second opinion yet: refused); will try again"),
    ]
    for jid, title, company, site, status, ago, code, msg in jobs:
        when = now - timedelta(minutes=ago)
        conn.execute("INSERT INTO jobs(id, run_id, title, company, company_norm, location, apply_url, apply_url_canon, source, score, ats, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (jid, 96 if ago < 1200 else 95, title, company, company.lower(), "Remote", f"https://{site}.example.test/{jid}", f"https://{site}.example.test/{jid}", "demo", 0.8, None, "pending", when.isoformat(), when.isoformat()))
        finished = None if status in ("in_progress", "needs_review", "unconfirmed") else when.isoformat()
        conn.execute("INSERT INTO applications(job_id, run_id, status, mode, ats, started_at, submitted_at, finished_at, error_code, error_message, attempts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (jid, 96 if ago < 1200 else 95, status, "autonomous", site, (when - timedelta(minutes=1)).isoformat(), when.isoformat() if status == "submitted" else None, finished, code, msg, 1))
        if status == "submitted":
            conn.execute("INSERT INTO rate_events(domain, job_id, event_at, event_type) VALUES (?,?,?,?)", (site, jid, when.isoformat(), "submit"))
    base = {"seniority_match": "match", "missing_must_haves": []}
    clearance = {"issue": "clearance", "quote": "must be a US citizen with an active TS/SCI clearance"}
    residence = {"issue": "residence", "quote": "must already reside in the region and cannot work remotely"}
    for jid, score, payload in (
        ("v1", 0.8, {**base, "fit": 0.8, "deal_breakers": [residence], "summary": "good match",
                     "review": {"agreement": "disputed", "model": "deepseek-coder-v2-lite-local", "verdict": "apply", "score": 0.85, "deal_breakers": [], "summary": "the posting allows remote work"}}),
        ("v2", 0.6, {**base, "fit": 0.6, "deal_breakers": [clearance], "summary": "good match",
                     "review": {"agreement": "confirmed", "model": "deepseek-coder-v2-lite-local", "verdict": "skip", "score": 0.2, "deal_breakers": [clearance], "summary": "clearance required"}}),
        ("v3", 0.8, {**base, "fit": 0.8, "deal_breakers": [clearance], "summary": "good match"}),
        ("i1", 0.35, {**base, "fit": 0.35, "deal_breakers": [], "summary": "needs 10+ years of Java"}),
    ):
        conn.execute("INSERT INTO fit_scores(job_id, cache_key, score, verdict, payload, model, created_at) VALUES (?,?,?,?,?,?,?)",
                     (jid, "k", score, "skip", json.dumps(payload), "qwen2.5:7b", _iso(now, minutes=30)))
    conn.execute("INSERT INTO pending_questions(job_id, question_key, question_text, kind, options, reason, first_seen_at) VALUES ('h1','years-php','How many years of work experience do you have with PHP?','text','','no fact', ?)", (_iso(now, minutes=55),))
    conn.execute("INSERT INTO pending_questions(job_id, question_key, question_text, kind, options, reason, first_seen_at) VALUES ('u2','years-js','How many years of JavaScript do you have?','text','','no fact', ?)", (_iso(now, hours=5),))
    conn.execute("INSERT INTO site_trust(site_key, approved, confirmed_submissions, updated_at) VALUES ('greenhouse', 1, 2, ?), ('linkedin', 0, 1, ?)", (_iso(now, days=2), _iso(now, days=1)))
    conn.execute("INSERT INTO site_clean_runs(site_key, job_id, run_at) VALUES ('lever','n0',?), ('lever','n1',?), ('agent','a2',?)", (_iso(now, days=1), _iso(now, hours=1), _iso(now, hours=31)))
    conn.execute("INSERT INTO daemon_lock(id, holder_pid, holder_host, acquired_at, heartbeat_at) VALUES (1,?,?,?,?)", (os.getpid(), socket.gethostname(), _iso(now, hours=6, minutes=41), _iso(now, seconds=4)))
    conn.commit()
    conn.close()

    def folder(job_id: str) -> Path:
        p = root / "applications" / job_id / "trace"
        p.mkdir(parents=True, exist_ok=True)
        return p.parent

    live = folder("n1")
    steps = [
        {"step": 1, "page": "job_page", "decision": "click", "target": "Apply for this job", "why": "the one clear way forward", "source": "policy"},
        {"step": 2, "page": "form", "decision": "fill", "target": None, "why": "answer the visible questions and attach files", "source": "policy"},
        {"step": 3, "page": "form", "decision": "click", "target": "Next", "why": "continue to the next step", "source": "policy"},
        {"step": 4, "page": "unknown", "decision": "click", "target": "Let's go!", "why": "the model chose this in every menu order", "source": "llm"},
    ]
    (live / "trace" / "steps.jsonl").write_text("\n".join(json.dumps(s) for s in steps) + "\n", encoding="utf-8")
    (live / "fit.json").write_text(json.dumps({"verdict": "apply", "score": 0.84, "model": "qwen2.5:7b", "summary": "Python, PostgreSQL and Docker match; seniority matches."}), encoding="utf-8")
    done = folder("g1")
    (done / "trace" / "steps.jsonl").write_text("\n".join(json.dumps(s) for s in steps[:3] + [{"step": 4, "page": "form", "decision": "ready", "target": "Submit application", "why": "everything required is filled in", "source": "policy"}]) + "\n", encoding="utf-8")
    (done / "fit.json").write_text(json.dumps({"verdict": "apply", "score": 0.88, "model": "qwen2.5:7b", "summary": "Strong match on stack and seniority."}), encoding="utf-8")
    disputed = folder("v1")
    (disputed / "fit.json").write_text(json.dumps({
        "verdict": "skip", "score": 0.8, "model": "qwen2.5:7b", "summary": "good match | deal-breaker: residence",
        "deal_breakers": [residence], "vetoes_met": [{"issue": "residence", "quote": "authorized to work in the United States", "why": "you are authorized to work in US"}],
        "review": {"agreement": "disputed", "model": "deepseek-coder-v2-lite-local", "verdict": "apply", "score": 0.85},
    }), encoding="utf-8")
    (done / "answers.json").write_text(json.dumps([{"answered": {"question": "Email", "answer": "x", "source": "profile"}}, {"answered": {"question": "Authorized to work in the US?", "answer": "Yes", "source": "facts"}}, {"answered": {"question": "Salary expectation", "answer": "120000", "source": "facts"}}]), encoding="utf-8")
    return root


def demo_snapshot(root: Path) -> data.Snapshot:
    """Like data.snapshot, but with a made-up answer from Ollama so the models panel has something to show."""
    soon = datetime.now(timezone.utc) + timedelta(minutes=4)
    return data.snapshot(root, ollama=lambda endpoint: [data.LoadedModel("qwen2.5-coder-7b-local", 4.8, 4.8, soon)])
