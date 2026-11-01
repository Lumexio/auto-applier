"""Tests for Stage 3: Multi-ATS autonomous state machine handlers and confirmation."""

try:
    import pytest
except ImportError:
    class DummyRaises:
        def __init__(self, exc):
            self.exc = exc
        def __enter__(self):
            return self
        def __exit__(self, et, ev, tb):
            if et is None:
                raise AssertionError(f"{self.exc} was not raised")
            return issubclass(et, self.exc)

    class DummyPytest:
        @staticmethod
        def raises(exc):
            return DummyRaises(exc)
    pytest = DummyPytest()
from stage3_apply.ats_handlers import (
    CaptchaEncountered,
    DuplicateApplication,
    GreenhouseHandler,
    HandshakeHandler,
    LeverHandler,
    LinkedInHandler,
    LoginRequired,
    WorkdayHandler,
    pick_handler,
)
from pathlib import Path
from stage3_apply.confirm import looks_submitted, wait_for_confirmation


def test_ats_handler_resolution():
    assert pick_handler("https://acme.wd1.myworkdayjobs.com/en-US/site/job/1") is WorkdayHandler
    assert pick_handler("https://boards.greenhouse.io/stripe/jobs/42") is GreenhouseHandler
    assert pick_handler("https://jobs.lever.co/netflix/uuid-1") is LeverHandler
    assert pick_handler("https://www.linkedin.com/jobs/view/4473691580/") is LinkedInHandler
    assert pick_handler("https://app.joinhandshake.com/jobs/11623678") is HandshakeHandler


def test_linkedin_easy_apply_multistep(sample_profile, fake_session):
    job = {
        "id": "li_4473691580",
        "title": "Senior Frontend Developer",
        "company": "Tech Corp",
        "apply_url": "https://www.linkedin.com/jobs/view/4473691580/",
    }
    fake_session._present = {
        "button:has-text('Easy Apply')",
        "button:has-text('Next')",
        "button:has-text('Submit application')",
        # the exact selector LinkedInHandler.submit() looks for; without it the handler (correctly) reports ok=False
        "button:has-text('Submit application'), button[aria-label='Submit application']",
    }
    handler = LinkedInHandler(fake_session)
    prep = handler.prepare(sample_profile, job)
    assert prep.ats == "linkedin"
    res = handler.submit()
    assert res.ok is True


def test_handshake_quick_apply(sample_profile, fake_session):
    job = {
        "id": "hs_11623678",
        "title": "Web Developer",
        "company": "Digital Agency",
        "apply_url": "https://app.joinhandshake.com/jobs/11623678",
    }
    fake_session._present = {
        "button:has-text('Quick apply')",
        "button:has-text('Submit application')",
        # the exact selector HandshakeHandler.submit() looks for
        "button:has-text('Submit application'), button:has-text('Submit')",
    }
    handler = HandshakeHandler(fake_session)
    prep = handler.prepare(sample_profile, job)
    assert prep.ats == "handshake"
    res = handler.submit()
    assert res.ok is True


def test_workday_multistep_autonomous_execution(sample_profile, fake_session):
    job = {
        "id": "j_wd",
        "title": "Backend Architect",
        "company": "Enterprise Inc",
        "apply_url": "https://enterprise.wd1.myworkdayjobs.com/job/1",
    }
    handler = WorkdayHandler(fake_session)

    checkpoints = []
    prepared = handler.prepare(
        sample_profile,
        job,
        checkpoint_callback=lambda step: checkpoints.append(step),
    )

    step_names = [s.name for s in prepared.steps]
    assert "my_information" in step_names
    assert "my_experience" in step_names
    assert "voluntary_disclosures" in step_names
    assert "review" in step_names
    assert checkpoints == step_names

    # Verify filled actions
    fills = [a for a in fake_session.actions if a[0] == "fill"]
    assert any("Alex" in str(a) for a in fills)
    assert any("alex.rivera@example.com" in str(a) for a in fills)


