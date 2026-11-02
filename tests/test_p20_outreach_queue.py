"""Messages to recruiters wait for the user's OK.

After an application was submitted the bot drafts a note to the recruiter of the posting. It used to send it at once. Now the draft is only queued; the
user approves it (command line or web interface, with the wording editable) and the daemon sends exactly that text after its cycle, within the daily
cap. Everything here uses made-up names and a stand-in for the page that does the sending.
"""

from __future__ import annotations

import io
import json
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import outreach
import outreach_queue
from daemon import Daemon
from gui import demo
from gui.service import ActionError, Desk
from outreach import RecruiterInfo
from pending import render
from state import StateStore
from stage3_apply.run import _handle_recruiter_outreach
from tests.test_p0_safety import PROFILE, tmp_cwd
from tests.test_p11_gui import INDEX, project, raises
from tui import data

DANA = RecruiterInfo(name="Dana Whitfield", title="Talent Acquisition", profile_url="https://www.linkedin.com/in/dana-whitfield-demo", source="job_page_poster")
SAM = RecruiterInfo(name="Sam Okafor", title="Recruiter", profile_url="https://www.linkedin.com/in/sam-okafor-demo", source="job_page_poster")
JOB = {"id": "j1", "title": "Backend Engineer", "company": "Northwind Labs"}
NOTE = "Hi Dana, I applied for the Backend Engineer role at Northwind Labs and would love to connect. Best, Alex."


def on(**over):
    return {"outreach": {"enabled": True, "max_per_day": 5, "delay_seconds": [0, 0], **over}, "llm": {"enabled": False}}


def queued(store, who=DANA, job_id="j1", text=NOTE, company="Northwind Labs"):
    return store.queue_outreach(job_id, company, "Backend Engineer", who.name, who.profile_url, text)


class Page:
    """What the sender needs of a browser session; ``url`` is where the browser is after the send."""

    def __init__(self, url="https://www.linkedin.com/in/dana-whitfield-demo"):
        self.url = url

    def current_url(self):
        return self.url

    def screenshot(self):
        return b"png"


def sender(*results):
    """A stand-in for outreach.send_recruiter_outreach that returns the given (channel, status) pairs in turn and records what it was asked to send."""
    calls = []
    answers = list(results)

    def fake(session, recruiter, text, is_dry_run=False, screenshot_path=None):
        calls.append((recruiter.profile_url, text, is_dry_run))
        return answers.pop(0) if answers else ("connection_note", "sent")
    return fake, calls


def run_send(store, cfg, page=None, fake=None):
    out = io.StringIO()
    with patch("outreach.send_recruiter_outreach", fake or sender()[0]), redirect_stdout(out):
        report = outreach_queue.send_approved(store, cfg, session=page or Page(), pause=lambda s: None)
    return report, out.getvalue()


# --------------------------------------------------------------------------- drafting: nothing is sent

def drafted(store, cfg, recruiter=DANA, job=JOB, job_id="j1"):
    out = io.StringIO()
    with patch("outreach.extract_recruiter_from_job_page", return_value=recruiter), \
            patch("outreach.send_recruiter_outreach", side_effect=AssertionError("sent without asking")), redirect_stdout(out):
        _handle_recruiter_outreach(MagicMock(), job, {"name": "Alex Rivera", "skills": ["Python"]}, store, job_id, Path("."), cfg, is_dry_run=False)
    return out.getvalue()


def test_after_an_application_the_note_is_queued_for_approval_and_nothing_is_sent():
    with tmp_cwd() as tmp:
        store = StateStore(tmp / "s.db")
        log = drafted(store, on())
        rows = store.outreach_queue_rows("awaiting")
        assert len(rows) == 1 and rows[0]["recruiter_url"] == DANA.profile_url and rows[0]["state"] == "awaiting"
        assert rows[0]["drafted_text"] == rows[0]["message_text"] and "Dana" in rows[0]["message_text"] and len(rows[0]["message_text"]) <= 280
        assert "waits for your OK, nothing is sent" in log and "outreach_queue.py list" in log
        assert store.conn.execute("SELECT COUNT(*) FROM recruiter_outreach").fetchone()[0] == 0, "nothing was sent or logged as sent"

        assert "already asked about or contacted" in drafted(store, on()), "the same recruiter is not drafted for twice"
        assert len(store.outreach_queue_rows("awaiting")) == 1

        for n in range(3):                                                             # the daily cap is for sending: it does not stop a draft
            store.record_outreach(f"s{n}", "Contoso", f"R{n}", f"https://www.linkedin.com/in/r{n}-demo", "hi", "message", "sent")
        assert "Drafted a message to Sam Okafor" in drafted(store, on(max_per_day=1), recruiter=SAM, job_id="j9")
        assert len(store.outreach_queue_rows("awaiting")) == 2

        for bad in ("", "x" * 400):                                                    # a draft that could never be sent is not put in front of you
            line = outreach_queue.hold_for_approval(store, on(), "j8", JOB, RecruiterInfo("Kim Ito", "", "https://www.linkedin.com/in/kim-ito-demo", "x"), bad)
            assert "was not usable" in line
        assert len(store.outreach_queue_rows("awaiting")) == 2
        store.close()


