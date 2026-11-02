"""What the web interface shows and what its buttons do. No HTTP in here: plain functions over the project folder, so the
same code is tested directly and served by ``gui.server``.

Reading never writes (the database is opened read-only, so a page refresh cannot get in the daemon's way); every action opens
the database briefly, does one thing a command-line tool (``answers.py``, ``fit_review.py``, ``sites.py``) already does, and
closes it. Nothing here submits an application or contacts anyone: the buttons record YOUR decisions, and the daemon acts on
them in its next cycle.
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterator

import fit_review
from form_questions import WHOLE_LIST_MAX, normalize_key
from sites import DEFAULT_PROBATION
from state import StateStore
from tui import data

MAX_ANSWER_CHARS = 4000
SHOWN_STEPS = 80
SHOWN_DESCRIPTION = 3500
# The only files of a job's folder the browser may ask for: the form's picture, the picture taken when it went wrong, and the
# agent's per-step pictures.
_FILE_NAME = re.compile(r"^(form_snapshot\.png|failure\.png|trace/step_[0-9A-Za-z_-]{1,40}\.png)\Z")        # \Z: "$" would also accept a trailing newline


class ActionError(Exception):
    """The request cannot be done (unknown job, empty answer, ...). The message is shown to the person who pressed the button."""


def safe_url(url: Any) -> str:
    """Postings come from the web: only plain http(s) links are ever offered as links (never ``javascript:``)."""
    url = str(url or "").strip()
    return url if re.match(r"^https?://[^\s]+$", url, re.IGNORECASE) else ""


def _clip(text: Any, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class Desk:
    """One project folder: its database, its application folders, its configuration."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        cfg = self.config()
        self.db_path = self.root / str((cfg.get("daemon") or {}).get("state_db", "state.db"))
        if self.db_path.is_file():
            self._migrate()

    def config(self) -> dict[str, Any]:
        return data.load_config(self.root)

    def _migrate(self) -> None:
        """Bring an older database up to date once, so the read-only queries below never meet a missing column."""
        try:
            StateStore(self.db_path).close()
        except sqlite3.Error:
            pass

    def clean_runs_needed(self) -> int:
        return int({**DEFAULT_PROBATION, **((self.config().get("agent") or {}).get("probation") or {})}["clean_runs_to_trust"])

    def folder(self, job_id: str) -> Path:
        return self.root / "applications" / data.safe_dirname(str(job_id))

    # ------------------------------------------------------------------ connections

    @contextlib.contextmanager
    def reader(self) -> Iterator[StateStore | None]:
        """A read-only view of the database (None before the daemon has ever run here)."""
        if not self.db_path.is_file():
            yield None
            return
        conn = sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True, timeout=3)
        conn.execute("PRAGMA query_only = ON")
        store = StateStore.from_connection(conn)
        try:
            yield store
        finally:
            conn.close()

    @contextlib.contextmanager
    def writer(self) -> Iterator[StateStore]:
        store = StateStore(self.db_path)
        try:
            yield store
        finally:
            store.close()

    # ------------------------------------------------------------------ what needs you

    def overview(self) -> dict[str, Any]:
        """The header: is the daemon working, how much it has done today, what it is doing right now, the latest outcomes."""
        try:
            snap = data.snapshot(self.root, with_ollama=False)
        except Exception as e:                                           # a half-written file must not blank the page
            return {"db": self.db_path.is_file(), "error": f"{type(e).__name__}: {e}"}
        d = snap.daemon
        now = snap.now
        return {
            "db": snap.db_ok,
            "daemon": {"state": d.state, "pid": d.pid, "activity": _clip(d.activity, 160)},
            "mode": snap.mode,
            "today": snap.today,
            "cap": {"used": snap.cap_used, "limit": snap.cap_limit},
            "all_time": snap.all_time,
            "now": ({"job_id": now.app.job_id, "title": now.app.title, "company": now.app.company, "live": now.live}
                    if now else None),
            "recent": [self._app_summary(a) for a in snap.recent[:40]],
            "log": [_clip(line, 240) for line in snap.log[-30:]],
        }

    @staticmethod
    def _app_summary(a: Any) -> dict[str, Any]:
        return {"job_id": a.job_id, "title": a.title, "company": a.company, "status": a.status, "site": a.site,
                "when": a.when.isoformat() if a.when else "", "error_code": a.error_code, "detail": _clip(a.detail, 200)}

    def inbox(self) -> dict[str, Any]:
        """Everything that is waiting for a person, in the order it is worth looking at."""
        with self.reader() as s:
            if s is None:
                return {"db": False, "questions": [], "fit": [], "probation": [], "apply_by_hand": [], "verify": [], "other": [],
                        "unconfirmed": [], "failed": [], "counts": {}}
            questions = self._questions(s)
            fit = self._fit(s)
            probation = self._probation(s)
            parked = self._parked(s)
            unconfirmed = self._by_status(s, "unconfirmed")
            failed = self._by_status(s, "failed")
        by_kind = {k: [p for p in parked if p["kind"] == k] for k in ("apply", "verify", "other")}
        disputed = [f for f in fit if f["where"] == "disputed"]
        counts = {
            "questions": len(questions), "fit": len(disputed), "probation": sum(len(p["jobs"]) for p in probation),
            "apply_by_hand": len(by_kind["apply"]), "verify": len(by_kind["verify"]), "other": len(by_kind["other"]),
            "unconfirmed": len(unconfirmed), "failed": len(failed),
        }
        counts["needs_you"] = sum(counts[k] for k in ("questions", "fit", "apply_by_hand", "verify", "other")) + len(probation)
        return {"db": True, "questions": questions, "fit": fit, "probation": probation, "apply_by_hand": by_kind["apply"],
                "verify": by_kind["verify"], "other": by_kind["other"], "unconfirmed": unconfirmed, "failed": failed, "counts": counts}

    @staticmethod
    def _job(r: Any) -> dict[str, Any]:
        return {"job_id": r["job_id"], "title": r["title"] or "(unknown)", "company": r["company"] or "", "url": safe_url(r["url"])}

    def _questions(self, s: StateStore) -> list[dict[str, Any]]:
        out = []
        for r in s.list_pending():
            jobs = s.jobs_waiting_on(r["question_key"], r["scope"] or "")
            out.append({
                "key": r["question_key"], "scope": r["scope"] or "", "text": r["question_text"], "kind": r["kind"] or "text",
                "options": [o for o in (r["options"] or "").split("|") if o], "reason": r["reason"] or "",
                "jobs": [{"job_id": j["job_id"], "title": j["title"] or "(unknown)", "company": j["company"], "url": safe_url(j["url"])} for j in jobs],
                "proposal": r["proposal"] or "", "proposal_basis": r["proposal_basis"] or "",
                "proposal_verified": bool(r["proposal_verified"]), "proposal_note": r["proposal_note"] or "",
            })
        return out

    def _fit(self, s: StateStore) -> list[dict[str, Any]]:
        order = {"disputed": 0, "waiting": 1}
        out = []
        for r in s.list_fit_reviews():
            if r.get("override"):
                continue                                                  # you already decided
            payload = fit_review._payload(r)
            review = payload.get("review") or {}
            where = ("disputed" if r["status"] == "needs_review"
                     else "waiting" if (r.get("error_message") or "").startswith("fit veto not confirmed") else "vetoed")
            out.append({
                "job_id": r["job_id"], "title": r["title"] or "(unknown)", "company": r["company"] or "", "url": safe_url(r["apply_url"]),
                "where": where, "stands": fit_review.describe(r), "score": r.get("score"), "model": r.get("model") or "",
                "summary": _clip(payload.get("summary"), 400),
                "deal_breakers": [{"issue": _clip(d.get("issue"), 200), "quote": _clip(d.get("quote"), 300)} for d in (payload.get("deal_breakers") or [])[:4]],
                "review": ({"model": review.get("model") or "", "verdict": review.get("verdict") or "", "score": review.get("score"),
                            "summary": _clip(review.get("summary"), 400),
                            "deal_breakers": [{"issue": _clip(d.get("issue"), 200), "quote": _clip(d.get("quote"), 300)} for d in (review.get("deal_breakers") or [])[:4]]}
                           if review else None),
            })
        out.sort(key=lambda f: order.get(f["where"], 2))
        return out

    _APP_COLUMNS = """a.job_id, a.status, a.error_code, a.error_message, a.ats, a.attempts, a.started_at, a.finished_at, a.submitted_at,
                      j.title, j.company, COALESCE(j.apply_url, j.url) AS url"""

    def _parked(self, s: StateStore) -> list[dict[str, Any]]:
        rows = s.conn.execute(
            f"""SELECT {self._APP_COLUMNS} FROM applications a LEFT JOIN jobs j ON j.id = a.job_id
                 WHERE a.status = 'needs_review' AND COALESCE(a.error_code, '') NOT IN ('needs_answers', 'fit_disagreement', 'probation')
                 ORDER BY COALESCE(a.finished_at, a.started_at) DESC"""
        ).fetchall()
        out = []
        for r in rows:
            detail = " ".join((r["error_message"] or "").split())
            kind = "apply" if detail.startswith("no automated submit") else "verify" if detail.startswith("interrupted") else "other"
            out.append({**self._job(r), "kind": kind, "detail": _clip(detail, 300), "error_code": r["error_code"] or "",
                        "site": r["ats"] or "", "when": r["finished_at"] or r["started_at"] or ""})
        return out

    def _by_status(self, s: StateStore, status: str, limit: int = 300) -> list[dict[str, Any]]:
        rows = s.conn.execute(
            f"""SELECT {self._APP_COLUMNS} FROM applications a LEFT JOIN jobs j ON j.id = a.job_id
                 WHERE a.status = ? ORDER BY COALESCE(a.finished_at, a.started_at) DESC LIMIT ?""",
            (status, limit),
        ).fetchall()
        return [{**self._job(r), "detail": _clip(r["error_message"], 300), "error_code": r["error_code"] or "", "site": r["ats"] or "",
                 "attempts": r["attempts"] or 0, "when": r["finished_at"] or r["started_at"] or ""} for r in rows]

    def _probation(self, s: StateStore) -> list[dict[str, Any]]:
        """Sites held on probation, each with the jobs that were filled in and parked there for you to look at."""
        needed = self.clean_runs_needed()
        clean = {r["site_key"]: r["clean_runs"] for r in s.list_sites()}
        site_of = {r["job_id"]: r["site_key"] for r in s.conn.execute("SELECT job_id, site_key FROM site_clean_runs")}
        rows = s.conn.execute(
            f"""SELECT {self._APP_COLUMNS} FROM applications a LEFT JOIN jobs j ON j.id = a.job_id
                 WHERE a.status = 'needs_review' AND a.error_code = 'probation'
                 ORDER BY COALESCE(a.finished_at, a.started_at) DESC"""
        ).fetchall()
        sites: dict[str, dict[str, Any]] = {}
        for r in rows:
            folder = self.folder(r["job_id"])
            bundle = _read_json(folder / "probation.json") or {}
            site = site_of.get(r["job_id"]) or str(bundle.get("site") or r["ats"] or "unknown")
            answers = _read_json(folder / "answers.json")
            answers = answers if isinstance(answers, list) else []
            fit = _read_json(folder / "fit.json") or {}
            entry = sites.setdefault(site, {"site": site, "trusted": s.site_is_trusted(site, needed), "clean_runs": clean.get(site, 0),
                                            "needed": needed, "jobs": []})
            entry["jobs"].append({
                **self._job(r), "when": r["finished_at"] or r["started_at"] or "",
                "snapshot": (folder / "form_snapshot.png").is_file(),
                "answered": sum(1 for a in answers if isinstance(a, dict) and "answered" in a),
                "unanswered": sum(1 for a in answers if isinstance(a, dict) and "unanswered" in a),
                "fit_score": fit.get("score") if isinstance(fit, dict) else None,
                "fit_summary": _clip(fit.get("summary"), 200) if isinstance(fit, dict) else "",
            })
        return sorted(sites.values(), key=lambda e: (-len(e["jobs"]), e["site"]))

    def sites(self) -> list[dict[str, Any]]:
        needed = self.clean_runs_needed()
        with self.reader() as s:
            if s is None:
                return []
            return [{"site": r["site_key"], "trusted": s.site_is_trusted(r["site_key"], needed), "approved": bool(r["approved"]),
                     "clean_runs": r["clean_runs"], "needed": needed, "submitted": r["confirmed_submissions"], "parked": r["parked"]}
                    for r in s.list_sites()]

    def answers(self) -> list[dict[str, Any]]:
        with self.reader() as s:
            if s is None:
                return []
            return [{"key": r["question_key"], "question": r["question_text"], "answer": r["answer"], "source": r["source"],
                     "updated": r["updated_at"]} for r in s.list_answers()]

    # ------------------------------------------------------------------ one job in detail

    def job(self, job_id: str) -> dict[str, Any]:
        detail = data.job_detail(self.root, str(job_id))
        folder = self.folder(job_id)
        description = ""
        with self.reader() as s:
            if s is not None:
                row = s.conn.execute("SELECT description FROM jobs WHERE id = ?", (job_id,)).fetchone()
                description = (row["description"] or "") if row else ""
        app = detail.get("app") or None
        job = detail.get("job") or None
        if not app and not job:
            raise ActionError("No such job.")
        if job:
            job = {**job, "apply_url": safe_url(job.get("apply_url"))}
        shots = [name for name in ("failure.png", "form_snapshot.png") if (folder / name).is_file()]
        shots += sorted(f"trace/{Path(p).name}" for p in (detail.get("shots") or []))
        return {
            "job_id": job_id, "app": app, "job": job, "fit": detail.get("fit"), "probation": detail.get("probation"),
            "answers": detail.get("answers") or [], "steps": (detail.get("steps") or [])[-SHOWN_STEPS:],
            "shots": [n for n in shots if _FILE_NAME.match(n)], "description": description[:SHOWN_DESCRIPTION],
            "failure": self._failure(folder),
        }

    @staticmethod
    def _failure(folder: Path) -> dict[str, str] | None:
        """What the page showed when the last attempt went wrong (written by stage 3), as short plain strings."""
        saved = _read_json(folder / "failure.json")
        if not isinstance(saved, dict):
            return None
        return {key: _clip(saved.get(key), limit) for key, limit in
                (("kind", 20), ("error", 300), ("url", 300), ("page_type", 30), ("title", 120), ("text", 1200), ("saved_at", 30))}

    def file(self, job_id: str, name: str) -> Path | None:
        """The picture ``name`` of a job's folder, or None. Only the two kinds of file in ``_FILE_NAME``, only inside the job's folder."""
        if not _FILE_NAME.match(name or ""):
            return None
        folder = self.folder(job_id).resolve()
        path = (folder / name).resolve()
        if folder not in path.parents or not path.is_file():
            return None
        return path

    # ------------------------------------------------------------------ what the buttons do

    def act(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"_do_{action}", None) if re.fullmatch(r"[a-z_]{1,40}", action or "") else None
        if handler is None:
            raise ActionError("Unknown action.")
        if not self.db_path.is_file():
            raise ActionError("There is no database yet: the daemon has never run in this folder.")
        return handler(args)

    @staticmethod
    def _text(args: dict[str, Any], name: str, limit: int = MAX_ANSWER_CHARS) -> str:
        value = args.get(name)
        if not isinstance(value, str):
            return ""
        value = value.strip()
        if len(value) > limit:
            raise ActionError(f"That is longer than {limit} characters.")
        return value

    @staticmethod
    def _as_option(pending: dict[str, Any], text: str) -> str:
        """For a question with a fixed list of choices, the answer is the form's own spelling of one of them. A dropdown's list counts as
        fixed when it is short enough to be the whole list (a long one is probably only what a search shows before you type)."""
        options = [o for o in (pending["options"] or "").split("|") if o]
        if not options or (pending["kind"] == "combobox" and len(options) > WHOLE_LIST_MAX):
            return text
        match = next((o for o in options if o.casefold() == text.casefold()), None)
        if match is None:
            raise ActionError("Pick one of the options the form offers: " + " | ".join(options))
        return match

    @staticmethod
    def _requeue_ready(s: StateStore) -> int:
        return sum(1 for job_id in s.jobs_ready_to_requeue() if s.requeue(job_id))

    def _do_answer(self, args: dict[str, Any]) -> dict[str, Any]:
        """Save your answer to a question the forms asked (or accept the drafted one) and queue the jobs that were waiting on it."""
        key, scope, text = self._text(args, "key", 500), self._text(args, "scope", 200), self._text(args, "text")
        if not text:
            raise ActionError("Write the answer first.")
        with self.writer() as s:
            row = next((r for r in s.list_pending() if r["question_key"] == key and (r["scope"] or "") == scope), None)
            if row is None:
                raise ActionError("That question is no longer waiting (someone answered it already). Refresh the page.")
            text = self._as_option(row, text)
            bank_key = key + (f"@{scope}" if scope else "")
            s.put_answer(bank_key, row["question_text"] + (f"  [{scope} only]" if scope else ""), text, "user")
            s.resolve_pending(key, scope or None)
            queued = self._requeue_ready(s)
        return {"ok": True, "queued": queued, "message": "Saved." + (f" {queued} job(s) will be tried again." if queued else "")}

    def _do_skip_question(self, args: dict[str, Any]) -> dict[str, Any]:
        """You do not want to answer this: skip every job it is holding up, for good."""
        key, scope = self._text(args, "key", 500), self._text(args, "scope", 200)
        with self.writer() as s:
            jobs = s.jobs_waiting_on(key, scope)
            if not jobs:
                raise ActionError("That question is no longer waiting. Refresh the page.")
            for j in jobs:
                s.skip_by_hand(j["job_id"], f"you chose not to answer: {_clip(key, 120)}")
                s.drop_pending_for_job(j["job_id"])
        return {"ok": True, "message": f"Skipped {len(jobs)} job(s) for good."}

    def _do_save_answer(self, args: dict[str, Any]) -> dict[str, Any]:
        """Change the wording of an answer you saved earlier (or add one for a question nobody has asked yet)."""
        key, text = self._text(args, "key", 500), self._text(args, "text")
        question = self._text(args, "question", 500)
        if not text:
            raise ActionError("Write the answer first.")
        with self.writer() as s:
            old = next((r for r in s.list_answers() if r["question_key"] == key), None)
            if old is None:
                key = normalize_key(question)
                if not key:
                    raise ActionError("Write the question the way a form would ask it.")
                old = {"question_text": question}
            waiting = next((r for r in s.list_pending() if r["question_key"] == key.split("@", 1)[0]), None)
            if waiting is not None:
                text = self._as_option(waiting, text)
            s.put_answer(key, old["question_text"], text, "user")
            s.resolve_pending(key.split("@", 1)[0], (key.split("@", 1)[1] if "@" in key else None))
            queued = self._requeue_ready(s)
        return {"ok": True, "queued": queued, "message": "Saved." + (f" {queued} job(s) will be tried again." if queued else "")}

    def _do_forget_answer(self, args: dict[str, Any]) -> dict[str, Any]:
        with self.writer() as s:
            gone = s.delete_answer(self._text(args, "key", 500))
        if not gone:
            raise ActionError("That answer is not saved.")
        return {"ok": True, "message": "Forgotten. The next application that asks it will be parked again."}

    def _do_fit(self, args: dict[str, Any]) -> dict[str, Any]:
        """Overrule the fit models: ``apply`` queues the posting again (no model can veto it now), ``skip`` ends it for good."""
        job_id, decision = self._text(args, "job_id", 200), args.get("decision")
        if decision not in ("apply", "skip"):
            raise ActionError("Choose apply or skip.")
        with self.writer() as s:
            row = next((r for r in s.list_fit_reviews(job_id) if r["job_id"] == job_id), None)
            if row is None:
                raise ActionError("That posting is not waiting for a fit decision (refresh the page).")
            if decision == "apply":
                s.set_fit_override(job_id, "apply", "approved in the web interface")
                s.requeue_after_fit_decision(job_id)
                return {"ok": True, "message": f"{row['title']} @ {row['company']} is queued again; no model can veto it now."}
            s.set_fit_override(job_id, "skip", "rejected in the web interface")
            s.finalize_fit_skip(job_id, "you rejected this posting (web interface)")
            return {"ok": True, "message": f"{row['title']} @ {row['company']} is skipped for good."}

    def _do_trust_site(self, args: dict[str, Any]) -> dict[str, Any]:
        """Trust a kind of site: from now on its applications are submitted, and the jobs held back are released."""
        site = self._text(args, "site", 120).lower()
        if not site:
            raise ActionError("Which site?")
        with self.writer() as s:
            s.approve_site(site)
            released = s.release_probation(site)
        return {"ok": True, "released": released, "message": f"'{site}' is trusted. {released} job(s) will be applied to in the next cycle."}

    def _do_distrust_site(self, args: dict[str, Any]) -> dict[str, Any]:
        site = self._text(args, "site", 120).lower()
        if not site:
            raise ActionError("Which site?")
        with self.writer() as s:
            s.revoke_site(site)
        return {"ok": True, "message": f"'{site}' is back on probation: filled in and reviewed, not submitted."}

    def _do_job(self, args: dict[str, Any]) -> dict[str, Any]:
        """What happened to a parked, unconfirmed or failed posting: you applied yourself, try it again, or never apply."""
        job_id, decision = self._text(args, "job_id", 200), args.get("decision")
        if decision not in ("applied", "retry", "skip"):
            raise ActionError("Choose applied, retry or skip.")
        with self.writer() as s:
            done = {"applied": s.mark_applied_by_hand, "retry": s.retry_by_hand, "skip": s.skip_by_hand}[decision](job_id)
        if not done:
            raise ActionError("That application is not waiting for a decision any more. Refresh the page.")
        return {"ok": True, "message": {"applied": "Recorded as applied.", "retry": "Queued again: if the posting is still listed, the next cycle tries it.",
                                        "skip": "Skipped for good."}[decision]}
