"""What is waiting for you, on one screen. Read-only (it never writes and never touches the daemon).

    python pending.py              print it once
    python pending.py --watch 60   redraw every 60 seconds (the "Pending" pane that desk.ps1 opens at login)

The lists come from the same data layer as the dashboard (tui/data.py), plus the parked jobs the dashboard's panel does not
list: postings that send you to the company's own site and runs that were cut off. Uses only the standard library and PyYAML.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from tui import data

ROOT = Path(__file__).resolve().parent
SHOWN = 8                    # items listed per section; the rest are counted


class Style:
    def __init__(self, color: bool) -> None:
        self.on = color

    def _wrap(self, code: str, text: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.on else text

    def bold(self, t: str) -> str:
        return self._wrap("1", t)

    def dim(self, t: str) -> str:
        return self._wrap("2", t)

    def good(self, t: str) -> str:
        return self._wrap("32", t)

    def warn(self, t: str) -> str:
        return self._wrap("33", t)

    def bad(self, t: str) -> str:
        return self._wrap("31", t)

    def need(self, t: str) -> str:
        return self._wrap("1;35", t)


def clip(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: max(1, n - 1)] + "…"


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def visible_len(text: str) -> int:
    return len(_ANSI.sub("", text))


def wrap(text: str, width: int) -> list[str]:
    """Break a line at spaces to fit ``width`` columns, keeping its indent plus two for the continuation. Colour codes have no
    spaces in them and a terminal carries a colour across a line break, so they are left where they are."""
    if visible_len(text) <= width:
        return [text]
    lead = len(text) - len(text.lstrip(" "))
    lines: list[str] = []
    current, used = " " * lead, lead
    for word in text.strip(" ").split(" "):
        w = visible_len(word)
        if used > (lead if not lines else lead + 2) and used + 1 + w > width:
            lines.append(current)
            current, used = " " * (lead + 2) + word, lead + 2 + w
        else:
            sep = " " if used > (lead if not lines else lead + 2) else ""
            current, used = current + sep + word, used + len(sep) + w
    lines.append(current)
    return lines


def ago(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    m = int(seconds // 60)
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def render(snap: data.Snapshot, parked: list[data.ParkedRow], color: bool = False, width: int = 100, now: datetime | None = None,
           outreach: list[data.OutreachRow] | None = None) -> str:
    s = Style(color)
    now = now or datetime.now().astimezone()
    width = max(50, width)
    out: list[str] = []

    def line(text: str = "", indent: int = 0) -> None:
        out.extend(wrap(" " * indent + text, width))

    def link(url: str, indent: int) -> None:
        if url:
            out.append(" " * indent + s.dim(clip(url, max(30, width - indent - 1))))

    def more(total: int) -> None:
        if total > SHOWN:
            line(s.dim(f"… and {total - SHOWN} more"), 3)

    d = snap.daemon
    line(s.bold("AUTO-APPLIER") + s.dim(f"  ·  what is waiting for you  ·  {now.strftime('%a %d %b %H:%M')}"))
    if not snap.db_ok:
        line(s.bad("No state.db yet: the daemon has never run in this folder."))
        return "\n".join(out)

    if d.state == "running":
        since = (now - d.since).total_seconds() if d.since else None
        nxt = f"  ·  next cycle ~{d.next_cycle.astimezone().strftime('%H:%M')}" if d.next_cycle else ""
        line(f"daemon  {s.good('running')} (pid {d.pid}, up {ago(since)}){nxt}")
    elif d.state == "stalled":
        line(f"daemon  {s.warn('not responding')} (pid {d.pid}); check the log pane")
    else:
        line(f"daemon  {s.bad('STOPPED')}   start it:  setup.bat -All -Background")
    if snap.models is None:
        line(f"ollama  {s.bad('not answering')}   (models are used again once it is up; the daemon only measures it at start)")
    else:
        loaded = ", ".join(m.name for m in snap.models) or "no model loaded"
        line(f"ollama  {s.good('up')} ({loaded})")
    line(f"today   {snap.today.get('submitted', 0)} submitted  ·  {snap.cap_used}/{snap.cap_limit} of the daily cap used")

    questions = snap.pending
    disputed = [v for v in snap.vetoes if v.where == "disputed"]
    waiting = [v for v in snap.vetoes if v.where == "waiting"]
    apply_by_hand = [p for p in parked if p.kind == "apply"]
    verify = [p for p in parked if p.kind == "verify"]
    other = [p for p in parked if p.kind == "other"]
    probation = [x for x in snap.sites if not x["trusted"]]
    messages = outreach or []

    line()
    line(s.bold("NEEDS YOU"))
    if not (questions or disputed or apply_by_hand or verify or other or probation or messages):
        line(s.good("Nothing is waiting for you."), 1)

    if questions:
        line(s.need(plural(len(questions), "question")) + " the forms asked that it cannot answer from your facts", 1)
        line(s.dim('answer once, used everywhere:  python answers.py set "<question>" "<answer>" --requeue'), 3)
        for q in questions[:SHOWN]:
            line(f"• {clip(q['question_text'], width - 16)}  " + s.dim(f"×{plural(q['jobs'], 'job')}"), 3)
        more(len(questions))
    if messages:
        line(s.need(plural(len(messages), "message")) + " to recruiters waiting for your OK (nothing is sent without it)", 1)
        line(s.dim("python outreach_queue.py list     then  approve <id> | reject <id>"), 3)
        for m in messages[:SHOWN]:
            line(f"• {clip(m.recruiter, 24)} @ {clip(m.company, 22)}  " + s.dim(clip(m.text, 40)), 3)
        more(len(messages))
    if disputed:
        line(s.need(plural(len(disputed), "job")) + " where the fit models disagree: your call", 1)
        line(s.dim("python fit_review.py list     then  approve | reject <job>"), 3)
        for v in disputed[:SHOWN]:
            line(f"• {clip(v.title, 46)} @ {clip(v.company, 22)}", 3)
        more(len(disputed))
    if apply_by_hand:
        line(s.need(plural(len(apply_by_hand), "job")) + " to apply for yourself (the posting sends you to the company's own site)", 1)
        for p in apply_by_hand[:SHOWN]:
            line(f"• {clip(p.title, 46)} @ {clip(p.company, 22)}", 3)
            link(p.url, 5)
        more(len(apply_by_hand))
    if verify:
        line(s.need(plural(len(verify), "job")) + " to check by hand (a run was cut off: did the application go through?)", 1)
        for p in verify[:SHOWN]:
            line(f"• {clip(p.title, 46)} @ {clip(p.company, 22)}", 3)
            link(p.url, 5)
        more(len(verify))
    if other:
        line(s.need(plural(len(other), "other parked job")) + " (python -m tui, then the job's detail for why)", 1)
        for p in other[:SHOWN]:
            line(f"• {clip(p.title, 40)} @ {clip(p.company, 20)}  " + s.dim(clip(p.detail, 40)), 3)
        more(len(other))
    if probation:
        line(s.warn(plural(len(probation), "site")) + " on probation: filled in and reviewed, not submitted until trusted", 1)
        for x in probation[:SHOWN]:
            line(f"• {x['site']}  {x['clean']}/{x['needed']} clean runs   " + s.dim(f"python sites.py approve {x['site']}"), 3)

    line()
    line(s.bold("WAITING ON THE MACHINE"))
    nothing = True
    if waiting:
        nothing = False
        line(f"{plural(len(waiting), 'veto')} waiting for a second opinion " + s.dim("(retried next cycle)"), 1)
    unconfirmed = snap.all_time.get("unconfirmed", 0)
    if unconfirmed:
        nothing = False
        line(f"{unconfirmed} application{'s' if unconfirmed != 1 else ''} sent but never confirmed " + s.dim("(not retried, so nothing is applied twice)"), 1)
    if snap.now and snap.now.live:
        nothing = False
        line(f"applying now: {clip(snap.now.app.title, 40)} @ {clip(snap.now.app.company, 20)}", 1)
    if nothing:
        line(s.dim("nothing"), 1)

    line()
    line(s.dim("live view: tui.bat    answer and decide in your browser: gui.bat    log: setup.bat -Logs    status: setup.bat -Status"))
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="What is waiting for you (read-only).")
    ap.add_argument("--watch", type=int, default=0, metavar="SECONDS", help="redraw every SECONDS until Ctrl+C")
    ap.add_argument("--root", default=str(ROOT), help="the project folder (default: this one)")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")       # type: ignore[attr-defined]
    except Exception:
        pass
    color = sys.stdout.isatty() and "NO_COLOR" not in os.environ
    if color and os.name == "nt":
        os.system("")                                                    # switches the Windows console to ANSI colour handling

    def once() -> str:
        width = shutil.get_terminal_size((100, 30)).columns - 1
        snap = data.snapshot(args.root)
        return render(snap, data.parked_for_you(args.root), color=color, width=width, outreach=data.outreach_waiting(args.root))

    if not args.watch:
        print(once())
        return 0
    try:
        while True:
            text = once()
            sys.stdout.write(("\x1b[H\x1b[2J" + text + "\n") if color else (text + "\n\n"))
            sys.stdout.flush()
            time.sleep(max(5, args.watch))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