def test_a_full_list_stops_new_drafts_and_a_turned_down_recruiter_is_not_asked_about_again():
    with tmp_cwd() as tmp:
        store = StateStore(tmp / "s.db")
        drafted(store, on(max_waiting=1))
        assert "1 messages are already waiting for you" in drafted(store, on(max_waiting=1), recruiter=SAM)
        assert [r["recruiter_name"] for r in store.outreach_queue_rows("awaiting")] == ["Dana Whitfield"]
        first = store.outreach_queue_rows("awaiting")[0]["id"]
        assert store.reject_outreach(first)
        assert "already asked about" in drafted(store, on(), job_id="j2"), "you said no to Dana once"
        assert store.outreach_queue_rows("awaiting") == []
        store.close()


def test_without_approval_the_old_way_still_sends_at_once_and_the_daily_cap_applies_to_it():
    with tmp_cwd() as tmp:
        store = StateStore(tmp / "s.db")
        fake, calls = sender(("connection_note", "sent"))
        out = io.StringIO()
        with patch("outreach.extract_recruiter_from_job_page", return_value=DANA), patch("outreach.send_recruiter_outreach", fake), redirect_stdout(out):
            _handle_recruiter_outreach(MagicMock(), JOB, {"name": "Alex Rivera"}, store, "j1", Path("."), on(approval=False), is_dry_run=False)
        assert len(calls) == 1 and store.outreach_queue_rows("awaiting") == []
        assert store.conn.execute("SELECT status FROM recruiter_outreach").fetchall()[0][0] == "sent"
        with patch("outreach.extract_recruiter_from_job_page", return_value=SAM), patch("outreach.send_recruiter_outreach", fake), redirect_stdout(out):
            _handle_recruiter_outreach(MagicMock(), JOB, {}, store, "j2", Path("."), on(approval=False, max_per_day=1), is_dry_run=False)
        assert len(calls) == 1 and "Daily recruiter outreach limit reached" in out.getvalue()
        store.close()


def test_outreach_stays_off_by_default_and_asks_first_when_switched_on():
    import yaml
    cfg = yaml.safe_load((Path(__file__).resolve().parent.parent / "config.yaml").read_text(encoding="utf-8"))["outreach"]
    assert cfg["enabled"] is False and cfg["approval"] is True and cfg["max_waiting"] >= 1
    assert outreach_queue.settings({})["enabled"] is False and outreach_queue.settings({})["approval"] is True


# --------------------------------------------------------------------------- the queue's rules

def test_the_queue_moves_only_in_the_allowed_directions():
    with tmp_cwd() as tmp:
        store = StateStore(tmp / "s.db")
        a, b = queued(store), queued(store, SAM, text="Hi Sam, I applied and would love to connect. Best, Alex.")
        assert a and b and queued(store) is None, "one message per recruiter"
        assert not store.reject_outreach(9999) and not store.approve_outreach(9999)

        assert store.approve_outreach(a, "Hi Dana, a shorter note. Best, Alex.")
        row = store.outreach_queue_row(a)
        assert row["state"] == "approved" and row["message_text"] == "Hi Dana, a shorter note. Best, Alex." and row["drafted_text"] == NOTE, "your wording is kept next to the draft"
        assert not store.approve_outreach(a), "already decided"
        assert store.reject_outreach(a) and store.outreach_queue_row(a)["state"] == "rejected", "an approval can be taken back until it is sent"
        assert not store.approve_outreach(a), "a message turned down stays turned down"

        assert not store.finish_outreach(b, "sent", "x"), "only an approved message can finish"
        store.approve_outreach(b)
        assert store.finish_outreach(b, "sent", "sent as a connection note") and not store.reject_outreach(b), "a sent message cannot be taken back"
        assert store.outreach_queue_row(b)["finished_at"]

        store.record_outreach("j3", "Contoso", "Pat Lee", "https://www.linkedin.com/in/pat-lee-demo", "hi", "message", "sent")
        assert queued(store, RecruiterInfo("Pat Lee", "", "https://www.linkedin.com/in/pat-lee-demo", "x"), job_id="j3") is None, "someone already written to is not asked about"
        store.close()


