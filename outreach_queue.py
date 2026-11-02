"""Messages to recruiters wait for your OK. Nothing is sent that you have not approved.

    python outreach_queue.py list                       the messages waiting for you, then the ones you approved that are not sent yet
    python outreach_queue.py show <id>                  one message in full, with the wording the bot drafted
    python outreach_queue.py approve <id> [--text "..."]   send it (your own wording with --text); the daemon does it after its next cycle
    python outreach_queue.py approve --all              approve every waiting message as drafted
    python outreach_queue.py reject <id>                never send it (also takes back an approval that is not sent yet)
    python outreach_queue.py status                     counts, today's cap and whether outreach is switched on

After an application is submitted on LinkedIn the bot finds the recruiter on the posting, drafts a short note from your profile and puts it here.
It does not open that recruiter's page or send anything. The web interface shows the same list under "Needs you", with the text editable.

Sending happens only for an approved message, with exactly the stored wording, after the daemon's cycle, within ``outreach.max_per_day``, and only
while ``outreach.enabled`` is true. The bot's browser is used, so it is your LinkedIn account that sends. A recruiter is asked about once: a message
you turned down is not drafted again, and nobody you already wrote to is drafted at all. Set ``outreach.approval: false`` to go back to sending
without asking (not recommended).
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import bot_browser

MAX_CHARS = 300                      # LinkedIn's limit for the note on a connection request
DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "approval": True,                # false = send without asking
    "max_per_day": 5,
    "max_waiting": 20,               # drafts waiting for you; no new one is written while the list is this long
    "delay_seconds": [20, 60],       # between two messages
    "headless": True,
}


@dataclass
class Report:
    sent: int = 0
    not_sent: int = 0
    stopped: str = ""               # why the rest waits for the next cycle ("" = it did not stop)


def settings(cfg: dict[str, Any] | None) -> dict[str, Any]:
    return {**DEFAULTS, **((cfg or {}).get("outreach") or {})}


def check_text(text: str | None) -> str:
    """The message as it will be sent, or ValueError saying what is wrong with it."""
    text = " ".join((text or "").split())
    if not text:
        raise ValueError("The message is empty.")
    if len(text) > MAX_CHARS:
        raise ValueError(f"The message has {len(text)} characters; LinkedIn takes at most {MAX_CHARS}.")
    return text


def _is_profile(url: str) -> bool:
    u = urlparse(url or "")
    return (u.hostname or "").lower().removeprefix("www.").endswith("linkedin.com") and u.path.startswith("/in/")


# --------------------------------------------------------------------------- drafting (called after an application was submitted)

def hold_for_approval(state: Any, cfg: dict[str, Any] | None, job_id: str, job: dict[str, Any], recruiter: Any, text: str) -> str:
    """Put a drafted message in the queue instead of sending it. Returns the line for the daemon log."""
    opts = settings(cfg)
    who = f"{recruiter.name} ({job.get('company', '?')})"
    if len(state.outreach_queue_rows("awaiting")) >= int(opts["max_waiting"]):
        return f"[Outreach] {int(opts['max_waiting'])} messages are already waiting for you; not drafting one for {who} (python outreach_queue.py list)."
    try:
        text = check_text(text)
    except ValueError as e:
        return f"[Outreach] The draft for {who} was not usable ({e}); nothing queued."
    queued = state.queue_outreach(job_id, job.get("company", ""), job.get("title", ""), recruiter.name, recruiter.profile_url, text)
    if queued is None:
        return f"[Outreach] {who}: already asked about or contacted. Nothing queued."
    return f"[Outreach] Drafted a message to {who}. It waits for your OK, nothing is sent (python outreach_queue.py list)."


# --------------------------------------------------------------------------- sending what you approved

def ready_to_send(state: Any, opts: dict[str, Any]) -> list[dict[str, Any]]:
    """The approved messages that today's cap leaves room for, oldest first."""
    approved = state.outreach_queue_rows("approved")
    room = int(opts["max_per_day"]) - state.get_outreach_count_today()
    if approved and room <= 0:
        print(f"[Outreach] Daily limit reached ({state.get_outreach_count_today()}/{int(opts['max_per_day'])}); {len(approved)} approved message(s) wait for tomorrow.")
        return []
    return approved[: max(0, room)]


