"""A file picker that is not a plain file field: a page that makes its file input only when a button is pressed.

The agent used to see only ``<input type="file">`` elements, so a resume button like this one left the form without its resume. It now presses an
upload button that names the resume (or cover letter), hands the file to the picker that opens, and leaves every other upload button alone.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent import ApplyAgent
from page_state import Action, build_snapshot, classify_action, upload_button_kind, upload_kind
from tests.test_p3_agent import agent_env, raw_frame, skipped

RESUME_BYTES = b"%PDF-1.4 a pretend resume"


class Picker:
    """A session that can press upload buttons and records what it was asked to do."""

    def __init__(self, shown=True, fail=None):
        self.pressed: list[tuple[str, str]] = []
        self.filled: list[tuple[str, str]] = []
        self.shown, self.fail = shown, fail

    def current_url(self):
        return "https://careers.acme.com/jobs/1"

    def upload_file(self, field, path):
        self.filled.append((field.label, path))

    def upload_via_button(self, action, path):
        self.pressed.append((action.label, path))
        if self.fail:
            raise RuntimeError(self.fail)
        return self.shown


def snapshot_with(buttons, files=()):
    frame = raw_frame(actions=[(label, True) for label, _ in buttons], files=list(files))
    for raw, (_, near) in zip(frame["actions"], buttons):
        raw["near"] = near
    return build_snapshot([frame], [])


def agent_for(session, resume):
    return ApplyAgent(session, {"attachments": {"resume_path": str(resume), "cover_letter_path": str(resume)}},
                      {"title": "Backend Engineer", "company": "Acme"}, solve=lambda: None, cfg={"consensus": 2})


# --------------------------------------------------------------------------- no browser: which buttons, which document

def test_a_button_is_for_the_document_its_own_words_or_its_surroundings_name():
    for label, near, kind in (
        ("Upload resume", "", "resume"), ("Upload your CV", "", "resume"), ("Attach cover letter", "", "cover_letter"),
        ("Upload", "Resume/CV * Upload", "resume"), ("Browse", "Cover letter (optional) Browse", "cover_letter"),
        ("Upload", "Resume Cover letter Upload Upload", "other"),                  # a wrapper holding both says nothing about this button
        ("Upload photo", "Profile photo (optional) Upload photo", "other"), ("Attach", "", "other"), ("Upload", "", "other"),
        ("Upload resume", "Cover letter Upload resume", "resume"),                  # its own words win over its surroundings
    ):
        assert upload_button_kind(label, near) == kind, (label, near)
    assert upload_kind("Resume/CV") == "resume" and upload_kind("Cover letter") == "cover_letter" and upload_kind("Photo") == "other"


def test_upload_buttons_and_dropzones_are_recognised_as_uploads_and_never_as_the_way_forward():
    for label in ("Upload resume", "Attach", "Drop your file here, or click to browse", "Choose file", "Add your resume"):
        assert classify_action(Action(1, label), True) == "upload", label
    assert classify_action(Action(1, "Submit application"), True) == "submit"
    assert classify_action(Action(1, "Next"), True) == "next"


def test_the_agent_presses_only_the_buttons_it_has_a_file_for():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        resume = Path(d) / "resume.pdf"
        resume.write_bytes(RESUME_BYTES)

        session = Picker()
        agent = agent_for(session, resume)
        snap = snapshot_with([("Upload resume", ""), ("Upload photo", "Profile photo"), ("Attach", ""), ("Upload", "Resume/CV * Upload")])
        agent._upload_files(snap)
        assert [label for label, _ in session.pressed] == ["Upload resume", "Upload"], session.pressed          # not the photo, not the unlabelled one
        assert all(path == str(resume) for _, path in session.pressed)
        assert agent.history == ["attached the resume with the 'Upload resume' button", "attached the resume with the 'Upload' button"]

        agent._upload_files(snap)                                                                              # the buttons are still there: not pressed twice
        assert len(session.pressed) == 2

        session = Picker()
        agent_for(session, resume)._upload_files(snapshot_with(
            [("Upload resume", "")], files=[dict(id=1, label="Resume/CV", required=True, has_file=False)]))
        assert session.pressed == [], "a real file field for the resume exists: it is used, the button is left alone"
        assert session.filled == [("Resume/CV", str(resume))]

        session = Picker()
        agent_for(session, Path(d) / "missing.pdf")._upload_files(snapshot_with([("Upload resume", "")]))
        assert session.pressed == [], "no file to give: nothing to press"

        class Plain:                                                                                           # a session that cannot press buttons
            def current_url(self):
                return "https://careers.acme.com/jobs/1"

        plain = agent_for(Plain(), resume)
        plain._upload_files(snapshot_with([("Upload resume", "")]))
        assert plain.history == []


def test_a_picker_that_fails_or_shows_nothing_is_noted_and_does_not_stop_the_agent():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        resume = Path(d) / "resume.pdf"
        resume.write_bytes(RESUME_BYTES)
        agent = agent_for(Picker(fail="no file chooser opened"), resume)
        agent._upload_files(snapshot_with([("Upload resume", "")]))
        assert agent.history == ["could not attach the resume with the 'Upload resume' button: no file chooser opened"]
        agent = agent_for(Picker(shown=False), resume)
        agent._upload_files(snapshot_with([("Upload resume", "")]))
        assert agent.history == ["attached the resume with the 'Upload resume' button (the page did not show its name)"]


def test_an_unfilled_picker_makes_the_agent_fill_the_page_instead_of_clicking_on():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        resume = Path(d) / "resume.pdf"
        resume.write_bytes(RESUME_BYTES)
        agent = agent_for(Picker(), resume)
        snap = snapshot_with([("Upload resume", ""), ("Next", True)])
        assert agent._decide(snap, "s1").kind == "fill"
        agent._upload_files(snap)
        assert agent._decide(snap, "s1").kind == "click", "once the button has been pressed the page is not filled in again"


# --------------------------------------------------------------------------- real browser, fake application site

def _run(env, path, **fresh):
    """(scenario, how many times the page's picker was opened while the form was being filled in, the submit result)."""
    sc = env.fresh(**fresh)
    sc.run(path)
    picks = sc.session.page.evaluate("window.picks || 0")
    return sc, picks, sc.handler.submit()


def test_a_custom_resume_button_gets_the_resume_and_the_application_goes_through():
    with agent_env() as env:
        if skipped(env):
            return
        for path in ("/apply/picker", "/apply/picker/generic", "/apply/picker/dropzone"):
            sc, picks, res = _run(env, path)
            assert res.ok and res.confirmed, (path, res.evidence)
            assert len(env.ats.applications) == 1, path
            sent = env.ats.applications[0]
            assert sent["resume_name"] == "resume.pdf" and sent["resume_size"] == len(RESUME_BYTES), (path, sent)
            assert picks == 1, f"{path}: the picker must be opened exactly once"
            steps = [json.loads(line) for line in (sc.handler.trace_dir / "steps.jsonl").read_text(encoding="utf-8").splitlines()]
            assert any(h.startswith("attached the resume with the") for step in steps for h in step["so_far"]), f"{path}: the trace must say what was attached"
            history = sc.handler._agent.history
            assert any(h.startswith("attached the resume with the") and h.endswith("button") for h in history), history     # and the page showed the name


def test_an_upload_button_for_something_else_is_never_pressed_and_without_a_resume_nothing_is_sent():
    with agent_env() as env:
        if skipped(env):
            return
        sc, picks, res = _run(env, "/apply/picker/photo")
        assert not res.confirmed and env.ats.applications == []
        assert picks == 0, "the photo button must not be pressed"

        sc, picks, res = _run(env, "/apply/picker", with_resume=False)
        assert not res.confirmed and env.ats.applications == []
        assert picks == 0, "with no resume configured there is nothing to give the picker"