def test_a_message_is_checked_before_it_can_be_approved():
    for text, problem in (("", "empty"), ("   \n ", "empty"), ("x" * 301, "301 characters")):
        try:
            outreach_queue.check_text(text)
        except ValueError as e:
            assert problem in str(e), (text, e)
        else:
            raise AssertionError(f"{text!r} was accepted")
    assert outreach_queue.check_text("  Hi   Dana,\n  best, Alex ") == "Hi Dana, best, Alex"
    assert len(outreach_queue.check_text("x" * 300)) == 300


# --------------------------------------------------------------------------- sending what was approved

def test_only_approved_messages_are_sent_and_with_exactly_the_approved_words():
    with tmp_cwd() as tmp:
        store = StateStore(tmp / "s.db")
        waiting = queued(store)
        mine = queued(store, SAM, job_id="j2", text="Hi Sam, draft. Best, Alex.")
        no = queued(store, RecruiterInfo("Kim Ito", "", "https://www.linkedin.com/in/kim-ito-demo", "x"), job_id="j3")
        store.approve_outreach(mine, "Hi Sam, my own wording. Best, Alex.")
        store.reject_outreach(no)

        fake, calls = sender(("connection_note", "sent"))
        report, log = run_send(store, on(), page=Page(SAM.profile_url), fake=fake)
        assert calls == [(SAM.profile_url, "Hi Sam, my own wording. Best, Alex.", False)], "the approved one only, as edited"
        assert (report.sent, report.not_sent, report.stopped) == (1, 0, "")
        assert store.outreach_queue_row(mine)["state"] == "sent" and store.outreach_queue_row(waiting)["state"] == "awaiting" and store.outreach_queue_row(no)["state"] == "rejected"
        logged = [dict(r) for r in store.conn.execute("SELECT recruiter_url, message_text, channel, status FROM recruiter_outreach")]
        assert logged == [{"recruiter_url": SAM.profile_url, "message_text": "Hi Sam, my own wording. Best, Alex.", "channel": "connection_note", "status": "sent"}]
        assert "Sent the message you approved to Sam Okafor" in log
        assert (Path("applications") / "j2").is_dir(), "the screenshot of what was sent has its place in the job's folder"

        report, _ = run_send(store, on(), fake=fake)
        assert report.sent == 0 and len(calls) == 1, "a sent message is not sent twice"
        store.close()


def test_the_browser_is_opened_only_for_an_approved_message_and_only_when_outreach_is_on():
    with tmp_cwd() as tmp:
        store = StateStore(tmp / "s.db")
        queued(store)
        with patch.object(outreach_queue.bot_browser, "run_with_session", side_effect=AssertionError("browser opened for nothing")):
            assert outreach_queue.send_approved(store, on()).sent == 0                      # nothing approved
            store.approve_outreach(1)
            assert outreach_queue.send_approved(store, {"outreach": {"enabled": False}}).sent == 0     # approved, but outreach is off
            assert outreach_queue.send_approved(store, {}).sent == 0
        with patch.object(outreach_queue.bot_browser, "run_with_session", return_value=outreach_queue.Report(sent=1)) as opened, redirect_stdout(io.StringIO()):
            assert outreach_queue.send_approved(store, on()).sent == 1
        opened.assert_called_once()
        store.close()