def send_approved(
    state: Any,
    cfg: dict[str, Any] | None,
    *,
    session: Any = None,
    should_stop: Callable[[], bool] | None = None,
    on_tick: Callable[[], None] | None = None,
    pause: Callable[[float], None] = time.sleep,
) -> Report:
    """Send the messages you approved. Opens the bot's browser only when there is one to send (or uses ``session``)."""
    opts = settings(cfg)
    if not opts["enabled"]:
        return Report()
    ready = ready_to_send(state, opts)
    if not ready:
        return Report()
    if session is None:
        print(f"[Outreach] Sending {len(ready)} message(s) you approved.")
        report = bot_browser.run_with_session(cfg or {}, bool(opts["headless"]), lambda s: _send(s, state, opts, ready, should_stop, on_tick, pause), "Outreach")
        return report or Report()
    return _send(session, state, opts, ready, should_stop, on_tick, pause)


def _send(session: Any, state: Any, opts: dict[str, Any], ready: list[dict[str, Any]], should_stop, on_tick, pause) -> Report:
    import outreach
    import reverify
    from stage3_apply.run import _safe_dirname

    report = Report()
    low, high = (list(opts["delay_seconds"]) + [0, 0])[:2]
    for n, row in enumerate(ready):
        if should_stop and should_stop():
            report.stopped = "shutdown requested"
            break
        row = state.outreach_queue_row(row["id"]) or row                      # you may have taken it back since the list was made
        if row["state"] != "approved":
            continue
        label = f"{row['recruiter_name']} ({row['company'] or '?'})"
        try:
            text = check_text(row["message_text"])
        except ValueError as e:
            state.finish_outreach(row["id"], "not_sent", f"not usable: {e}")
            report.not_sent += 1
            print(f"[Outreach] Not sending to {label}: {e}")
            continue
        if not _is_profile(row["recruiter_url"]):
            state.finish_outreach(row["id"], "not_sent", "not a LinkedIn profile link")
            report.not_sent += 1
            print(f"[Outreach] Not sending to {label}: {row['recruiter_url']!r} is not a LinkedIn profile.")
            continue
        if state.has_recruiter_been_contacted(row["recruiter_url"]):
            state.finish_outreach(row["id"], "not_sent", "already contacted")
            report.not_sent += 1
            print(f"[Outreach] Not sending to {label}: that recruiter was contacted before.")
            continue
        if n and max(low, high) > 0:
            pause(random.uniform(min(low, high), max(low, high)))
        if on_tick:
            on_tick()
        folder = Path("applications") / _safe_dirname(str(row["job_id"]))
        folder.mkdir(parents=True, exist_ok=True)
        recruiter = outreach.RecruiterInfo(name=row["recruiter_name"], title="", profile_url=row["recruiter_url"], source="approved_queue")
        channel, status = outreach.send_recruiter_outreach(session, recruiter, text, is_dry_run=False, screenshot_path=folder / "recruiter_outreach.png")
        here = session.current_url() if hasattr(session, "current_url") else ""
        if status in ("failed", "inmail_locked") and reverify.is_sign_in_url(here):
            report.stopped = "signed out"
            print(f"[Outreach] LinkedIn sent the browser to a sign-in page: not sending more this cycle. Sign in again with python daemon.py --login https://www.linkedin.com/login")
            break
        state.record_outreach(row["job_id"], row["company"] or "", row["recruiter_name"], row["recruiter_url"], text, channel, status)
        if status == "limit_reached":
            report.stopped = "LinkedIn's invitation limit"
            print(f"[Outreach] LinkedIn's weekly invitation limit is reached: the rest of the approved messages wait.")
            break
        if status == "sent":
            state.finish_outreach(row["id"], "sent", f"sent as a {channel.replace('_', ' ')}")
            report.sent += 1
            print(f"[Outreach] Sent the message you approved to {label}.")
        else:
            state.finish_outreach(row["id"], "not_sent", status)
            report.not_sent += 1
            print(f"[Outreach] Did not send to {label}: {status.replace('_', ' ')}.")
    return report


# --------------------------------------------------------------------------- command line

