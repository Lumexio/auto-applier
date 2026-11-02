"""The web interface for what needs a person: what it lists, what each button changes in the database, and who may press them.

The buttons do what ``answers.py`` / ``fit_review.py`` / ``sites.py`` already do; these tests hold them to that, and hold the
HTTP side to its promises (this computer only, a token for everything, no markup built from data, no file outside the two kinds
a job's folder holds). All names are made up.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Iterator

from gui import demo
from gui.__main__ import already_serving
from gui.server import APP_NAME, DeskServer
from gui.service import ActionError, Desk, safe_url
from state import StateStore

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "gui" / "static" / "index.html"


@contextlib.contextmanager
def project() -> Iterator[Desk]:
    tmp = tempfile.mkdtemp(prefix="desk-test-")
    try:
        yield Desk(demo.build(Path(tmp) / "p"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def application(desk: Desk, job_id: str) -> dict | None:
    with desk.reader() as s:
        row = s.conn.execute("SELECT * FROM applications WHERE job_id = ?", (job_id,)).fetchone()
        return dict(row) if row else None


def raises(fn, *args, **kwargs) -> str:
    try:
        fn(*args, **kwargs)
    except ActionError as e:
        return str(e)
    raise AssertionError("expected an ActionError")


def question(desk: Desk, text_part: str) -> dict:
    return next(x for x in desk.inbox()["questions"] if text_part in x["text"].lower())


# --------------------------------------------------------------------------- what is listed

def test_inbox_lists_everything_that_needs_a_person_and_counts_it():
    with project() as desk:
        ib = desk.inbox()
        assert ib["db"] is True
        texts = {x["text"] for x in ib["questions"]}
        assert "What do you use to store tracking data?" in texts and "Are you willing to relocate?" in texts
        tracking = question(desk, "tracking")
        assert len(tracking["jobs"]) == 2 and {j["company"] for j in tracking["jobs"]} == {"Hooli", "Initech"}
        assert tracking["proposal_verified"] is True and "PostgreSQL" in tracking["proposal"] and tracking["proposal_basis"]
        hooli = question(desk, "join hooli")
        assert hooli["scope"] == "hooli" and hooli["proposal_verified"] is False                 # an essay naming the company: not checked, for Hooli only
        english = question(desk, "english level")
        assert english["options"] == ["Basic", "Intermediate", "Professional", "Native"] and english["proposal"] == ""
        assert {s["site"] for s in ib["probation"]} >= {"ashby"}
        ashby = next(s for s in ib["probation"] if s["site"] == "ashby")
        assert {j["job_id"] for j in ashby["jobs"]} == {"p1", "p2"} and all(j["snapshot"] for j in ashby["jobs"])
        assert ashby["clean_runs"] == 2 and ashby["needed"] == 3 and ashby["jobs"][0]["answered"] == 2 and ashby["jobs"][0]["unanswered"] == 1
        assert {p["job_id"] for p in ib["apply_by_hand"]} == {"e1", "e2"} and {p["job_id"] for p in ib["verify"]} == {"c1x"}
        assert {r["job_id"] for r in ib["unconfirmed"]} >= {"x1", "x2"} and {r["job_id"] for r in ib["failed"]} >= {"f2", "f3"}
        disputed = [f for f in ib["fit"] if f["where"] == "disputed"]
        assert len(disputed) == 1 and disputed[0]["review"]["verdict"] == "apply" and disputed[0]["deal_breakers"]
        c = ib["counts"]
        assert c["questions"] == len(ib["questions"]) and c["fit"] == 1 and c["apply_by_hand"] == 2 and c["verify"] == 1
        assert c["needs_you"] == c["questions"] + c["fit"] + c["apply_by_hand"] + c["verify"] + c["other"] + len(ib["probation"])


def test_what_is_parked_for_other_reasons_is_not_listed_twice():
    with project() as desk:
        ib = desk.inbox()
        parked = {p["job_id"] for p in ib["apply_by_hand"] + ib["verify"] + ib["other"]}
        assert not parked & {"q1", "q2", "q3", "p1", "p2", "v1"}       # waiting on answers, on probation, or on a fit decision: each has its own list


def test_an_empty_folder_is_a_page_not_an_error():
    tmp = tempfile.mkdtemp(prefix="desk-empty-")
    try:
        desk = Desk(tmp)
        assert desk.inbox()["db"] is False and desk.sites() == [] and desk.answers() == []
        assert "no database" in raises(desk.act, "answer", {"key": "k", "text": "x"}).lower()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_reading_never_writes():
    with project() as desk:
        with desk.reader() as s:
            try:
                s.conn.execute("DELETE FROM applications")
            except sqlite3.OperationalError:
                pass
            else:
                raise AssertionError("the read-only view accepted a write")
        before = desk.inbox()
        desk.overview()
        desk.sites()
        desk.answers()
        desk.job("p1")
        assert desk.inbox() == before


def test_a_database_from_before_drafts_existed_is_brought_up_to_date():
    with project() as desk:
        conn = sqlite3.connect(desk.db_path)
        for column in ("proposal", "proposal_basis", "proposal_verified", "proposal_note", "proposal_scope"):
            conn.execute(f"ALTER TABLE pending_questions DROP COLUMN {column}")
        conn.commit()
        conn.close()
        reopened = Desk(desk.root)                                      # migrates once, so the read-only queries never meet a missing column
        assert any(x["text"].startswith("What do you use") for x in reopened.inbox()["questions"])


def test_links_are_only_ever_http_or_https():
    assert safe_url("https://boards.example.test/jobs/1") == "https://boards.example.test/jobs/1"
    assert safe_url("http://x.test/a?b=1") == "http://x.test/a?b=1"
    for bad in ("javascript:alert(1)", "JAVASCRIPT:alert(1)", "data:text/html,<script>", "file:///c:/x", "//evil.test", "", None, "https://a b"):
        assert safe_url(bad) == "", bad
    with project() as desk:
        with desk.writer() as s:
            s.conn.execute("UPDATE jobs SET apply_url = 'javascript:alert(1)' WHERE id = 'e1'")
            s.conn.commit()
        assert next(p for p in desk.inbox()["apply_by_hand"] if p["job_id"] == "e1")["url"] == ""


# --------------------------------------------------------------------------- answering

def test_an_answer_is_saved_for_every_company_and_the_waiting_jobs_are_queued_again():
    with project() as desk:
        tracking = question(desk, "tracking")
        out = desk.act("answer", {"key": tracking["key"], "scope": tracking["scope"], "text": "  PostgreSQL and Redis  "})
        assert out["queued"] == 1                                         # Initech's job had nothing else to wait for; Hooli's still waits for its essay
        with desk.reader() as s:
            assert s.get_answer(tracking["key"]) == "PostgreSQL and Redis"
        assert application(desk, "q2") is None and application(desk, "q1")["status"] == "needs_review"
        assert not any("tracking" in x["text"].lower() for x in desk.inbox()["questions"])
        assert next(x for x in desk.inbox()["questions"] if "hooli" in x["text"].lower())["jobs"][0]["job_id"] == "q1"


def test_an_answer_that_names_a_company_is_saved_for_that_company_only():
    with project() as desk:
        hooli = question(desk, "join hooli")
        desk.act("answer", {"key": hooli["key"], "scope": hooli["scope"], "text": "Because of the order tracking work."})
        with desk.reader() as s:
            assert s.get_answer(hooli["key"] + "@hooli") == "Because of the order tracking work."
            assert s.get_answer(hooli["key"]) is None                     # another company asking the same is NOT given a Hooli answer
        assert application(desk, "q1")["status"] == "needs_review"


def test_answering_for_one_company_leaves_the_same_question_waiting_for_the_others():
    with project() as desk:
        hooli = question(desk, "join hooli")
        with desk.writer() as s:
            s.add_pending_questions("q3", [{"label": hooli["text"], "kind": "textarea", "reason": "an essay"}])     # another company's job asks the same, with no draft
        asked_about = lambda: [x for x in desk.inbox()["questions"] if "join hooli" in x["text"].lower()]           # noqa: E731
        assert len(asked_about()) == 2
        desk.act("answer", {"key": hooli["key"], "scope": "hooli", "text": "Because."})
        left = asked_about()
        assert len(left) == 1 and left[0]["scope"] == "" and [j["job_id"] for j in left[0]["jobs"]] == ["q3"]
        desk.act("answer", {"key": hooli["key"], "scope": "", "text": "Because of the people."})                    # an answer for every company clears every copy
        assert asked_about() == []


def test_the_answer_must_be_one_of_the_options_the_form_offers():
    with project() as desk:
        english = question(desk, "english level")
        assert "Professional" in raises(desk.act, "answer", {"key": english["key"], "scope": "", "text": "Fluent-ish"})
        desk.act("answer", {"key": english["key"], "scope": "", "text": "professional"})
        with desk.reader() as s:
            assert s.get_answer(english["key"]) == "Professional"         # the form's own spelling, whatever was typed


def test_answers_are_checked_before_they_are_saved():
    with project() as desk:
        tracking = question(desk, "tracking")
        base = {"key": tracking["key"], "scope": ""}
        assert "first" in raises(desk.act, "answer", {**base, "text": "   "})
        assert "longer" in raises(desk.act, "answer", {**base, "text": "x" * 4001})
        assert "no longer waiting" in raises(desk.act, "answer", {"key": "not a real question", "scope": "", "text": "x"})
        assert "no longer waiting" in raises(desk.act, "answer", {**base, "scope": "someone else", "text": "x"})    # the wrong company's copy
        with desk.reader() as s:
            assert s.get_answer(tracking["key"]) is None


def test_skipping_a_question_ends_the_jobs_it_was_holding_up_for_good():
    with project() as desk:
        relocate = question(desk, "relocate")
        out = desk.act("skip_question", {"key": relocate["key"], "scope": ""})
        assert "1 job" in out["message"]
        row = application(desk, "q3")
        assert row["status"] == "skipped" and row["error_code"] == "declined"
        with desk.reader() as s:
            assert s.is_already_submitted("https://lever.example.test/q3")      # no queue picks it up again
            assert not s.conn.execute("SELECT 1 FROM pending_questions WHERE job_id = 'q3'").fetchall()        # its other question went with it
        assert "no longer waiting" in raises(desk.act, "skip_question", {"key": relocate["key"], "scope": ""})


def test_saved_answers_can_be_edited_added_and_forgotten():
    with project() as desk:
        key = "are you legally authorized to work in mexico"
        desk.act("save_answer", {"key": key, "text": "Yes, with a work permit"})
        assert next(a for a in desk.answers() if a["key"] == key)["answer"] == "Yes, with a work permit"
        desk.act("save_answer", {"question": "Do you have a valid passport?", "text": "Yes"})                 # a new one, before a form asks
        assert any(a["key"] == "do you have a valid passport" and a["answer"] == "Yes" for a in desk.answers())
        assert "question" in raises(desk.act, "save_answer", {"question": "  ?!  ", "text": "Yes"})
        desk.act("forget_answer", {"key": key})
        assert not any(a["key"] == key for a in desk.answers())
        assert "not saved" in raises(desk.act, "forget_answer", {"key": key})


def test_saving_an_answer_for_a_waiting_question_from_the_answers_page_releases_its_jobs_too():
    with project() as desk:
        english = question(desk, "english level")
        assert "Professional" in raises(desk.act, "save_answer", {"question": english["text"], "text": "Fluent-ish"})      # the same option check as answering from the page
        desk.act("save_answer", {"question": english["text"], "text": "professional"})
        assert not any("english level" in x["text"].lower() for x in desk.inbox()["questions"])
        with desk.reader() as s:
            assert s.get_answer(english["key"]) == "Professional"


# --------------------------------------------------------------------------- the decisions about a job

def test_fit_decisions_do_what_fit_review_does():
    with project() as desk:
        out = desk.act("fit", {"job_id": "v1", "decision": "apply"})
        assert "queued again" in out["message"]
        with desk.reader() as s:
            assert s.get_fit_override("v1") == "apply"
        assert application(desk, "v1") is None                              # forgotten, so it is judged again and the override applies
        assert "not waiting" in raises(desk.act, "fit", {"job_id": "v1", "decision": "apply"})
        desk.act("fit", {"job_id": "v2", "decision": "skip"})
        assert application(desk, "v2")["status"] == "skipped" and application(desk, "v2")["error_code"] == "not_a_fit"
        with desk.reader() as s:
            assert s.get_fit_override("v2") == "skip"
        assert not any(f["job_id"] in ("v1", "v2") for f in desk.inbox()["fit"])
        assert "apply or skip" in raises(desk.act, "fit", {"job_id": "v3", "decision": "maybe"})


def test_rejecting_a_disputed_or_postponed_veto_makes_the_skip_final():
    with project() as desk:
        assert application(desk, "v1")["status"] == "needs_review"               # the models disagree: parked until you decide
        desk.act("fit", {"job_id": "v1", "decision": "skip"})
        row = application(desk, "v1")
        assert row["status"] == "skipped" and row["error_code"] == "not_a_fit" and "rejected" in row["error_message"]
        with desk.reader() as s:
            assert s.is_already_submitted("https://greenhouse.example.test/v1")
        assert application(desk, "v3")["error_code"] is None                       # a postponed veto would be judged again...
        desk.act("fit", {"job_id": "v3", "decision": "skip"})
        assert application(desk, "v3")["error_code"] == "not_a_fit"                # ...until you say it is final


def test_trusting_a_site_releases_the_jobs_parked_on_probation():
    with project() as desk:
        out = desk.act("trust_site", {"site": "Ashby"})
        assert out["released"] == 2 and application(desk, "p1") is None and application(desk, "p2") is None
        assert not any(s["site"] == "ashby" for s in desk.inbox()["probation"])
        row = next(s for s in desk.sites() if s["site"] == "ashby")
        assert row["trusted"] is True and row["approved"] is True
        desk.act("distrust_site", {"site": "ashby"})
        assert next(s for s in desk.sites() if s["site"] == "ashby")["trusted"] is False
        assert "Which site" in raises(desk.act, "trust_site", {"site": " "})


def test_a_site_that_became_trusted_by_itself_still_lists_its_held_jobs_for_release():
    with project() as desk:
        with desk.writer() as s:
            s.approve_site("ashby")                                         # trusted, but p1 and p2 are still parked from before
        ashby = next(p for p in desk.inbox()["probation"] if p["site"] == "ashby")
        assert ashby["trusted"] is True and len(ashby["jobs"]) == 2
        assert desk.act("trust_site", {"site": "ashby"})["released"] == 2


def test_what_you_say_about_a_job_is_recorded_exactly():
    with project() as desk:
        desk.act("job", {"job_id": "e1", "decision": "applied"})
        row = application(desk, "e1")
        assert row["status"] == "submitted" and row["approved_by_user"] == 1 and row["submitted_at"] and row["error_code"] is None
        with desk.reader() as s:
            assert s.is_already_submitted("https://linkedin.example.test/e1")        # never tried again
        desk.act("job", {"job_id": "x1", "decision": "retry"})
        assert application(desk, "x1") is None                                       # forgotten, so the next cycle queues it
        desk.act("job", {"job_id": "f2", "decision": "skip"})
        row = application(desk, "f2")
        assert row["status"] == "skipped" and row["error_code"] == "declined"
        with desk.reader() as s:
            assert s.is_already_submitted("https://linkedin.example.test/f2")
        for gone in ("e1", "x1", "f2"):
            assert not any(r["job_id"] == gone for k in ("apply_by_hand", "unconfirmed", "failed") for r in desk.inbox()[k])


def test_a_decision_cannot_touch_an_application_that_is_running_or_done():
    with project() as desk:
        assert application(desk, "n1")["status"] == "in_progress" and application(desk, "g1")["status"] == "submitted"
        for job_id in ("n1", "g1", "no-such-job"):
            for decision in ("applied", "retry", "skip"):
                assert "not waiting" in raises(desk.act, "job", {"job_id": job_id, "decision": decision})
        assert application(desk, "n1")["status"] == "in_progress" and application(desk, "g1")["status"] == "submitted"
        assert "applied, retry or skip" in raises(desk.act, "job", {"job_id": "e1", "decision": "delete"})


def test_unknown_actions_and_odd_names_are_refused():
    with project() as desk:
        for name in ("", "nope", "__init__", "answer; drop", "../answer", "ANSWER", None):
            assert "Unknown action" in raises(desk.act, name, {})
        assert application(desk, "e1")["status"] == "needs_review"


# --------------------------------------------------------------------------- one job, and its pictures

def test_job_detail_has_the_picture_the_answers_and_the_posting():
    with project() as desk:
        d = desk.job("p1")
        assert d["shots"] == ["form_snapshot.png"] and d["probation"]["site"] == "ashby" and d["fit"]["score"] == 0.86
        assert [list(a)[0] for a in d["answers"]] == ["answered", "answered", "unanswered"] and "PostgreSQL" in d["description"]
        assert "No such job" in raises(desk.job, "does-not-exist")


def test_the_picture_name_pattern_itself_refuses_anything_that_could_climb():
    from gui.service import _FILE_NAME

    for ok in ("form_snapshot.png", "trace/step_01.png", "trace/step_12_b.png"):
        assert _FILE_NAME.match(ok), ok
    for bad in ("trace/step_/../../p2/form_snapshot.png", "trace/step_../x.png", "trace/step_a/b.png", "trace\\step_01.png", "trace/step_01.png\n",
                "form_snapshot.png/", "x/form_snapshot.png", "trace/step_" + "a" * 41 + ".png", "form_snapshot.PNG"):
        assert not _FILE_NAME.match(bad), bad                                   # the folder check is a second line of defence, not the first


def test_only_the_two_kinds_of_picture_in_a_jobs_own_folder_can_be_fetched():
    with project() as desk:
        trace = desk.root / "applications" / "p1" / "trace"
        trace.mkdir()
        (trace / "step_01.png").write_bytes(b"\x89PNG")
        (trace / "steps.jsonl").write_text("{}", encoding="utf-8")
        (desk.root / "secret.png").write_bytes(b"x")
        assert desk.file("p1", "form_snapshot.png") and desk.file("p1", "trace/step_01.png")
        for name in ("answers.json", "probation.json", "trace/steps.jsonl", "../p2/form_snapshot.png", "..\\p2\\form_snapshot.png",
                     "trace/../answers.json", "trace/step_01.png/../../answers.json", "/etc/passwd", "C:\\x.png", "form_snapshot.png\x00.txt",
                     "trace/step_%2e%2e.png", "", None):
            assert desk.file("p1", name) is None, name
        assert desk.file("../../secret", "form_snapshot.png") is None            # a job id cannot climb out either
        assert desk.file("p1", "trace/step_09.png") is None                      # a name that matches but is not there
        assert desk.file("p2", "trace/step_01.png") is None


def _link_dir(link: Path, target: Path) -> bool:
    import os

    try:
        if os.name == "nt":
            return subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True).returncode == 0
        os.symlink(target, link, target_is_directory=True)
        return True
    except OSError:
        return False


def test_a_linked_folder_cannot_lead_a_picture_request_outside_the_jobs_folder():
    with project() as desk:
        outside = Path(tempfile.mkdtemp(prefix="desk-outside-"))
        try:
            (outside / "step_01.png").write_bytes(b"\x89PNG")
            if not _link_dir(desk.root / "applications" / "p1" / "trace", outside):
                return                                                      # no permission to make a link on this machine
            assert desk.file("p1", "trace/step_01.png") is None
        finally:
            shutil.rmtree(outside, ignore_errors=True)


# --------------------------------------------------------------------------- over HTTP

@contextlib.contextmanager
def served() -> Iterator[tuple[DeskServer, Desk]]:
    with project() as desk:
        server = DeskServer(desk, port=0)
        thread = server.serve_in_thread()
        try:
            yield server, desk
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def call(server: DeskServer, method: str, path: str, *, headers: dict | None = None, body: bytes | None = None, host: str | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    try:
        hdrs = {"Host": host or f"127.0.0.1:{server.server_port}", **(headers or {})}
        conn.request(method, path, body=body, headers=hdrs)
        r = conn.getresponse()
        return r.status, dict(r.getheaders()), r.read()
    finally:
        conn.close()


def token_of(server: DeskServer) -> str:
    status, _, page = call(server, "GET", "/")
    assert status == 200
    return re.search(r'const TOKEN = "([^"]+)"', page.decode("utf-8")).group(1)


def act(server: DeskServer, token: str, payload: object, **extra) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json", "X-Desk-Token": token, **extra.pop("headers", {})}
    status, _, body = call(server, "POST", "/api/act", headers=headers, body=json.dumps(payload).encode("utf-8"), **extra)
    return status, json.loads(body or b"{}")


def test_the_page_carries_its_token_and_a_policy_that_forbids_loading_anything_else():
    with served() as (server, _):
        status, headers, page = call(server, "GET", "/")
        text = page.decode("utf-8")
        assert status == 200 and "__TOKEN__" not in text and server.token in text
        csp = headers["Content-Security-Policy"]
        assert "default-src 'none'" in csp and "connect-src 'self'" in csp and "frame-ancestors 'none'" in csp and "http" not in csp
        assert headers["Cache-Control"] == "no-store" and headers["X-Content-Type-Options"] == "nosniff" and headers["Referrer-Policy"] == "no-referrer"


def test_every_endpoint_but_the_page_and_the_ping_needs_the_token():
    with served() as (server, _):
        assert json.loads(call(server, "GET", "/api/ping")[2]) == {"app": APP_NAME}
        for path in ("/api/inbox", "/api/overview", "/api/sites", "/api/answers", "/api/job?id=p1", "/file?job=p1&name=form_snapshot.png"):
            assert call(server, "GET", path)[0] == 403, path
            assert call(server, "GET", path, headers={"X-Desk-Token": "wrong"})[0] == 403, path
        token = token_of(server)
        status, _, body = call(server, "GET", "/api/inbox", headers={"X-Desk-Token": token})
        assert status == 200 and json.loads(body)["counts"]["needs_you"] > 0
        status, headers, body = call(server, "GET", f"/file?job=p1&name=form_snapshot.png&t={token}")      # an <img> cannot send a header: the token rides in the address
        assert status == 200 and body.startswith(b"\x89PNG") and headers["Content-Type"] == "image/png"
        assert call(server, "GET", f"/file?job=p1&name=answers.json&t={token}")[0] == 404
        assert call(server, "GET", f"/file?job=p1&name=..%2Fp2%2Fform_snapshot.png&t={token}")[0] == 404
        assert call(server, "GET", f"/api/nothing?t={token}")[0] == 404
        assert call(server, "GET", f"/api/job?id=zzz&t={token}")[0] == 404


def test_a_request_for_another_host_name_is_refused_so_a_rebinding_page_learns_nothing():
    with served() as (server, _):
        for host in ("evil.example", f"evil.example:{server.server_port}", "127.0.0.1", f"127.0.0.1:{server.server_port + 1}"):
            assert call(server, "GET", "/", host=host)[0] == 403, host
            assert call(server, "GET", "/api/ping", host=host)[0] == 403, host
        assert call(server, "GET", "/", headers={"Host": ""})[0] == 403
        assert call(server, "GET", "/", host=f"localhost:{server.server_port}")[0] == 200


def test_a_button_press_needs_the_token_a_json_body_and_our_own_origin():
    with served() as (server, desk):
        token = token_of(server)
        body = {"action": "job", "job_id": "e1", "decision": "skip"}
        raw = json.dumps(body).encode("utf-8")
        assert call(server, "POST", "/api/act", headers={"Content-Type": "application/json"}, body=raw)[0] == 403                          # no token
        assert call(server, "POST", "/api/act", headers={"Content-Type": "application/json", "X-Desk-Token": "x"}, body=raw)[0] == 403
        assert act(server, token, body, headers={"Origin": "https://evil.example"})[0] == 403                                             # another site's page
        assert act(server, token, body, headers={"Origin": "null"})[0] == 403
        for ctype in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data", ""):                                       # what a form on another site can send
            status, _, _ = call(server, "POST", "/api/act", headers={"Content-Type": ctype, "X-Desk-Token": token}, body=raw)
            assert status == 415, ctype
        assert application(desk, "e1")["status"] == "needs_review"                                                                         # nothing above did anything
        status, out = act(server, token, body, headers={"Origin": f"http://127.0.0.1:{server.server_port}"})
        assert status == 200 and out["ok"] is True and application(desk, "e1")["status"] == "skipped"


def test_a_bad_request_is_an_error_message_not_a_crash():
    with served() as (server, desk):
        token = token_of(server)
        status, out = act(server, token, {"action": "job", "job_id": "no-such", "decision": "skip"})
        assert status == 400 and "not waiting" in out["error"]
        assert act(server, token, {"action": "nope"})[0] == 400
        assert act(server, token, ["not", "an", "object"])[0] == 400
        assert act(server, token, {"no": "action"})[0] == 400
        headers = {"Content-Type": "application/json", "X-Desk-Token": token}
        assert call(server, "POST", "/api/act", headers=headers, body=b"{not json")[0] == 400
        assert call(server, "POST", "/api/act", headers=headers, body=b"")[0] == 413
        assert call(server, "POST", "/api/act", headers=headers, body=b"{" + b" " * (70 * 1024) + b"}")[0] == 413
        assert call(server, "POST", "/api/other", headers=headers, body=b"{}")[0] == 404
        assert call(server, "GET", "/api/inbox", headers={"X-Desk-Token": token})[0] == 200                  # still serving


def test_two_presses_at_once_do_not_corrupt_anything():
    with served() as (server, desk):
        token = token_of(server)
        results: list[int] = []

        def press(job_id: str) -> None:
            results.append(act(server, token, {"action": "job", "job_id": job_id, "decision": "skip"})[0])

        threads = [threading.Thread(target=press, args=(j,)) for j in ("e1", "e2", "c1x", "x1", "x2", "f2", "f3")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        assert results.count(200) == 7, results
        assert all(application(desk, j)["status"] == "skipped" for j in ("e1", "e2", "c1x", "x1", "x2", "f2", "f3"))


def test_every_start_has_a_token_of_its_own():
    with project() as desk:
        first, second = DeskServer(desk, port=0), DeskServer(desk, port=0)
        try:
            assert first.token != second.token and len(first.token) >= 24
        finally:
            first.server_close()
            second.server_close()


def test_a_failure_inside_a_request_is_reported_and_the_server_keeps_serving():
    with served() as (server, desk):
        token = token_of(server)
        broken = {"X-Desk-Token": token}

        def boom(*_args, **_kwargs):
            raise RuntimeError("boom")

        desk.inbox, desk.act = boom, boom
        status, _, body = call(server, "GET", "/api/inbox", headers=broken)
        assert status == 500 and "RuntimeError: boom" in json.loads(body)["error"]
        status, out = act(server, token, {"action": "job", "job_id": "e1", "decision": "skip"})
        assert status == 500 and "RuntimeError: boom" in out["error"]
        assert call(server, "GET", "/api/sites", headers=broken)[0] == 200                                    # the same thread pool, still answering


def test_a_stranger_on_the_port_is_not_mistaken_for_the_web_interface():
    import http.server

    def stranger(reply: bytes) -> tuple[http.server.HTTPServer, threading.Thread]:
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):                                        # noqa: N802
                self.send_response(200)
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    for reply in (b'{"app": "something-else"}', b"<html>hello</html>", b"{}", b'{"app": null}'):
        server, thread = stranger(reply)
        try:
            assert already_serving(server.server_port) is False, reply
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    server, thread = stranger(json.dumps({"app": APP_NAME}).encode("utf-8"))
    try:
        assert already_serving(server.server_port) is True                                                  # the real answer is recognised
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_it_listens_on_this_computer_only_and_a_second_copy_notices_the_first():
    with project() as desk:
        for host in ("0.0.0.0", "192.168.1.20", "example.test", ""):
            try:
                DeskServer(desk, host=host, port=0)
            except ValueError:
                continue
            raise AssertionError(f"bound to {host!r}")
    with served() as (server, desk):
        assert server.server_address[0] == "127.0.0.1"
        assert already_serving(server.server_port) is True
        try:
            DeskServer(desk, port=server.server_port)
        except OSError:
            pass
        else:
            raise AssertionError("a second server shared the port")
    assert already_serving(1) is False                                       # nothing there


# --------------------------------------------------------------------------- the page itself

def test_the_page_never_turns_data_into_markup():
    html = INDEX.read_text(encoding="utf-8")
    script = html[html.index("<script>"):html.index("</script>")]
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "setTimeout(\"", "srcdoc"):
        assert forbidden not in script, forbidden
    assert html.count("__TOKEN__") == 1
    assert "http://" not in html and "https://" not in html.replace("</script>", "")      # nothing is loaded from elsewhere
    assert re.search(r'target: "_blank", rel: "noopener noreferrer"', script)


def test_the_page_script_is_valid_javascript():
    node = shutil.which("node")
    if not node:
        return                                                                # a syntax check is a bonus where Node exists
    html = INDEX.read_text(encoding="utf-8")
    script = html[html.index("<script>") + len("<script>"):html.index("</script>")]
    tmp = Path(tempfile.mkdtemp(prefix="desk-js-"))
    try:
        (tmp / "page.js").write_text(script, encoding="utf-8")
        out = subprocess.run([node, "--check", str(tmp / "page.js")], capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, out.stderr
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_store_methods_behind_the_buttons():
    with project() as desk, desk.writer() as s:
        assert [j["job_id"] for j in s.jobs_waiting_on("what do you use to store tracking data", "")] == ["q1", "q2"]
        assert [j["job_id"] for j in s.jobs_waiting_on("why do you want to join hooli", "hooli")] == ["q1"]
        assert s.jobs_waiting_on("why do you want to join hooli", "other") == []
        assert s.mark_applied_by_hand("no-such") is False and s.retry_by_hand("no-such") is False and s.skip_by_hand("no-such") is False
        assert s.drop_pending_for_job("q1") == 2 and s.drop_pending_for_job("q1") == 0
        assert s.delete_answer("are you legally authorized to work in mexico") is True and s.delete_answer("nope") is False
        assert {a["question_key"] for a in s.list_answers()} == {"why do you want to join northwind@northwind"}


# --------------------------------------------------------------------------- starting it

def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_gui(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    import sys

    return subprocess.run([sys.executable, "-m", "gui", *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout)


def test_starting_it_twice_opens_the_first_one_and_a_stranger_on_the_port_is_reported():
    import socket
    import sys
    import time

    with project() as desk:
        port = _free_port()
        first = subprocess.Popen([sys.executable, "-m", "gui", "--root", str(desk.root), "--port", str(port)], cwd=ROOT,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            for _ in range(100):
                if already_serving(port):
                    break
                time.sleep(0.1)
            else:
                raise AssertionError("the server did not come up")
            again = _run_gui("--root", str(desk.root), "--port", str(port))
            assert again.returncode == 0 and "Already running" in again.stdout and f":{port}/" in again.stdout, again.stdout + again.stderr
        finally:
            first.kill()
            first.wait(timeout=10)
    with socket.socket() as stranger:
        stranger.bind(("127.0.0.1", 0))
        stranger.listen(1)
        taken = stranger.getsockname()[1]
        out = _run_gui("--port", str(taken))
        assert out.returncode == 1 and "taken by another program" in out.stdout and str(taken + 1) in out.stdout, out.stdout + out.stderr


def test_the_launchers_start_stop_and_report_the_web_interface_where_the_server_listens():
    from gui.__main__ import DEFAULT_PORT

    setup = (ROOT / "setup.ps1").read_text(encoding="utf-8")
    desk = (ROOT / "desk.ps1").read_text(encoding="utf-8")
    assert f"$GuiPort = {DEFAULT_PORT}" in setup and f"http://127.0.0.1:{DEFAULT_PORT}/" in desk
    assert "Start-DeskGui" in setup.split("function Start-AllServices")[1].split("function Stop-AllServices")[0]
    stop = setup.split("function Stop-AllServices")[1].split("function Set-WindowsAutoStart")[0]
    block = stop[stop.index("# 4. Stop the web interface"):]
    assert block.index("Test-OwnProcess") < block.index("Stop-Process"), "a pid file may name a stranger after a reboot: check before stopping it"
    status = setup.split("function Show-DaemonStatus")[1].split("# Handle Command Line Arguments")[0]
    assert "-m gui --port" in status and "Web interface: RUNNING" in status and "Web interface: STOPPED" in status
    assert "[switch]$Gui" in setup and "-or $Gui" in setup.split("$isQuickOp =")[1].split("\n")[0], "-Gui must not run the pip installs first"
    guard = desk.split("if (-not $DaemonOnly -and -not $NoGui) {")[1].split("\n    }")[0]
    assert guard.count('"-Pane", "Gui"') == 2 and f"http://127.0.0.1:{DEFAULT_PORT}/" in guard, "-NoGui and -DaemonOnly must keep both pages closed"
    assert desk.count('"-Pane", "Gui"') == 2, "no browser launch may sit outside the -NoGui guard"
    bat = (ROOT / "gui.bat").read_bytes()
    assert b"\r\n" in bat and b"python -m gui --open %*" in bat and b'cd /d "%~dp0"' in bat
