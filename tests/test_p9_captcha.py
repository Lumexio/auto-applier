"""CAPTCHA detection: stop for a challenge a person must act on, and for nothing else.

An iframe whose address mentions reCAPTCHA / hCaptcha / Turnstile is not a challenge. Greenhouse (and many other sites) load an
INVISIBLE reCAPTCHA on every application form; the old check treated that as a wall and skipped nearly every job from those
sites. It also ran BEFORE the browser went to the new job, so it looked at the previous application's page. These tests use a
real browser and real Google/hCaptcha/Cloudflare-style markup. Nothing here solves or avoids a challenge: when one is on screen
the application is skipped, which is also asserted.
"""

from __future__ import annotations

from unittest.mock import patch

from llm_client import LLMError
from stage3_apply.ats_handlers import BaseHandler, CaptchaEncountered
from stage3_apply.run import stage3_run
from state import StateStore
from tests.fake_ats import FakeATS
from tests.test_p0_safety import app_row, save_job, tmp_cwd
from tests.test_p3_agent import agent_env, expect, profile_for, skipped, stage3_cfg


def probe(env, html: str):
    """What the CAPTCHA check says about a page made of ``html`` in a real browser (frames point at the local fake site)."""
    sc = env.fresh()
    sc.session.page.set_content(html.replace("{b}", env.base))
    return sc.session.captcha_challenges()


def frame(src: str, w: int, h: int, style: str = "") -> str:
    return f'<iframe src="{{b}}{src}" width="{w}" height="{h}" frameborder="0" style="border:0;{style}"></iframe>'


ANCHOR = "/recaptcha/api2/anchor?ar=1&k=KEY&co=aHR0cHM6Ly9ib2FyZHMuZXhhbXBsZTo0NDM.&hl=en&v=abc&{size}&cb=xyz"
BFRAME = "/recaptcha/api2/bframe?hl=en&v=abc&k=KEY"
HCAPTCHA = "/captcha/v1/abc/static/hcaptcha.html#frame={kind}&id=1&host=boards.example.com"
TURNSTILE = "/cdn-cgi/challenge-platform/h/b/turnstile/if/ov2/av0/rcv/abc/0x4AAAA/light/fbE/normal/auto/"


# =========================================================================== things that look like a CAPTCHA and are not one

def test_invisible_captcha_widgets_are_not_challenges():
    with agent_env() as env:
        if skipped(env):
            return
        invisible_src = ANCHOR.format(size="size=invisible")
        plain_src = ANCHOR.format(size="size=normal")
        cases = {
            "an anchor frame that says size=invisible": frame(invisible_src, 304, 78),
            "Google's corner badge (hangs off the right edge)": (
                '<div class="grecaptcha-badge" style="position:fixed;bottom:14px;right:-186px;width:256px;height:60px;overflow:hidden">'
                + frame(plain_src, 256, 60) + "</div>"),
            "the same badge, wherever it sits": '<div class="grecaptcha-badge" style="width:256px;height:60px">' + frame(plain_src, 256, 60) + "</div>",
            "a frame parked far off the page": frame(plain_src, 304, 78, "position:absolute;left:-10000px;top:0"),
            "a frame parked above the page": frame(plain_src, 304, 78, "position:absolute;left:20px;top:-10000px"),
            "a frame mostly outside the window": frame(plain_src, 304, 78, "position:fixed;right:-250px;bottom:10px"),
            "a frame with no room for a checkbox": frame(plain_src, 1, 1) + frame(plain_src, 0, 0),
            "the puzzle in a wrapper that is only visibility:hidden": '<div style="visibility:hidden">' + frame(BFRAME, 400, 580) + "</div>",
            "the puzzle in a wrapper that is only transparent": '<div style="opacity:0">' + frame(BFRAME, 400, 580) + "</div>",
            "the puzzle in a wrapper inside a transparent one": '<div style="opacity:0"><div>' + frame(BFRAME, 400, 580) + "</div></div>",
            "the way Google parks it until it decides to challenge": (
                '<div style="visibility:hidden;opacity:0;position:absolute;top:-10000px">' + frame(BFRAME, 400, 580) + "</div>"),
            "the puzzle in a wrapper that is display:none": '<div style="display:none">' + frame(BFRAME, 400, 580) + "</div>",
            "an invisible hCaptcha": frame(HCAPTCHA.format(kind="checkbox-invisible"), 303, 78),
            "an hCaptcha puzzle nobody has asked for yet": '<div style="visibility:hidden">' + frame(HCAPTCHA.format(kind="challenge"), 500, 600) + "</div>",
            "an unrelated frame": frame("/embed/apply/1", 800, 600) + frame("/jobs/1", 300, 200),
        }
        for what, html in cases.items():
            assert probe(env, html) == [], what


