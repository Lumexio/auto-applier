"""A made-up project to look at the web interface (``python -m gui --demo``) and to test it. Fictional people and companies, a
temporary folder; nothing here touches the real project. Standard library only."""

from __future__ import annotations

import json
import struct
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from state import StateStore
from tui import demo as tui_demo


def _png(width: int = 640, height: int = 400, tint: int = 0) -> bytes:
    """A small picture standing in for a form's screenshot: a header bar, label lines and input boxes."""
    ink, border, white = (90, 100, 115), (200, 206, 214), (255, 255, 255)

    def pixel(x: int, y: int) -> tuple[int, int, int]:
        if y < 46:
            return (31 + tint, 85, 209)
        for top, bottom in ((90, 126), (170, 206)):                       # two input boxes
            if top <= y < bottom and 40 <= x < width - 40:
                return border if y in (top, bottom - 1) or x in (40, width - 41) else white
        if (70 <= y < 80 and 40 <= x < 260) or (150 <= y < 160 and 40 <= x < 300):       # two labels
            return ink
        return (245, 247, 250)

    raw = b"".join(b"\x00" + b"".join(bytes(pixel(x, y)) for x in range(width)) for y in range(height))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def build(dest: Path | None = None) -> Path:
    """The dashboard's demo project plus everything that needs a person: drafted answers, a site on probation, jobs to do by hand."""
    root = tui_demo.build(dest)
    now = datetime.now(timezone.utc)
    iso = lambda **delta: (now - timedelta(**delta)).isoformat()      # noqa: E731

    store = StateStore(root / "state.db")
    try:
        def add_job(job_id: str, title: str, company: str, site: str, status: str, ago_min: int, code: str | None, message: str | None) -> None:
            when = now - timedelta(minutes=ago_min)
            store.conn.execute(
                "INSERT OR REPLACE INTO jobs(id, run_id, title, company, company_norm, location, apply_url, apply_url_canon, source, score, status, created_at, updated_at, description) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, 96, title, company, company.lower(), "Remote", f"https://{site}.example.test/{job_id}", f"https://{site}.example.test/{job_id}", "demo", 0.8,
                 "pending", when.isoformat(), when.isoformat(), f"{title} at {company}. You will design and run backend services in Python and PostgreSQL."))
            store.conn.execute(
                "INSERT OR REPLACE INTO applications(job_id, run_id, status, mode, ats, started_at, finished_at, error_code, error_message, attempts) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (job_id, 96, status, "autonomous", site, (when - timedelta(minutes=1)).isoformat(), None if status == "needs_review" else when.isoformat(), code, message, 1))

        # questions the forms asked: one with a drafted, checked answer; one drafted but not checked and named for the company; plain ones
        add_job("q1", "Backend Engineer", "Hooli", "greenhouse", "needs_review", 30, "needs_answers", "2 required question(s) need your answer")
        add_job("q2", "Data Engineer", "Initech", "greenhouse", "needs_review", 31, "needs_answers", "2 required question(s) need your answer")
        add_job("q3", "Platform Engineer", "Globex", "lever", "needs_review", 33, "needs_answers", "1 required question(s) need your answer")
        pend = [
            ("q1", "what do you use to store tracking data", "What do you use to store tracking data?", "textarea", "", "no fact covers it",
             "I store shipment events in PostgreSQL and keep hot tracking lookups in Redis.", "resumes/alex-backend.md: “Built an event-driven order tracking service in Python and PostgreSQL”", 1,
             "checked by deepseek-coder-v2-lite-local", ""),
            ("q2", "what do you use to store tracking data", "What do you use to store tracking data?", "textarea", "", "no fact covers it",
             "I store shipment events in PostgreSQL and keep hot tracking lookups in Redis.", "resumes/alex-backend.md: “Built an event-driven order tracking service in Python and PostgreSQL”", 1,
             "checked by deepseek-coder-v2-lite-local", ""),
            ("q1", "why do you want to join hooli", "Why do you want to join Hooli?", "textarea", "", "an essay: needs your own words",
             "I want to join Hooli to keep building reliable backend systems like the order tracking service I built at Northwind.", "resumes/alex-backend.md: “Mentored four junior engineers through code review and pairing”", 0,
             "no second model confirmed it", "hooli"),
            ("q3", "which of these best describes your english level", "Which of these best describes your English level?", "select", "Basic|Intermediate|Professional|Native", "needs a choice", None, None, 0, None, ""),
            ("q3", "are you willing to relocate", "Are you willing to relocate?", "radio", "Yes|No", "no fact covers it", None, None, 0, None, ""),
        ]
        for job_id, key, text, kind, options, reason, proposal, basis, verified, note, scope in pend:
            store.conn.execute(
                "INSERT INTO pending_questions(job_id, question_key, question_text, kind, options, reason, first_seen_at, proposal, proposal_basis, proposal_verified, proposal_note, proposal_scope) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (job_id, key, text, kind, options, reason, iso(minutes=30), proposal, basis, verified, note, scope))

        # a site on probation with two forms filled in and held back
        add_job("p1", "Senior Python Developer", "Stark Labs", "ashby", "needs_review", 90, "probation", "held on probation: ashby has 2/3 clean runs")
        add_job("p2", "Backend Engineer", "Pied Piper", "ashby", "needs_review", 80, "probation", "held on probation: ashby has 2/3 clean runs")
        store.conn.execute("INSERT OR IGNORE INTO site_clean_runs(site_key, job_id, run_at) VALUES ('ashby','p1',?), ('ashby','p2',?)", (iso(minutes=90), iso(minutes=80)))

        # jobs a person has to finish
        add_job("e1", "Backend Developer", "Sigma Beauty", "linkedin", "needs_review", 200, None, "no automated submit: external_application_required")
        add_job("e2", "API Engineer", "Ediphai", "linkedin", "needs_review", 210, None, "no automated submit: external_application_required")
        add_job("c1x", "Cloud Engineer", "Delinea", "linkedin", "needs_review", 400, None, "interrupted: outcome unknown, verify manually")
        add_job("x1", "Python Developer", "Contoso Labs", "linkedin", "unconfirmed", 3000, None, "neither a confirmation nor an error appeared")
        add_job("x2", "Backend Engineer", "Adatum", "linkedin", "unconfirmed", 3100, None, "backfilled from daemon.log: 3 earlier attempt(s) ended 'unconfirmed'")
        add_job("f2", "Software Engineer", "Tailspin", "linkedin", "failed", 500, None, "linkedin: no apply button found on job posting")
        add_job("f3", "Platform Engineer", "Wingtip", "custom", "failed", 520, None, "custom: no submit button found")

        store.put_answer("are you legally authorized to work in mexico", "Are you legally authorized to work in Mexico?", "Yes", "user")
        store.put_answer("why do you want to join northwind@northwind", "Why do you want to join Northwind?  [northwind only]", "I like logistics problems.", "user")
        store.conn.commit()
    finally:
        store.close()

    def folder(job_id: str) -> Path:
        path = root / "applications" / job_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    for job_id, tint in (("p1", 0), ("p2", 40)):
        d = folder(job_id)
        (d / "form_snapshot.png").write_bytes(_png(tint=tint))
        (d / "probation.json").write_text(json.dumps({"site": "ashby", "job": job_id, "company": "demo", "clean_runs": 2, "needed": 3,
                                                       "review": ["form_snapshot.png", "answers.json", "fit.json"], "to_trust": "python sites.py approve ashby"}), encoding="utf-8")
        (d / "answers.json").write_text(json.dumps([
            {"answered": {"question": "Full name", "answer": "Alex Rivera", "source": "profile", "basis": ""}},
            {"answered": {"question": "Are you authorized to work in Mexico?", "answer": "Yes", "source": "facts", "basis": ""}},
            {"unanswered": {"label": "Anything else?", "reason": "optional"}},
        ]), encoding="utf-8")
        (d / "fit.json").write_text(json.dumps({"verdict": "apply", "score": 0.86, "model": "qwen2.5:7b", "summary": "Python and PostgreSQL match."}), encoding="utf-8")
    return root