def test_the_daily_cap_and_linkedins_own_limit_leave_approved_messages_waiting():
    with tmp_cwd() as tmp:
        store = StateStore(tmp / "s.db")
        ids = [queued(store, RecruiterInfo(f"R{n}", "", f"https://www.linkedin.com/in/r{n}-demo", "x"), job_id=f"j{n}", text=f"Hi R{n}. Best, Alex.") for n in range(3)]
        for i in ids:
            store.approve_outreach(i)
        fake, calls = sender()
        report, log = run_send(store, on(max_per_day=2), fake=fake)
        assert report.sent == 2 and len(calls) == 2 and store.outreach_queue_row(ids[2])["state"] == "approved", "two today, the third waits"
        report, log = run_send(store, on(max_per_day=2), fake=fake)
        assert report.sent == 0 and "Daily limit reached (2/2); 1 approved message(s) wait for tomorrow" in log and len(calls) == 2

        fake, calls = sender(("connection_note", "limit_reached"))
        report, log = run_send(store, on(max_per_day=9), fake=fake)
        assert report.stopped == "LinkedIn's invitation limit" and len(calls) == 1
        assert store.outreach_queue_row(ids[2])["state"] == "approved", "the message that hit the limit stays approved for the next cycle"
        store.close()


def test_things_that_stop_a_send_leave_a_reason_and_never_a_blind_attempt():
    with tmp_cwd() as tmp:
        store = StateStore(tmp / "s.db")

        def approved(who, text=NOTE, url=None, job_id="j1"):
            i = store.queue_outreach(job_id, "Northwind Labs", "Backend Engineer", who, url or f"https://www.linkedin.com/in/{who.lower().replace(' ', '-')}-demo", text)
            store.approve_outreach(i)
            return i

        long_one = approved("Too Long")
        store.conn.execute("UPDATE outreach_queue SET message_text = ? WHERE id = ?", ("x" * 400, long_one))
        not_linkedin = approved("Off Site", url="https://example.test/in/someone")
        contacted = approved("Known Person")
        store.record_outreach("j0", "Contoso", "Known Person", "https://www.linkedin.com/in/known-person-demo", "hi", "message", "already_connected")
        good = approved("Good Person")
        store.conn.commit()

        fake, calls = sender(("connection_note", "sent"))
        report, log = run_send(store, on(max_per_day=50), fake=fake)
        assert [c[0] for c in calls] == ["https://www.linkedin.com/in/good-person-demo"], "only the one that passed every check was attempted"
        state = {i: (r["state"], r["outcome"]) for i in (long_one, not_linkedin, contacted, good) for r in [store.outreach_queue_row(i)]}
        assert state[long_one][0] == "not_sent" and "400 characters" in state[long_one][1]
        assert state[not_linkedin] == ("not_sent", "not a LinkedIn profile link") and state[contacted] == ("not_sent", "already contacted") and state[good][0] == "sent"
        assert report.sent == 1 and report.not_sent == 3

        # taken back after the list was made: the sender looks again before it acts
        late = approved("Late Change")
        stale = [store.outreach_queue_row(late)]
        store.reject_outreach(late)
        fake, calls = sender()
        with patch("outreach.send_recruiter_outreach", fake), redirect_stdout(io.StringIO()):
            outreach_queue._send(Page(), store, outreach_queue.settings(on()), stale, None, None, lambda s: None)
        assert calls == [] and store.outreach_queue_row(late)["state"] == "rejected"

        # LinkedIn wants a sign-in: the message stays approved, nothing is logged as sent, the rest waits
        first, second = approved("First Try"), approved("Second Try")
        fake, calls = sender(("message", "failed"))
        report, log = run_send(store, on(max_per_day=50), page=Page("https://www.linkedin.com/uas/login?next=x"), fake=fake)
        assert report.stopped == "signed out" and len(calls) == 1 and "daemon.py --login" in log
        assert store.outreach_queue_row(first)["state"] == "approved" and store.outreach_queue_row(second)["state"] == "approved"
        assert store.conn.execute("SELECT COUNT(*) FROM recruiter_outreach WHERE recruiter_name = 'First Try'").fetchone()[0] == 0

        # an ordinary failure, and a recruiter whose messages are locked, end that message with the reason
        fake, _ = sender(("message", "failed"), ("inmail", "inmail_locked"))
        report, _ = run_send(store, on(max_per_day=50), page=Page("https://www.linkedin.com/in/first-try-demo"), fake=fake)
        assert (report.sent, report.not_sent) == (0, 2)
        assert (store.outreach_queue_row(first)["state"], store.outreach_queue_row(first)["outcome"]) == ("not_sent", "failed")
        assert store.outreach_queue_row(second)["outcome"] == "inmail_locked"
        store.close()