def test_a_page_with_all_the_invisible_recaptcha_parts_is_not_a_dead_end():
    from page_state import take_snapshot

    with agent_env() as env:
        if skipped(env):
            return
        sc = env.fresh()
        sc.session.goto(env.base + "/apply/invisible")                    # badge + hidden puzzle, exactly as Greenhouse leaves them
        assert sc.session.has_captcha() is False and sc.session.captcha_evidence() == ""
        snap = take_snapshot(sc.session)
        assert snap.captcha is False and snap.captcha_detail == "" and snap.page_type == "form"


# =========================================================================== real challenges still stop the application

def test_a_challenge_a_person_must_act_on_is_found_and_described():
    with agent_env() as env:
        if skipped(env):
            return
        checkbox = ANCHOR.format(size="size=normal")
        shown = "visibility:visible;opacity:1;position:fixed;top:40px;left:300px"
        cases = {
            "reCAPTCHA 'I'm not a robot' checkbox": (frame(checkbox, 304, 78), ("recaptcha", "checkbox", 304, 78)),
            "reCAPTCHA puzzle that is showing": (frame(BFRAME, 400, 580, shown), ("recaptcha", "puzzle", 400, 580)),
            "hCaptcha checkbox": (frame(HCAPTCHA.format(kind="checkbox"), 303, 78), ("hcaptcha", "checkbox", 303, 78)),
            "hCaptcha puzzle": (frame(HCAPTCHA.format(kind="challenge"), 500, 600, shown), ("hcaptcha", "puzzle", 500, 600)),
            "Cloudflare Turnstile": (frame(TURNSTILE, 300, 65), ("turnstile", "checkbox", 300, 65)),
            "a checkbox far below the fold of a long form": ('<div style="height:3000px"></div>' + frame(checkbox, 304, 78), ("recaptcha", "checkbox", 304, 78)),
            "a checkbox inside an open shadow root": (
                '<div id="host"></div><script>document.getElementById("host").attachShadow({mode:"open"}).innerHTML = '
                + "'" + frame(checkbox, 304, 78) + "'" + "</script>", ("recaptcha", "checkbox", 304, 78)),
        }
        for what, (html, (provider, kind, w, h)) in cases.items():
            (found,) = probe(env, html)
            assert (found["provider"], found["kind"], found["w"], found["h"]) == (provider, kind, w, h), what


def test_the_invisible_recaptcha_puzzle_counts_the_moment_it_is_shown():
    with agent_env() as env:
        if skipped(env):
            return
        sc = env.fresh()
        sc.session.goto(env.base + "/apply/challenge-on-submit")
        assert sc.session.has_captcha() is False                                  # hidden until the site decides to challenge
        sc.session.page.evaluate("showPuzzle()")
        assert sc.session.has_captcha() is True
        assert "recaptcha puzzle 400x580" in sc.session.captcha_evidence()


def test_a_visible_challenge_skips_the_job_and_the_reason_names_what_was_seen():
    with agent_env() as env:
        if skipped(env):
            return
        sc = env.fresh()
        e = expect(CaptchaEncountered, lambda: sc.run("/jobs/captcha"))
        assert "recaptcha checkbox" in str(e) and "CAPTCHA" in str(e)
        assert [p for m, p in env.ats.hits if m == "POST"] == []                  # nothing was sent, nothing was attempted


# =========================================================================== the applications themselves

def test_an_application_on_a_page_with_an_invisible_recaptcha_goes_through():
    with agent_env() as env:
        if skipped(env):
            return
        sc = env.fresh()
        sc.run("/apply/invisible")
        res = sc.handler.submit()
        assert res.ok and res.confirmed is True, res.evidence
        assert len(env.ats.applications) == 1


def test_a_puzzle_that_appears_after_the_submit_click_is_reported_and_never_solved():
    with agent_env() as env:
        if skipped(env):
            return
        sc = env.fresh()
        sc.run("/apply/challenge-on-submit")
        res = sc.handler.submit()                                                  # the click shows the puzzle instead of sending
        assert res.confirmed is None and "captcha" in res.evidence, res.evidence
        assert "recaptcha puzzle 400x580" in res.evidence
        assert env.ats.applications == []                                          # unconfirmed (blocks a retry), but nothing arrived


def test_the_agent_refuses_to_press_submit_while_a_puzzle_is_on_screen():
    with agent_env() as env:
        if skipped(env):
            return
        sc = env.fresh()
        sc.run("/apply/challenge-on-submit")
        sc.session.page.evaluate("showPuzzle()")                                   # the site decided to challenge before the click
        e = expect(CaptchaEncountered, sc.handler.submit)
        assert "recaptcha puzzle 400x580" in str(e) and "submit step" in str(e)
        assert env.ats.applications == []


# =========================================================================== the check looks at the page it is about

class BareHandler(BaseHandler):
    """Just the shared prepare()/submit(): no site-specific steps."""

    name = "bare"
    outcome_polls = 4

    def iter_steps(self, profile, job):
        return iter(())

    def success_selector(self):
        return None


