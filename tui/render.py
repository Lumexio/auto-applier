"""Turns a data.Snapshot into rich Text for each panel. Text objects (not markup strings), so a job title or an error message
containing [brackets] can never be misread as formatting. No Textual import here: this is testable without it."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from rich.text import Text

from .data import STATUSES, AppRow, Snapshot, VetoRow

STATUS_STYLE = {
    "submitted": "bold #5fd75f", "unconfirmed": "#ffd75f", "needs_review": "#d787ff", "skipped": "dim", "failed": "#ff5f5f",
    "in_progress": "bold #5fd7ff", "duplicate": "dim",
}
STATUS_LABEL = {"needs_review": "parked", "in_progress": "working"}
SPARK = "▁▂▃▄▅▆▇█"


def fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    s = int(max(0, seconds))
    if s < 90:
        return f"{s}s"
    if s < 90 * 60:
        return f"{s // 60}m"
    h, m = divmod(s // 60, 60)
    return f"{h}h {m:02d}m" if h < 48 else f"{h // 24}d"


def hm(dt: datetime | None, now: datetime | None = None) -> str:
    if dt is None:
        return "--:--"
    local = dt.astimezone()
    today = (now or datetime.now(timezone.utc)).astimezone().date()
    return local.strftime("%H:%M") if local.date() == today else local.strftime("%a %H:%M")


def clip(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def label(status: str) -> str:
    return STATUS_LABEL.get(status, status)


def bar(used: int, limit: int, width: int = 20) -> Text:
    limit = max(1, limit)
    filled = min(width, round(width * used / limit))
    style = "#5fd75f" if used < limit * 0.7 else ("#ffd75f" if used < limit else "#ff5f5f")
    return Text("█" * filled, style=style) + Text("░" * (width - filled), style="dim")


def spark(values: list[int]) -> str:
    top = max(values) if values else 0
    if top == 0:
        return SPARK[0] * len(values)
    return "".join(SPARK[min(len(SPARK) - 1, round(v / top * (len(SPARK) - 1)))] for v in values)


# --------------------------------------------------------------------------- panels

def render_header(snap: Snapshot) -> Text:
    d = snap.daemon
    t = Text()
    style = {"running": "bold #5fd75f", "stalled": "bold #ffd75f", "stopped": "bold #ff5f5f"}.get(d.state, "dim")
    t.append("● ", style=style).append(f"daemon {d.state}", style=style)
    if d.pid and d.state != "stopped":
        t.append(f" (pid {d.pid}", style="dim")
        if d.since:
            t.append(f", up {fmt_age((snap.taken_at - d.since).total_seconds())}", style="dim")
        t.append(")", style="dim")
    t.append("   │   ")
    if snap.models is None:
        t.append("● Ollama not answering", style="bold #ff5f5f")
    elif snap.models:
        used = sum(m.vram_gb for m in snap.models)
        t.append("● Ollama ", style="#5fd75f").append(", ".join(m.name for m in snap.models)).append(f"  {used:.1f} GB GPU", style="dim")
    else:
        t.append("● Ollama idle (no model loaded)", style="#5fd75f")
    if snap.mode:
        t.append("   │   mode ").append(snap.mode, style="bold" if snap.mode != "autonomous" else "bold #ffd75f")
    if d.cycle_running:
        t.append("   │   cycle running", style="#5fd7ff")
    elif d.next_cycle:
        t.append(f"   │   next cycle ~{hm(d.next_cycle, snap.taken_at)}", style="dim")
    return t


def render_today(snap: Snapshot) -> Text:
    t = Text()
    t.append("Submissions, last 24 h  ").append(f"{snap.cap_used}/{snap.cap_limit}\n", style="bold")
    t.append(bar(snap.cap_used, snap.cap_limit, 24)).append("\n\n")
    t.append("Today\n", style="bold underline")
    shown = False
    for status in STATUSES:
        n = snap.today.get(status, 0)
        if n:
            shown = True
            t.append(f"  {n:>3}  ", style=STATUS_STYLE[status]).append(f"{label(status)}\n")
    if not shown:
        t.append("  nothing yet today\n", style="dim")
    t.append("\nAll time  ", style="bold")
    total = sum(snap.all_time.values())
    t.append(f"{total} applications", style="dim")
    parts = [f"{snap.all_time[s]} {label(s)}" for s in STATUSES if snap.all_time.get(s)]
    if parts:
        t.append("\n  " + "  ·  ".join(parts), style="dim")
    if snap.week:
        t.append("\n\nLast 7 days (submitted)  ", style="bold")
        t.append(spark([w[1] for w in snap.week]), style="#5fd75f").append("  ").append(str(sum(w[1] for w in snap.week)), style="bold")
        t.append("\nLast 7 days (all outcomes) ", style="bold")
        t.append(spark([w[1] + w[2] for w in snap.week]), style="#5fd7ff").append("  ").append(str(sum(w[1] + w[2] for w in snap.week)), style="bold")
    return t


FUNNEL_LABELS = (
    ("total_scraped", "scraped"), ("removed_duplicates", "duplicates"), ("removed_expired", "expired"), ("removed_scam", "scams"),
    ("removed_exclusions", "excluded"), ("removed_salary", "salary"), ("removed_relevance", "not relevant"),
    ("removed_location", "location"), ("removed_authorization", "authorization"), ("truncated_by_cap", "over the daily cap"),
)


def render_funnel(snap: Snapshot) -> Text:
    t = Text()
    if not snap.run:
        return t.append("no runs yet", style="dim")
    run = snap.run
    style = {"completed": "#5fd75f", "running": "#5fd7ff", "failed": "#ff5f5f", "halted": "#ffd75f"}.get(run.get("status", ""), "")
    t.append(f"Run #{run['id']}  ", style="bold").append(str(run.get("status")), style=style)
    from .data import parse_ts
    t.append(f"  started {hm(parse_ts(run.get('started_at')), snap.taken_at)}\n", style="dim")
    if snap.funnel:
        scraped = snap.funnel.get("total_scraped", 0)
        for key, name in FUNNEL_LABELS:
            n = snap.funnel.get(key, 0)
            if n or key == "total_scraped":
                t.append(f"  {n:>4}  ", style="bold" if key == "total_scraped" else "").append(f"{name}\n", style="" if key == "total_scraped" else "dim")
        removed = sum(snap.funnel.get(k, 0) for k, _ in FUNNEL_LABELS[1:9])
        t.append(f"  {max(0, scraped - removed):>4}  ", style="bold #5fd75f").append("went on to stage 3\n")
    else:
        t.append("  (no funnel numbers for this run yet)\n", style="dim")
    if snap.stage3:
        t.append("Applied stage:  ", style="bold")
        t.append("  ".join(f"{n} {label(s)}" for s, n in snap.stage3.items()), style="")
    return t


def render_models(snap: Snapshot) -> Text:
    t = Text()
    if snap.models is None:
        t.append("Ollama is not answering on its API.\n", style="#ff5f5f")
    elif snap.models:
        t.append("Loaded now\n", style="bold underline")
        for m in snap.models:
            where = "GPU" if m.total_gb and m.vram_gb >= m.total_gb * 0.98 else f"{m.vram_gb:.1f} of {m.total_gb:.1f} GB on GPU"
            until = f", unloads {hm(m.expires, snap.taken_at)}" if m.expires else ""
            t.append(f"  {m.name}", style="#5fd75f").append(f"  {where}{until}\n", style="dim")
    else:
        t.append("No model loaded right now\n", style="dim")
    if snap.roles:
        t.append("\nRoles\n", style="bold underline")
        for r in snap.roles:
            t.append("  ● " if r.loaded else "  ○ ", style="#5fd75f" if r.loaded else "dim")
            t.append(f"{r.role:<7}", style="bold").append(f" {r.model}", style="").append("  " + (", ".join(r.tasks) or "unused") + "\n", style="dim")
    elif snap.base_model:
        t.append(f"\nOne model for every job: {snap.base_model}\n", style="dim")
    return t


def decision_label(step: dict[str, Any]) -> str:
    kind = str(step.get("decision", "?"))
    target = step.get("target")
    if kind == "click" and target:
        return f"click “{clip(str(target), 18)}”"
    return {"fill": "fill form", "ready": "ready", "stop": "stop"}.get(kind, kind)


def flow_chain(steps: list[dict[str, Any]], fit: dict[str, Any] | None, outcome: str = "") -> Text:
    """open ▸ fit 82% ✓ ▸ fill form ▸ click “Next” ▸ ready. A model-chosen step is marked ◆."""
    t = Text("open", style="dim")
    if fit and fit.get("score") is not None:
        verdict = fit.get("verdict", "")
        t.append(" ▸ ", style="dim").append(f"fit {float(fit['score']):.0%} {'✓' if verdict == 'apply' else '✗'}", style="#5fd75f" if verdict == "apply" else "#ff5f5f")
    for step in steps:
        t.append(" ▸ ", style="dim")
        kind = step.get("decision")
        style = {"stop": "#ff5f5f", "ready": "bold #5fd75f"}.get(kind, "#5fd7ff" if step.get("source") == "llm" else "")
        t.append(decision_label(step) + (" ◆" if step.get("source") == "llm" else ""), style=style)
    if outcome:
        t.append(" ▸ ", style="dim").append(label(outcome), style=STATUS_STYLE.get(outcome, ""))
    return t


def render_now(snap: Snapshot) -> Text:
    t = Text()
    n = snap.now
    if n and n.live:
        t.append("▶ ", style="bold #5fd7ff").append(clip(n.app.title, 48), style="bold").append(f"  @ {clip(n.app.company, 28)}", style="")
        t.append(f"   via {n.app.site or '…'}   working for {fmt_age(n.age_s)}\n", style="dim")
        t.append(flow_chain(n.steps, n.fit)).append("\n")
        if n.steps:
            last = n.steps[-1]
            t.append(f"step {last.get('step', len(n.steps))}: ", style="dim").append(clip(str(last.get("why", "")), 110), style="italic")
            t.append(f"  [{last.get('source', 'policy')}]", style="dim")
        else:
            t.append("opening the posting and checking the fit …", style="dim")
        return t
    d = snap.daemon
    if d.state == "stopped":
        t.append("The daemon is not running.\n", style="bold #ff5f5f").append("Start it with: setup.bat -All -Background  (or python daemon.py)", style="dim")
    elif d.cycle_running:
        t.append(f"▶ {d.stage or 'cycle'}  ", style="#5fd7ff").append(clip(d.activity, 120), style="dim")
    else:
        t.append("Idle between cycles", style="dim")
        if d.next_cycle:
            t.append(f", next ~{hm(d.next_cycle, snap.taken_at)}", style="dim")
        t.append("\n")
        if snap.recent:
            r = snap.recent[0]
            t.append("last: ", style="dim").append(clip(r.title, 40)).append(f" @ {clip(r.company, 24)}  ", style="").append(label(r.status), style=STATUS_STYLE.get(r.status, ""))
    return t


VETO_LABEL = {
    "disputed": "models disagree: your call", "waiting": "waiting for a 2nd opinion", "confirmed": "veto confirmed", "single": "vetoed by one model",
    "low": "low fit score", "rejected": "you rejected it",
}
VETO_STYLE = {"disputed": "bold #d787ff", "waiting": "#ffd75f", "single": "#ffd75f", "confirmed": "dim", "low": "dim", "rejected": "dim"}


def veto_cells(row: VetoRow, now: datetime) -> tuple[Text, Text, Text, Text, Text, Text]:
    return (
        Text(hm(row.when, now)), Text(clip(row.title, 34)), Text(clip(row.company, 20)),
        Text("-" if row.score is None else f"{row.score:.0%}"), Text(VETO_LABEL.get(row.where, row.where), style=VETO_STYLE.get(row.where, "")),
        Text(clip(row.reason, 70), style="dim"),
    )


def render_needs(snap: Snapshot) -> Text:
    t = Text()
    disputed = [v for v in snap.vetoes if v.where == "disputed"]
    waiting = [v for v in snap.vetoes if v.where == "waiting"]
    if disputed:
        t.append(f"{len(disputed)} job{'s' if len(disputed) != 1 else ''} where the fit models disagree", style="bold #d787ff")
        t.append("   (press v, then python fit_review.py approve|reject <job>)\n", style="dim")
        for v in disputed[:2]:
            t.append(f"  • {clip(v.title, 50)} @ {clip(v.company, 24)}\n", style="")
    if waiting:
        t.append(f"{len(waiting)} veto{'es' if len(waiting) != 1 else ''} waiting for a second opinion", style="#ffd75f")
        t.append("   (the reviewing model is unavailable; retried next cycle)\n", style="dim")
    if snap.pending:
        t.append(f"{len(snap.pending)} question{'s' if len(snap.pending) != 1 else ''} need your answer", style="bold #d787ff")
        t.append("   (python answers.py pending)\n", style="dim")
        for p in snap.pending[:4]:
            t.append(f"  • {clip(p['question_text'], 72)}", style="").append(f"  ×{p['jobs']} job{'s' if p['jobs'] != 1 else ''}\n", style="dim")
    else:
        t.append("Nothing waiting for your answers\n", style="#5fd75f")
    t.append("\nSites  ", style="bold")
    if not snap.sites:
        t.append("none seen yet", style="dim")
    for s in snap.sites:
        if s["trusted"]:
            t.append(f"{s['site']} ✓  ", style="#5fd75f")
        else:
            t.append(f"{s['site']} {s['clean']}/{s['needed']}  ", style="#ffd75f")
    if any(not s["trusted"] for s in snap.sites):
        t.append("\n  ✓ trusted · n/m = clean runs toward trust (python sites.py approve <site>)", style="dim")
    return t


def app_cells(row: AppRow, now: datetime) -> tuple[Text, Text, Text, Text, Text, Text]:
    return (
        Text(hm(row.when, now)), Text(clip(row.title, 38)), Text(clip(row.company, 22)), Text(row.site or "-", style="dim"),
        Text(label(row.status), style=STATUS_STYLE.get(row.status, "")), Text(clip(row.detail or row.error_code, 60), style="dim"),
    )


# --------------------------------------------------------------------------- one job

def render_job(detail: dict[str, Any]) -> Text:
    t = Text()
    app, job, fit = detail.get("app") or {}, detail.get("job") or {}, detail.get("fit") or {}
    t.append(clip(job.get("title") or "(unknown job)", 90) + "\n", style="bold")
    t.append(f"{job.get('company', '')}   {job.get('location') or ''}   source: {job.get('source', '')}\n", style="dim")
    if job.get("apply_url"):
        t.append(clip(job["apply_url"], 120) + "\n", style="dim")
    status = app.get("status", "?")
    t.append("\nOutcome  ", style="bold").append(label(status), style=STATUS_STYLE.get(status, ""))
    if app.get("error_code"):
        t.append(f"  ({app['error_code']})", style="dim")
    t.append(f"   attempts {app.get('attempts', 0)}\n")
    if app.get("error_message"):
        t.append(clip(app["error_message"], 300) + "\n", style="dim")
    if fit:
        t.append("\nFit  ", style="bold")
        if fit.get("score") is not None:
            t.append(f"{float(fit['score']):.0%} {fit.get('verdict', '')}", style="#5fd75f" if fit.get("verdict") == "apply" else "#ff5f5f")
        t.append(f"   model {fit.get('model', '?')}\n", style="dim")
        if fit.get("summary"):
            t.append(clip(fit["summary"], 300) + "\n")
        for d in fit.get("deal_breakers") or []:
            t.append("  deal-breaker: ", style="#ff5f5f").append(clip(str(d.get("issue", "")) + " — “" + str(d.get("quote", "")) + "”", 200) + "\n", style="dim")
        for d in fit.get("vetoes_met") or []:
            t.append("  waived by your facts: ", style="#5fd75f").append(clip(str(d.get("quote", "")), 120) + f"  ({d.get('why', '')})\n", style="dim")
        review = fit.get("review")
        if review:
            agreement = str(review.get("agreement", ""))
            style = {"confirmed": "#ff5f5f", "disputed": "bold #d787ff", "unavailable": "#ffd75f"}.get(agreement, "")
            t.append("  second opinion ", style="bold").append(f"({review.get('model', '?')}): ").append(
                {"confirmed": "also skips", "disputed": f"would apply ({float(review.get('score') or 0):.0%})", "unavailable": "not available yet"}.get(agreement, agreement), style=style)
            t.append("\n")
        if status in ("needs_review", "skipped") and (app.get("error_code") in ("fit_disagreement", "not_a_fit")):
            t.append(f"  decide: python fit_review.py approve|reject {str(detail.get('job_id', ''))[:20]}\n", style="dim")
    answers = detail.get("answers") or []
    if answers:
        done = [a["answered"] for a in answers if isinstance(a, dict) and "answered" in a]
        todo = [a["unanswered"] for a in answers if isinstance(a, dict) and "unanswered" in a]
        t.append("\nAnswers  ", style="bold").append(f"{len(done)} filled, {len(todo)} not answered\n", style="")
        sources: dict[str, int] = {}
        for a in done:
            sources[str(a.get("source", "?"))] = sources.get(str(a.get("source", "?")), 0) + 1
        if sources:
            t.append("  from: " + ", ".join(f"{k} {v}" for k, v in sources.items()) + "\n", style="dim")
        for q in todo[:5]:
            t.append(f"  ? {clip(str(q.get('label', q.get('question_text', ''))), 80)}\n", style="#d787ff")
    if detail.get("probation"):
        t.append("\nProbation  ", style="bold").append(clip(json_line(detail["probation"]), 200) + "\n", style="dim")
    t.append("\nFlow  ", style="bold").append(flow_chain(detail.get("steps") or [], fit or None, status if status in STATUS_STYLE else "")).append("\n")
    return t


def json_line(obj: Any) -> str:
    import json
    try:
        return json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(obj)


def step_cells(step: dict[str, Any]) -> tuple[Text, Text, Text, Text, Text]:
    llm = step.get("source") == "llm"
    return (
        Text(str(step.get("step", "?"))), Text(str(step.get("page", "")), style="dim"), Text(decision_label(step), style="#5fd7ff" if llm else ""),
        Text("model ◆" if llm else "policy", style="#d787ff" if llm else "dim"), Text(clip(str(step.get("why", "")), 90), style="dim"),
    )