def test_the_daemon_sends_the_approved_messages_after_the_looks_and_a_failure_never_fails_the_cycle():
    with tmp_cwd() as tmp:
        cfg = tmp / "config.yaml"
        cfg.write_text(f'daemon:\n  state_db: "{(tmp / "d.db").as_posix()}"\nstage1:\n  target_urls:\n    - "https://jobs.lever.co/netco"\n', encoding="utf-8")
        (tmp / "profile.json").write_text(json.dumps(dict(PROFILE, mode="dry_run")), encoding="utf-8")
        d = Daemon(config_path=str(cfg), profile_path=str(tmp / "profile.json"))
        order = []
        with patch("daemon.fetch_lever_jobs", return_value=[]), patch("daemon.stage3_run", side_effect=lambda *a, **k: order.append("stage3")), \
                patch("daemon.reverify.run", side_effect=lambda *a, **k: order.append("reverify")), \
                patch("daemon.outreach_queue.send_approved", side_effect=lambda *a, **k: order.append("outreach")) as send:
            d.run_cycle(dry_run=True)
            assert order == ["stage3"], "a dry run sends nothing"
            d.run_cycle(dry_run=False)
            assert order == ["stage3", "stage3", "reverify", "outreach"]
            assert send.call_args.args[0] is d.state
            send.side_effect = RuntimeError("profile is locked")
            out = io.StringIO()
            with redirect_stdout(out):
                d.run_cycle(dry_run=False)
        assert "[Outreach] Could not send the approved messages (RuntimeError: profile is locked)" in out.getvalue()
        assert [r["status"] for r in d.state.conn.execute("SELECT status FROM runs ORDER BY id")] == ["completed"] * 3
        d.state.close()


# --------------------------------------------------------------------------- the command line

def cli(tmp, *argv):
    out = io.StringIO()
    with redirect_stdout(out):
        code = outreach_queue.main(["--config", str(tmp / "config.yaml"), *argv])
    return code, out.getvalue()


def test_the_command_line_lists_approves_edits_and_rejects():
    with tmp_cwd() as tmp:
        db = tmp / "s.db"
        (tmp / "config.yaml").write_text(f'daemon:\n  state_db: "{db.as_posix()}"\n', encoding="utf-8")
        store = StateStore(db)
        a, b, c = queued(store), queued(store, SAM, job_id="j2", text="Hi Sam. Best, Alex."), queued(store, RecruiterInfo("Kim Ito", "", "https://www.linkedin.com/in/kim-ito-demo", "x"), job_id="j3")
        store.close()

        code, text = cli(tmp, "list")
        assert code == 0 and "3 message(s) waiting for your OK" in text and "Dana Whitfield" in text and "Sam Okafor" in text
        code, text = cli(tmp, "show", str(a))
        assert "drafted as" not in text and "connect. Best, Alex." in text and f"{len(NOTE)} characters" in text and DANA.profile_url in text
        assert cli(tmp, "show", "999")[0] == 1

        code, text = cli(tmp, "approve", str(a))
        assert code == 0 and "approved" in text and "outreach.enabled is false" in text, "approving says that nothing goes out while outreach is off"
        code, text = cli(tmp, "approve", str(b), "--text", "Hi Sam, my own words. Best, Alex.")
        assert code == 0
        code, text = cli(tmp, "approve", str(c), "--text", "x" * 301)
        assert code == 1 and "301 characters" in text
        code, text = cli(tmp, "show", str(b))
        assert "my own words" in text and "drafted as: Hi Sam. Best, Alex." in text
        assert cli(tmp, "approve", str(a))[0] == 1, "already approved"
        assert cli(tmp, "approve")[0] == 1 and cli(tmp, "approve", str(c), "--all")[0] == 1

        code, text = cli(tmp, "reject", str(b))
        assert code == 0 and cli(tmp, "reject", str(b))[0] == 1
        code, text = cli(tmp, "list")
        assert "1 message(s) waiting" in text and "1 approved, sent by the daemon after its next cycle" in text

        code, text = cli(tmp, "approve", "--all")
        assert code == 0 and "Approved 1 message(s)." in text
        code, text = cli(tmp, "status")
        assert "outreach is OFF" in text and "approved, not sent yet: 2" in text and "turned down: 1" in text and "of 5" in text
        (tmp / "config.yaml").write_text(f'daemon:\n  state_db: "{db.as_posix()}"\noutreach:\n  enabled: true\n  approval: false\n', encoding="utf-8")
        code, text = cli(tmp, "status")
        assert "outreach is ON" in text and "SENDS WITHOUT ASKING" in text