def _describe(row: dict[str, Any], full: bool = False) -> list[str]:
    head = f"[{row['id']}] {row['recruiter_name']} - {row['company'] or '?'} ({row['job_title'] or 'a job you applied for'})"
    lines = [head, f"     {row['message_text'] if full else row['message_text'][:110] + ('...' if len(row['message_text']) > 110 else '')}"]
    if full:
        lines.append(f"     {len(row['message_text'])} characters; {row['recruiter_url']}")
        if row["drafted_text"] != row["message_text"]:
            lines.append(f"     drafted as: {row['drafted_text']}")
        if row["outcome"]:
            lines.append(f"     outcome: {row['state']} ({row['outcome']})")
    return lines


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="config.yaml")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("status")
    p_show = sub.add_parser("show")
    p_show.add_argument("id", type=int)
    p_ok = sub.add_parser("approve")
    p_ok.add_argument("id", type=int, nargs="?")
    p_ok.add_argument("--text", help="send this wording instead of the draft")
    p_ok.add_argument("--all", action="store_true", help="approve every waiting message as drafted")
    p_no = sub.add_parser("reject")
    p_no.add_argument("id", type=int)
    args = parser.parse_args(argv)

    cfg: dict[str, Any] = {}
    if Path(args.config).is_file():
        from config_loader import load_config
        cfg = load_config(Path(args.config))
    from state import StateStore
    state = StateStore(cfg.get("daemon", {}).get("state_db", "state.db"))
    opts = settings(cfg)

    if args.cmd == "list":
        waiting, approved = state.outreach_queue_rows("awaiting"), state.outreach_queue_rows("approved")
        if not waiting and not approved:
            print("No message to a recruiter is waiting.")
        if waiting:
            print(f"{len(waiting)} message(s) waiting for your OK (python outreach_queue.py approve <id> | reject <id>):")
            print("\n".join(line for row in waiting for line in _describe(row)))
        if approved:
            print(f"{len(approved)} approved, sent by the daemon after its next cycle:")
            print("\n".join(line for row in approved for line in _describe(row)))
        return 0

    if args.cmd == "status":
        counts = {s: len(state.outreach_queue_rows(s)) for s in ("awaiting", "approved", "sent", "not_sent", "rejected")}
        print(f"outreach is {'ON' if opts['enabled'] else 'OFF (outreach.enabled: false): nothing is drafted or sent'}"
              f"; {'asks you first' if opts['approval'] else 'SENDS WITHOUT ASKING (outreach.approval: false)'}")
        print(f"waiting for you: {counts['awaiting']}  approved, not sent yet: {counts['approved']}  sent: {counts['sent']}  not sent: {counts['not_sent']}  turned down: {counts['rejected']}")
        print(f"sent or drafted today: {state.get_outreach_count_today()} of {int(opts['max_per_day'])}")
        return 0

    if args.cmd == "show":
        row = state.outreach_queue_row(args.id)
        if row is None:
            print(f"There is no message {args.id}.")
            return 1
        print("\n".join(_describe(row, full=True)))
        return 0

    if args.cmd == "reject":
        if not state.reject_outreach(args.id):
            print(f"Message {args.id} is not waiting or approved any more.")
            return 1
        print(f"Message {args.id} will not be sent.")
        return 0

    if args.all:
        if args.id is not None or args.text:
            print("--all approves the drafts as they are: leave out the id and --text.")
            return 1
        rows = state.outreach_queue_rows("awaiting")
        done = [r["id"] for r in rows if state.approve_outreach(r["id"])]
        print(f"Approved {len(done)} message(s)." if done else "No message is waiting.")
    else:
        if args.id is None:
            print("Say which message to approve, or use --all.")
            return 1
        text = None
        if args.text is not None:
            try:
                text = check_text(args.text)
            except ValueError as e:
                print(e)
                return 1
        if not state.approve_outreach(args.id, text):
            print(f"Message {args.id} is not waiting for an answer.")
            return 1
        print(f"Message {args.id} is approved.")
        done = [args.id]
    if done and not opts["enabled"]:
        print("Nothing is sent while outreach.enabled is false in config: switch it on and restart the daemon.")
    elif done:
        print("The daemon sends it after its next cycle.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