class StubSession:
    """The few things BaseHandler asks of a session. ``challenge`` is what has_captcha() answers right now."""

    def __init__(self, challenge: bool = False, seen: str = "recaptcha checkbox 304x78"):
        self.url, self.challenge, self.seen, self.calls, self.waits = "https://boards.example.com/previous-job", challenge, seen, [], 0
        self.on_goto = self.on_click = None

    def goto(self, url, *, timeout_s=30):
        self.calls.append("goto")
        self.url = url
        if self.on_goto:
            self.on_goto()

    def has_captcha(self):
        self.calls.append("has_captcha")
        return self.challenge

    def captcha_evidence(self):
        return self.seen if self.challenge else ""

    def click(self, selector):
        self.calls.append("click")
        if self.on_click:
            self.on_click()

    def has(self, selector, wait_timeout_s=0):
        return selector == "button[type=submit]"

    def text(self, selector):
        return ""

    def wait_for_timeout(self, ms):
        self.waits += 1

    def current_url(self):
        return self.url

    def screenshot(self):
        return b""


JOB = {"id": "j", "title": "Engineer", "company": "Acme", "apply_url": "https://boards.example.com/acme/jobs/2"}


def test_the_previous_applications_page_is_not_mistaken_for_this_ones():
    # The tab still shows the last job when prepare() starts. A CAPTCHA there says nothing about the job about to be opened.
    session = StubSession(challenge=True)                                       # the old page has one
    session.on_goto = lambda: setattr(session, "challenge", False)              # the new page does not
    prepared = BareHandler(session).prepare({}, JOB)
    assert prepared.ats == "bare"
    assert session.calls.index("goto") < session.calls.index("has_captcha"), "the CAPTCHA check must come after going to the job"

    session = StubSession(challenge=False)                                      # the old page was clean, the new one is not
    session.on_goto = lambda: setattr(session, "challenge", True)
    e = expect(CaptchaEncountered, lambda: BareHandler(session).prepare({}, JOB))
    assert "recaptcha checkbox 304x78" in str(e) and "bare" in str(e)


def test_a_challenge_at_the_submit_step_stops_before_the_click():
    session = StubSession(challenge=True)
    e = expect(CaptchaEncountered, BareHandler(session).submit)
    assert "submit step" in str(e) and "click" not in session.calls


def test_a_challenge_that_appears_after_the_click_ends_the_wait_as_unconfirmed():
    session = StubSession(challenge=False)
    session.on_click = lambda: setattr(session, "challenge", True)
    res = BareHandler(session).submit()
    assert res.ok and res.confirmed is None
    assert "after the submit click" in res.evidence and "recaptcha checkbox 304x78" in res.evidence and "not sent" in res.evidence
    assert session.waits == 0, "it must not keep polling the full time once a challenge is on screen"

    session = StubSession(challenge=False)                                      # no challenge: the normal patient wait, then 'unknown'
    res = BareHandler(session).submit()
    assert res.confirmed is None and "neither a confirmation nor an error" in res.evidence and session.waits == BareHandler.outcome_polls


# =========================================================================== the whole of stage 3

FACTS_YAML = """work_authorization:
  authorized_countries: ["MX", "US"]
  requires_sponsorship: false
experience:
  years_by_skill:
    Python: 8
"""


def test_stage_3_applies_to_every_job_on_an_invisible_captcha_site_and_skips_only_a_real_challenge():
    ats = FakeATS()
    base = ats.start()
    try:
        with tmp_cwd() as tmp:
            try:
                from playwright.sync_api import sync_playwright

                with sync_playwright() as p:
                    p.chromium.launch(headless=True).close()
            except Exception:
                print("  (skipped: no Playwright browser available)")
                return
            (tmp / "facts.yaml").write_text(FACTS_YAML, encoding="utf-8")
            store = StateStore(tmp / "s.db")
            rid = store.start_run("stage3", "h", "p")
            # One cycle, one browser tab, three jobs: two on a form with an invisible reCAPTCHA, then one with a real checkbox.
            jobs = [save_job(store, rid, f"j{n}", base + path, title=f"Role {n}", company=f"Co{n}")
                    for n, path in ((1, "/apply/invisible"), (2, "/apply/invisible/2"), (3, "/jobs/captcha"))]
            cfg = stage3_cfg(tmp, probation={"enabled": False})
            with patch("answerer.chat_json", side_effect=LLMError("down")), patch("agent.chat_json", side_effect=LLMError("down")):
                stage3_run(jobs, dict(profile_for(tmp), mode="autonomous"), state=store, run_id=rid, config=cfg)

            assert [app_row(store, j)["status"] for j in ("j1", "j2")] == ["submitted", "submitted"]
            assert len(ats.applications) == 2
            skipped_row = app_row(store, "j3")
            assert skipped_row["status"] == "skipped" and skipped_row["error_code"] in (None, "")
            assert "CAPTCHA encountered" in skipped_row["error_message"] and "recaptcha checkbox" in skipped_row["error_message"]
            assert store.is_already_submitted(base + "/jobs/captcha") is False                      # retryable: not a verdict on the job
            store.close()
    finally:
        ats.stop()