# --------------------------------------------------------------------------- the web interface and the pending screen

def test_the_web_interface_lists_the_drafts_and_approves_the_edited_text():
    with project() as desk:
        ib = desk.inbox()
        assert [m["recruiter"] for m in ib["outreach"]] == ["Dana Whitfield", "Sam Okafor"] and ib["counts"]["outreach"] == 2
        assert ib["outreach"][0]["profile"].startswith("https://www.linkedin.com/in/") and ib["outreach"][0]["limit"] == 300
        first, second = ib["outreach"][0]["id"], ib["outreach"][1]["id"]

        assert "at most 300" in raises(desk.act, "outreach", {"id": first, "decision": "send", "text": "x" * 301})
        assert "empty" in raises(desk.act, "outreach", {"id": first, "decision": "send", "text": "  "})
        assert "Choose send or reject" in raises(desk.act, "outreach", {"id": first, "decision": "maybe"})
        assert "Which message" in raises(desk.act, "outreach", {"id": "abc", "decision": "send", "text": "hi"})
        with desk.reader() as s:
            assert len(s.outreach_queue_rows("awaiting")) == 2, "refused requests changed nothing"

        done = desk.act("outreach", {"id": first, "decision": "send", "text": "Hi Dana, my own words. Best, Alex."})
        assert done["ok"] and "outreach.enabled is false" in done["message"], "the demo project has outreach off, and the reply says what that means"
        with desk.reader() as s:
            row = s.outreach_queue_row(first)
        assert row["state"] == "approved" and row["message_text"] == "Hi Dana, my own words. Best, Alex." and row["drafted_text"].startswith("Hi Dana, I applied")
        assert "not waiting any more" in raises(desk.act, "outreach", {"id": first, "decision": "send", "text": "again"})

        assert desk.act("outreach", {"id": second, "decision": "reject"})["ok"]
        after = desk.inbox()
        assert after["outreach"] == [] and after["counts"]["outreach_approved"] == 1 and after["counts"]["needs_you"] == ib["counts"]["needs_you"] - 2
        assert "not waiting any more" in raises(desk.act, "outreach", {"id": second, "decision": "reject"})


def test_the_page_has_a_section_with_an_editable_message_and_builds_nothing_from_markup():
    page = INDEX.read_text(encoding="utf-8")
    assert "function outreachCard" in page and 'action: "outreach"' in page and "Messages to recruiters" in page and "Send this" in page and "Don't send" in page
    card = page[page.index("function outreachCard"):page.index("function fitCard")]
    assert "innerHTML" not in card and "<textarea" not in card, "the message is put in with textContent-style helpers, never as markup"


def test_the_pending_screen_lists_messages_waiting_for_an_ok():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        root = demo.build(Path(d) / "p")
        snap = data.snapshot(root, with_ollama=False)
        waiting = data.outreach_waiting(root)
        assert [m.recruiter for m in waiting] == ["Dana Whitfield", "Sam Okafor"]
        text = render(snap, [], color=False, width=100, outreach=waiting)
        assert "2 messages to recruiters waiting for your OK (nothing is sent without it)" in text and "python outreach_queue.py list" in text
        assert "Dana Whitfield @ Contoso Labs" in text
        assert "2 messages to recruiters" not in render(snap, [], color=False, width=100), "without the list the screen is as before"
        assert data.outreach_waiting(Path(d) / "nowhere") == [], "no project, no list"

        lonely = Path(d) / "lonely"
        lonely.mkdir()
        store = StateStore(lonely / "state.db")
        queued(store)
        store.close()
        snap = data.snapshot(lonely, with_ollama=False)
        alone = render(snap, [], color=False, width=100, outreach=data.outreach_waiting(lonely))
        assert "1 message to recruiters waiting for your OK" in alone and "Nothing is waiting for you." not in alone, "a waiting message is something waiting"
        assert "Nothing is waiting for you." in render(snap, [], color=False, width=100)
