"""Everything the dashboard shows, gathered from the files the daemon already writes. Read-only, standard library only.

Nothing here may write to the project or signal a process: the database is opened ``mode=ro``, and "is the daemon alive?"
is answered by asking Windows about the pid (never ``os.kill``: on Windows that terminates the process).
"""

from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

OLLAMA_DEFAULT = "http://127.0.0.1:11434"
FRESH_HEARTBEAT_S = 90          # the daemon beats every <=10 s while waiting, and once per job while working
STALE_IN_PROGRESS_S = 20 * 60   # an application that has been "in progress" longer than this is a leftover, not live work
STATUSES = ("submitted", "unconfirmed", "needs_review", "skipped", "failed", "in_progress", "duplicate")


# --------------------------------------------------------------------------- small helpers

def safe_dirname(name: str) -> str:
    """The folder name stage 3 gives a job's files (kept identical to stage3_apply.run._safe_dirname)."""
    return re.sub(r"[^\w.-]+", "_", name).strip("._")[:80] or "job"


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def pid_alive(pid: int | None) -> bool | None:
    """True/False when we can tell, None when we cannot. Never sends a signal."""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE
            handle = k32.OpenProcess(0x1000, False, pid)             # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return ctypes.get_last_error() == 5                  # access denied: it exists, we may not look inside
            try:
                code = wintypes.DWORD()
                if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return None
                return code.value == 259                             # STILL_ACTIVE
            finally:
                k32.CloseHandle(handle)
        except Exception:
            return None
    proc = Path(f"/proc/{pid}")
    return proc.exists() if Path("/proc").is_dir() else None


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=1.5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _rows(conn: sqlite3.Connection | None, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    if conn is None:
        return []
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def tail_lines(path: Path, n: int = 40, max_bytes: int = 48_000) -> list[str]:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    return [ln.rstrip() for ln in lines if ln.strip()][-n:]


def load_config(root: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError:
        return {}
    try:
        return yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, yaml.YAMLError):
        return {}


# --------------------------------------------------------------------------- the snapshot

@dataclass
class DaemonInfo:
    state: str = "unknown"                 # running | stalled | stopped | unknown
    pid: int | None = None
    since: datetime | None = None
    heartbeat_age: float | None = None
    last_log_age: float | None = None
    next_cycle: datetime | None = None
    cycle_running: bool = False
    stage: str = ""
    activity: str = ""                     # the last line the daemon printed


@dataclass
class LoadedModel:
    name: str
    vram_gb: float
    total_gb: float
    expires: datetime | None


@dataclass
class RoleInfo:
    role: str
    model: str
    tasks: list[str]
    loaded: bool


@dataclass
class AppRow:
    job_id: str
    title: str
    company: str
    status: str
    site: str
    when: datetime | None
    error_code: str
    detail: str
    attempts: int = 0


@dataclass
class VetoRow:
    job_id: str
    title: str
    company: str
    where: str                  # disputed | waiting | confirmed | single | low | rejected
    score: float | None
    reason: str
    reviewer: str
    when: datetime | None


@dataclass
class NowInfo:
    app: AppRow
    started: datetime | None
    age_s: float
    live: bool
    steps: list[dict[str, Any]] = field(default_factory=list)
    fit: dict[str, Any] | None = None


@dataclass
class Snapshot:
    taken_at: datetime
    root: Path
    db_ok: bool = False
    daemon: DaemonInfo = field(default_factory=DaemonInfo)
    today: dict[str, int] = field(default_factory=dict)
    all_time: dict[str, int] = field(default_factory=dict)
    week: list[tuple[str, int, int]] = field(default_factory=list)       # (day, submitted, everything else)
    cap_used: int = 0
    cap_limit: int = 25
    run: dict[str, Any] | None = None
    funnel: dict[str, int] = field(default_factory=dict)
    stage3: dict[str, int] = field(default_factory=dict)                 # outcomes of the latest run's applications
    recent: list[AppRow] = field(default_factory=list)
    now: NowInfo | None = None
    pending: list[dict[str, Any]] = field(default_factory=list)
    vetoes: list[VetoRow] = field(default_factory=list)                  # postings skipped for fit, or parked because the models disagree
    sites: list[dict[str, Any]] = field(default_factory=list)
    models: list[LoadedModel] | None = None                              # None = Ollama did not answer
    roles: list[RoleInfo] = field(default_factory=list)
    base_model: str = ""
    log: list[str] = field(default_factory=list)
    mode: str = ""


def _app_row(r: sqlite3.Row) -> AppRow:
    when = parse_ts(r["submitted_at"] or r["finished_at"] or r["started_at"])
    detail = (r["error_message"] or "").strip()
    return AppRow(
        job_id=r["job_id"], title=r["title"] or "(unknown)", company=r["company"] or "", status=r["status"],
        site=r["ats"] or "", when=when, error_code=r["error_code"] or "", detail=detail, attempts=r["attempts"] or 0,
    )


_APP_SQL = """
SELECT a.job_id, a.status, a.ats, a.error_code, a.error_message, a.started_at, a.finished_at, a.submitted_at, a.attempts,
       j.title, j.company
  FROM applications a LEFT JOIN jobs j ON j.id = a.job_id
"""


def ollama_models(endpoint: str = OLLAMA_DEFAULT, timeout: float = 1.5) -> list[LoadedModel] | None:
    base = endpoint.rstrip("/")
    base = base[:-3] if base.endswith("/v1") else base
    try:
        with urllib.request.urlopen(f"{base}/api/ps", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    out = []
    for m in data.get("models") or []:
        out.append(LoadedModel(
            name=str(m.get("name") or m.get("model") or "?"), vram_gb=float(m.get("size_vram") or 0) / 1e9,
            total_gb=float(m.get("size") or 0) / 1e9, expires=parse_ts(m.get("expires_at")),
        ))
    return out


def _roles(cfg: dict[str, Any], loaded: list[LoadedModel] | None) -> list[RoleInfo]:
    models = cfg.get("models") or {}
    roles = models.get("roles") or {}
    tasks = models.get("tasks") or {}
    names = {m.name.split(":")[0] if m.name.endswith(":latest") else m.name for m in (loaded or [])}
    names |= {m.name for m in (loaded or [])}
    out = []
    for role, spec in roles.items():
        if not isinstance(spec, dict):
            continue
        used = [t for t, v in tasks.items() if role in (v if isinstance(v, list) else [v])]
        model = str(spec.get("model", ""))
        out.append(RoleInfo(role=role, model=model, tasks=used, loaded=model in names or f"{model}:latest" in names))
    return out


def snapshot(root: str | Path, now: datetime | None = None, with_ollama: bool = True, ollama: Any = None) -> Snapshot:
    """``ollama``: a replacement for the live Ollama query (``endpoint -> list[LoadedModel] | None``), for the demo and tests."""
    root = Path(root)
    now = now or datetime.now(timezone.utc)
    cfg = load_config(root)
    snap = Snapshot(taken_at=now, root=root)
    snap.cap_limit = int(((cfg.get("stage3") or {}).get("rate_limit") or {}).get("global_per_day", 25))
    snap.mode = str((cfg.get("stage3") or {}).get("mode", ""))
    snap.base_model = str((cfg.get("llm") or {}).get("model", ""))
    snap.log = tail_lines(root / (((cfg.get("daemon") or {}).get("log_file")) or "daemon.log"), 40)

    db_path = root / str((cfg.get("daemon") or {}).get("state_db", "state.db"))
    conn: sqlite3.Connection | None = None
    if db_path.is_file():
        try:
            conn = _connect(db_path)
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
            snap.db_ok = True
        except sqlite3.Error:
            conn = None
    try:
        _fill_from_db(snap, conn, cfg, now)
    finally:
        if conn is not None:
            conn.close()

    _fill_daemon(snap, root, cfg, now)
    if with_ollama or ollama is not None:
        endpoint = str((cfg.get("llm") or {}).get("endpoint", OLLAMA_DEFAULT)) or OLLAMA_DEFAULT
        snap.models = (ollama or ollama_models)(endpoint)
    snap.roles = _roles(cfg, snap.models)
    return snap


def _fill_from_db(snap: Snapshot, conn: sqlite3.Connection | None, cfg: dict[str, Any], now: datetime) -> None:
    if conn is None:
        return
    local_midnight = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    day_start = local_midnight.astimezone(timezone.utc).isoformat()
    when = "COALESCE(submitted_at, finished_at, started_at)"

    snap.today = {r["status"]: r["n"] for r in _rows(conn, f"SELECT status, COUNT(*) n FROM applications WHERE {when} >= ? GROUP BY status", (day_start,))}
    snap.all_time = {r["status"]: r["n"] for r in _rows(conn, "SELECT status, COUNT(*) n FROM applications GROUP BY status")}
    since = (now - timedelta(hours=24)).isoformat()
    row = _rows(conn, "SELECT COUNT(*) n FROM rate_events WHERE event_type = 'submit' AND event_at > ?", (since,))
    snap.cap_used = row[0]["n"] if row else 0

    week_start = (local_midnight - timedelta(days=6)).astimezone(timezone.utc).isoformat()
    per_day: dict[str, list[int]] = {}
    for r in _rows(conn, f"SELECT {when} AS ts, status FROM applications WHERE {when} >= ?", (week_start,)):
        ts = parse_ts(r["ts"])
        if ts is None:
            continue
        slot = per_day.setdefault(ts.astimezone().strftime("%m-%d"), [0, 0])
        slot[0 if r["status"] == "submitted" else 1] += 1
    snap.week = [((local_midnight - timedelta(days=d)).strftime("%m-%d"),) + tuple(per_day.get((local_midnight - timedelta(days=d)).strftime("%m-%d"), [0, 0])) for d in range(6, -1, -1)]

    runs = _rows(conn, "SELECT id, started_at, finished_at, status, stage, error_message FROM runs ORDER BY id DESC LIMIT 1")
    if runs:
        run = dict(runs[0])
        snap.run = run
        diag = _rows(conn, "SELECT * FROM run_diagnostics WHERE run_id = ?", (run["id"],))
        if diag:
            d = dict(diag[0])
            d.pop("run_id", None)
            snap.funnel = {k: int(v or 0) for k, v in d.items()}
        snap.stage3 = {r["status"]: r["n"] for r in _rows(conn, "SELECT status, COUNT(*) n FROM applications WHERE run_id = ? GROUP BY status", (run["id"],))}

    snap.recent = [_app_row(r) for r in _rows(conn, _APP_SQL + " ORDER BY COALESCE(a.finished_at, a.started_at) DESC LIMIT 60")]

    live = _rows(conn, _APP_SQL + " WHERE a.status = 'in_progress' ORDER BY a.started_at DESC LIMIT 1")
    if live:
        app = _app_row(live[0])
        started = parse_ts(live[0]["started_at"])
        age = (now - started).total_seconds() if started else 0.0
        folder = snap.root / "applications" / safe_dirname(app.job_id)
        snap.now = NowInfo(app=app, started=started, age_s=age, live=age < STALE_IN_PROGRESS_S,
                           steps=read_trace(folder), fit=_read_json(folder / "fit.json"))

    snap.pending = [dict(r) for r in _rows(conn, """
        SELECT question_key, MIN(question_text) AS question_text, MIN(kind) AS kind, COUNT(DISTINCT job_id) AS jobs
          FROM pending_questions GROUP BY question_key ORDER BY jobs DESC, question_key""")]

    snap.vetoes = _vetoes(conn)

    needed = int(((cfg.get("agent") or {}).get("probation") or {}).get("clean_runs_to_trust", 3))
    probation_on = bool(((cfg.get("agent") or {}).get("probation") or {}).get("enabled", True))
    for r in _rows(conn, """
        SELECT k.site_key, COALESCE(t.approved, 0) AS approved, COALESCE(t.confirmed_submissions, 0) AS confirmed,
               (SELECT COUNT(*) FROM site_clean_runs c WHERE c.site_key = k.site_key) AS clean
          FROM (SELECT site_key FROM site_trust UNION SELECT site_key FROM site_clean_runs) k
          LEFT JOIN site_trust t ON t.site_key = k.site_key ORDER BY k.site_key"""):
        trusted = bool(r["approved"] or r["confirmed"] > 0 or (needed > 0 and r["clean"] >= needed) or not probation_on)
        snap.sites.append({"site": r["site_key"], "trusted": trusted, "approved": bool(r["approved"]), "confirmed": r["confirmed"], "clean": r["clean"], "needed": needed})


_VETO_SQL = """
SELECT a.job_id, a.status, a.error_code, a.error_message, a.started_at, a.finished_at, j.title, j.company,
       f.score, f.payload {override_col}
  FROM applications a JOIN jobs j ON j.id = a.job_id
  LEFT JOIN fit_scores f ON f.job_id = a.job_id AND f.created_at = (SELECT MAX(created_at) FROM fit_scores WHERE job_id = a.job_id)
  {override_join}
 WHERE (a.status = 'skipped' AND a.error_code = 'not_a_fit')
    OR (a.status = 'needs_review' AND a.error_code = 'fit_disagreement')
    OR (a.status = 'skipped' AND a.error_code IS NULL AND a.error_message LIKE 'fit veto not confirmed%')
 ORDER BY CASE a.status WHEN 'needs_review' THEN 0 ELSE 1 END, COALESCE(a.finished_at, a.started_at) DESC LIMIT 100
"""


def _vetoes(conn: sqlite3.Connection) -> list[VetoRow]:
    """Fit decisions, with the ones that need you first. Works on a database from before overrides existed."""
    rows = _rows(conn, _VETO_SQL.format(override_col=", o.decision AS override", override_join="LEFT JOIN fit_overrides o ON o.job_id = a.job_id"))
    if not rows:
        rows = _rows(conn, _VETO_SQL.format(override_col=", NULL AS override", override_join=""))
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload"] or "{}")
        except ValueError:
            payload = {}
        payload = payload if isinstance(payload, dict) else {}
        review = payload.get("review") or {}
        breakers = payload.get("deal_breakers") or []
        if r["override"] == "skip":
            where = "rejected"
        elif r["status"] == "needs_review":
            where = "disputed"
        elif (r["error_message"] or "").startswith("fit veto not confirmed"):
            where = "waiting"
        elif review.get("agreement") == "confirmed":
            where = "confirmed"
        elif breakers:
            where = "single"
        else:
            where = "low"
        reason = f"{breakers[0].get('issue', '')}: “{breakers[0].get('quote', '')}”" if breakers else (payload.get("summary") or r["error_message"] or "")
        out.append(VetoRow(job_id=r["job_id"], title=r["title"] or "(unknown)", company=r["company"] or "", where=where, score=r["score"],
                           reason=" ".join(str(reason).split()), reviewer=str(review.get("model") or ""), when=parse_ts(r["finished_at"] or r["started_at"])))
    return out


def read_trace(folder: Path) -> list[dict[str, Any]]:
    steps = []
    try:
        for line in (folder / "trace" / "steps.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                steps.append(entry)
    except OSError:
        pass
    return steps


def _fill_daemon(snap: Snapshot, root: Path, cfg: dict[str, Any], now: datetime) -> None:
    d = snap.daemon
    pid = None
    if snap.db_ok:
        try:
            conn = _connect(root / str((cfg.get("daemon") or {}).get("state_db", "state.db")))
            try:
                rows = _rows(conn, "SELECT holder_pid, holder_host, acquired_at, heartbeat_at FROM daemon_lock WHERE id = 1")
            finally:
                conn.close()
            if rows:
                pid = rows[0]["holder_pid"]
                d.pid, d.since = pid, parse_ts(rows[0]["acquired_at"])
                beat = parse_ts(rows[0]["heartbeat_at"])
                d.heartbeat_age = (now - beat).total_seconds() if beat else None
                same_host = rows[0]["holder_host"] in ("", None, socket.gethostname())
                alive = pid_alive(pid) if same_host else None
                if alive is False:
                    d.state = "stopped"
                elif d.heartbeat_age is not None and d.heartbeat_age < FRESH_HEARTBEAT_S:
                    d.state = "running"
                elif alive:
                    d.state = "running"               # alive, but busy in one long job between heartbeats
                else:
                    d.state = "stalled"
            else:
                d.state = "stopped"
        except sqlite3.Error:
            pass
    log = root / (((cfg.get("daemon") or {}).get("log_file")) or "daemon.log")
    try:
        d.last_log_age = max(0.0, now.timestamp() - log.stat().st_mtime)
    except OSError:
        pass
    for line in reversed(snap.log):
        if line.strip():
            d.activity = line.strip()
            break
    for line in reversed(snap.log):
        m = re.search(r"\[(Stage [123]|Daemon)\]", line)
        if m:
            d.stage = m.group(1)
            break
    if snap.run:
        d.cycle_running = snap.run.get("status") == "running" and d.state == "running"
        interval = int((cfg.get("daemon") or {}).get("interval_seconds", 3600))
        finished = parse_ts(snap.run.get("finished_at"))
        if finished and d.state == "running":
            d.next_cycle = finished + timedelta(seconds=interval)


def job_detail(root: str | Path, job_id: str) -> dict[str, Any]:
    """Everything known about one job: the application row, the posting, the fit verdict, the answers and the agent's steps."""
    root = Path(root)
    cfg = load_config(root)
    db_path = root / str((cfg.get("daemon") or {}).get("state_db", "state.db"))
    out: dict[str, Any] = {"job_id": job_id, "app": None, "job": None, "fit": None, "answers": [], "probation": None, "steps": [], "shots": []}
    folder = root / "applications" / safe_dirname(job_id)
    if db_path.is_file():
        try:
            conn = _connect(db_path)
        except sqlite3.Error:
            conn = None
        try:
            rows = _rows(conn, "SELECT * FROM applications WHERE job_id = ?", (job_id,))
            out["app"] = dict(rows[0]) if rows else None
            rows = _rows(conn, "SELECT id, title, company, location, apply_url, source, score, reason, ats FROM jobs WHERE id = ?", (job_id,))
            out["job"] = dict(rows[0]) if rows else None
        finally:
            if conn is not None:
                conn.close()
    out["fit"] = _read_json(folder / "fit.json")
    answers = _read_json(folder / "answers.json")
    out["answers"] = answers if isinstance(answers, list) else []
    out["probation"] = _read_json(folder / "probation.json")
    out["steps"] = read_trace(folder)
    out["shots"] = sorted(str(p) for p in (folder / "trace").glob("step_*.png")) if (folder / "trace").is_dir() else []
    out["folder"] = str(folder)
    return out