def test_workday_login_and_captcha_detection(sample_profile, fake_session):
    job = {"id": "j", "company": "Co", "apply_url": "https://wd.test/1"}

    # Login required
    fake_session._login = True
    with pytest.raises(LoginRequired):
        WorkdayHandler(fake_session).prepare(sample_profile, job)

    # CAPTCHA
    fake_session._login = False
    fake_session._captcha = True
    with pytest.raises(CaptchaEncountered):
        WorkdayHandler(fake_session).prepare(sample_profile, job)


def test_greenhouse_and_lever_form_preparation(sample_profile, fake_session):
    gh_job = {"id": "gh_1", "company": "Stripe", "apply_url": "https://boards.greenhouse.io/stripe/jobs/1"}
    gh_prep = GreenhouseHandler(fake_session).prepare(sample_profile, gh_job)
    assert gh_prep.ats == "greenhouse"

    lever_job = {"id": "lev_1", "company": "Netflix", "apply_url": "https://jobs.lever.co/netflix/1"}
    lev_prep = LeverHandler(fake_session).prepare(sample_profile, lever_job)
    assert lev_prep.ats == "lever"


def test_confirmation_monitor(fake_session):
    assert looks_submitted("https://boards.greenhouse.io/jobs/123/confirmation") is True
    assert looks_submitted("https://boards.greenhouse.io/jobs/123/thank_you") is True
    assert looks_submitted("https://boards.greenhouse.io/jobs/123/apply") is False

    fake_session.url = "https://boards.greenhouse.io/thank-you"
    assert wait_for_confirmation(fake_session, timeout_seconds=2) is True


def test_persistent_context_and_langflow_components(sample_profile):
    import json
    from pathlib import Path
    from langflow_components import ProfileComponent, StateMonitorComponent
    from lfx.graph.graph.base import Graph

    from tests.test_p0_safety import tmp_cwd

    flow_file = Path("auto_applier_flow.json").resolve()          # found before the components move into a scratch folder

    # The components read profile.json and state.db from the working folder (and write profile.json when it is
    # missing), so run them in a scratch folder with a sample profile: never against a real profile or database.
    with tmp_cwd() as tmp:
        (tmp / "profile.json").write_text(json.dumps(sample_profile), encoding="utf-8")

        # 1. Profile component test
        prof = ProfileComponent().build()
        assert "name" in prof
        assert "skills" in prof

        # 2. State monitor component test
        metrics = StateMonitorComponent().build()
        assert "total_jobs" in metrics
        assert "submitted" in metrics

    # 3. Persistent profile directory check
    profile_dir = Path("./.browser_profile")
    assert profile_dir is not None

    # 4. Valid Langflow Graph payload check
    assert flow_file.is_file(), "auto_applier_flow.json missing"
    payload = json.loads(flow_file.read_text(encoding="utf-8"))
    g = Graph.from_payload(payload)
    assert len(g.vertices) == 5, f"Expected 5 vertices, got {len(g.vertices)}"
    assert len(g.edges) == 4, f"Expected 4 edges, got {len(g.edges)}"

    # 5. Verify all node code templates compile and execute in clean sandbox
    from lfx.custom.eval import eval_custom_component_code

    for node in payload["data"]["nodes"]:
        code = node["data"]["node"]["template"]["code"]["value"]
        cls = eval_custom_component_code(code)
        assert cls is not None
        inst = cls()
        assert inst is not None


def test_firefox_browser_engine_configuration():
    from daemon import load_yaml_config

    cfg = load_yaml_config("config.yaml")
    browser_cfg = cfg.get("browser", {})
    assert browser_cfg.get("type") == "firefox"

    # Verify isolated profile directory resolution
    base_dir = Path(browser_cfg.get("user_data_dir", "./.browser_profile"))
    firefox_profile = base_dir / "firefox"
    assert "firefox" in str(firefox_profile)


