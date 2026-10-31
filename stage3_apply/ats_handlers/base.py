"""Abstract ATS Handler interface and BrowserSession protocol."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol

from ..confirm import looks_submitted


class StepStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class FormField:
    selector: str
    value: str | list[str] | None
    kind: str  # "text" | "select" | "radio" | "checkbox" | "file" | "textarea"
    label: str | None = None
    required: bool = False


@dataclass
class Step:
    name: str
    fields: list[FormField] = field(default_factory=list)
    uploads: dict[str, str] = field(default_factory=dict)
    next_selector: str | None = None
    status: StepStatus = StepStatus.PENDING
    error: str | None = None


@dataclass
class PreparedForm:
    steps: list[Step]
    review_screenshot: bytes | None = None
    cover_letter: str | None = None
    answers: dict[str, str] = field(default_factory=dict)
    ats: str = "unknown"


@dataclass
class SubmissionResult:
    """What clicking submit achieved.

    ``ok`` is False when nothing was submitted at all (e.g. the posting redirects to the employer's own site).
    ``confirmed`` is the handler's own observation made *before* any dialog was dismissed: True = the site said
    the application was received, False = the site rejected it (visible form errors), None = it could not tell.
    """

    ok: bool
    ats_id: str | None = None
    confirmation_url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    confirmed: bool | None = None
    evidence: str = ""


class ATSError(Exception):
    pass


class CaptchaEncountered(ATSError):
    pass


class LoginRequired(ATSError):
    pass


class DuplicateApplication(ATSError):
    pass


class UnsupportedForm(ATSError):
    pass


class ConfirmationTimeout(ATSError):
    pass


class NeedsHumanInput(ATSError):
    """Required questions that cannot be answered from the fact sheet; the application is parked, never guessed."""

    def __init__(self, questions: list[dict[str, Any]]):
        self.questions = questions
        super().__init__(f"{len(questions)} required question(s) need your answer: " + "; ".join(q.get("label", "?")[:60] for q in questions[:3]))


class PostingClosed(ATSError):
    """The posting is closed, filled or gone. Permanent: there is nothing to retry."""


class NotAFit(ATSError):
    """The posting was judged a poor fit before anything was filled in."""

    def __init__(self, summary: str, details: dict[str, Any] | None = None):
        self.summary = summary
        self.details = details or {}
        super().__init__(summary)


class FitDisputed(ATSError):
    """One model vetoed the posting and a second model, asked independently, would apply: nobody skips it for good and
    nobody applies; a person decides (``python fit_review.py``)."""

    def __init__(self, summary: str, details: dict[str, Any] | None = None):
        self.summary = summary
        self.details = details or {}
        super().__init__(summary)


class BrowserSession(Protocol):
    url: str

    def goto(self, url: str, *, timeout_s: int = 30) -> None: ...
    def fill(self, selector: str, value: str) -> None: ...
    def select(self, selector: str, value: str) -> None: ...
    def check(self, selector: str) -> None: ...
    def upload(self, selector: str, path: str) -> None: ...
    def click(self, selector: str) -> None: ...
    def wait_for(self, selector: str, *, timeout_s: int = 30) -> None: ...
    def wait_for_timeout(self, ms: int) -> None: ...
    def has(self, selector: str, wait_timeout_s: float = 0) -> bool: ...
    def text(self, selector: str) -> str: ...
    def screenshot(self) -> bytes: ...
    def current_url(self) -> str: ...
    def requires_login(self) -> bool: ...
    def has_captcha(self) -> bool: ...


class BaseHandler(abc.ABC):
    name: str = "base"
    url_patterns: tuple[str, ...] = ()
    html_markers: tuple[str, ...] = ()
    # Text a site shows on a posting you already applied to. Used before filling (-> duplicate) and after an
    # unconfirmed submit (a reload that now shows it proves the application went through).
    applied_markers: tuple[str, ...] = ()
    # After the submit click we poll for success / rejection evidence this many half-seconds.
    outcome_polls: int = 24

    @classmethod
    def detect(cls, url: str, html: str = "") -> bool:
        u = url.lower()
        if any(p in u for p in cls.url_patterns):
            return True
        h = html.lower()
        return any(m.lower() in h for m in cls.html_markers)

    def __init__(self, session: Any, *, timeout_s: int = 30) -> None:
        self.session = session
        self.timeout_s = timeout_s
        # Injected by stage 3. solver answers the form's questions; before_fill may veto the posting (NotAFit).
        self.solver: Any = None
        self.before_fill: Callable[[Any, dict[str, Any]], None] | None = None
        self._job: dict[str, Any] = {}
        # Settings for the application agent, which any handler can hand over to (e.g. an "Apply on employer site" link).
        self.agent_cfg: dict[str, Any] = {}
        self.llm_cfg: dict[str, Any] = {}
        self.trace_dir: Any = None
        self._delegate: BaseHandler | None = None

    def spawn_agent(self) -> BaseHandler:
        """A fresh application agent sharing this handler's browser session and configuration."""
        from .agent_handler import AgentHandler

        agent = AgentHandler(self.session, timeout_s=self.timeout_s)
        agent.solver, agent.agent_cfg, agent.llm_cfg, agent.trace_dir = self.solver, self.agent_cfg, self.llm_cfg, self.trace_dir
        return agent

    def _delegate_external(self, profile: dict[str, Any], job: dict[str, Any], apply_selectors: tuple[str, ...]) -> bool:
        """Click the "apply on the employer's site" control, follow it (often into a new tab) and let the agent finish there."""
        if not self.agent_cfg.get("enabled", True) or not hasattr(self.session, "adopt_new_page") or not hasattr(self.session, "has"):
            return False
        for selector in apply_selectors:
            if self.session.has(selector):
                self.session.click(selector)
                break
        else:
            return False
        self.session.wait_for_timeout(1500)                     # a click that leaves the site can take a moment to open the new tab
        self.session.adopt_new_page()
        delegate = self.spawn_agent()
        delegate.run_from_current_page(profile, job)           # raises the same exceptions as any handler
        self._delegate = delegate
        return True

    def prepare(self, profile: dict[str, Any], job: dict[str, Any], *, checkpoint_callback=None) -> PreparedForm:
        if hasattr(self.session, "has_captcha") and self.session.has_captcha():
            raise CaptchaEncountered(f"{self.name}: CAPTCHA detected")
        if hasattr(self.session, "requires_login") and self.session.requires_login():
            raise LoginRequired(f"{self.name}: Login required")

        self._job = job
        self._navigate(job["apply_url"])
        self._precheck_duplicate()
        if self.before_fill:
            self.before_fill(self.session, job)

        steps: list[Step] = []
        for step in self.iter_steps(profile, job):
            try:
                self.execute_step(step)
                step.status = StepStatus.DONE
                if checkpoint_callback:
                    checkpoint_callback(step.name)
            except (CaptchaEncountered, LoginRequired):
                step.status = StepStatus.BLOCKED
                raise
            except UnsupportedForm as e:
                step.status = StepStatus.FAILED
                step.error = str(e)
                raise
            steps.append(step)

        self._solve_questions()          # final sweep: single-page forms and review pages

        screenshot = self.session.screenshot() if hasattr(self.session, "screenshot") else b""
        return PreparedForm(
            steps=steps,
            review_screenshot=screenshot,
            cover_letter=self.build_cover_letter(profile, job),
            answers=self.collect_answers(steps),
            ats=self.name,
        )

    def submit(self) -> SubmissionResult:
        if hasattr(self.session, "has_captcha") and self.session.has_captcha():
            raise CaptchaEncountered(f"{self.name}: CAPTCHA at submit step")
        baseline = self._current_url()
        self._click_submit()
        confirmed, evidence = self._observe_outcome(baseline)
        url = self._current_url()
        return SubmissionResult(ok=True, confirmation_url=url, raw={"url": url}, confirmed=confirmed, evidence=evidence)

    # ---- outcome evidence --------------------------------------------------

    def _current_url(self) -> str:
        return self.session.current_url() if hasattr(self.session, "current_url") else getattr(self.session, "url", "")

    def error_selectors(self) -> tuple[str, ...]:
        """Visible markers of a rejected form. Deliberately specific: a toast or cookie banner must not look like one."""
        return ("[aria-invalid='true']:visible", ".field_with_errors:visible", ".artdeco-inline-feedback--error:visible")

    def _visible_errors(self) -> list[str]:
        found: list[str] = []
        for sel in self.error_selectors():
            if hasattr(self.session, "has") and self.session.has(sel):
                text = self.session.text(sel) if hasattr(self.session, "text") else ""
                found.append((text or sel).strip()[:120])
        return found

    def _observe_outcome(self, baseline_url: str | None = None) -> tuple[bool | None, str]:
        """Watch the page right after the submit click.

        Must run BEFORE any "Done"/"Dismiss" click: the success dialog is the evidence and it disappears with it.
        Returns (True, evidence) on success, (False, errors) when the form rejected the submit, (None, why) otherwise.
        """
        success = self.success_selector()
        for poll in range(max(1, self.outcome_polls)):
            if success and hasattr(self.session, "has") and self.session.has(success):
                return True, f"success marker visible: {success}"
            url = self._current_url()
            if baseline_url and url != baseline_url and looks_submitted(url):
                return True, f"redirected to a confirmation page: {url}"
            errors = self._visible_errors()
            if errors:
                return False, "form rejected the submit: " + "; ".join(errors[:3])
            if hasattr(self.session, "wait_for_timeout"):
                self.session.wait_for_timeout(500)
        return None, "neither a confirmation nor an error appeared"

    def verify_applied(self, job: dict[str, Any]) -> bool:
        """After an unconfirmed submit: reload the posting. Sites that remember applications now show it as applied."""
        if not self.applied_markers or not hasattr(self.session, "has"):
            return False
        try:
            self._navigate(job["apply_url"])
        except Exception:
            return False
        return any(self.session.has(f"text={m}") for m in self.applied_markers)

    # ---- questions ---------------------------------------------------------

    def _solve_questions(self) -> None:
        """Answer whatever questions the current page shows. Required ones nobody can answer park the application."""
        if self.solver is None:
            return
        report = self.solver.solve(self.session, self._job)
        if report.blocking:
            raise NeedsHumanInput(report.unanswered)

    @abc.abstractmethod
    def iter_steps(self, profile: dict[str, Any], job: dict[str, Any]):
        raise NotImplementedError

    @abc.abstractmethod
    def success_selector(self) -> str | None:
        raise NotImplementedError

    def _navigate(self, url: str) -> None:
        if hasattr(self.session, "goto"):
            self.session.goto(url, timeout_s=self.timeout_s)

    def _precheck_duplicate(self) -> None:
        for marker in ("already applied", "you have already", "application received", *self.applied_markers):
            if hasattr(self.session, "has") and self.session.has(f"text={marker}"):
                raise DuplicateApplication(f"{self.name}: {marker}")

    def execute_step(self, step: Step) -> None:
        for f in step.fields:
            if f.value is None:
                continue
            val = str(f.value)
            if f.kind == "select" and hasattr(self.session, "select"):
                self.session.select(f.selector, val)
            elif f.kind == "checkbox" and hasattr(self.session, "check"):
                self.session.check(f.selector)
            elif f.kind in ("text", "textarea", "radio") and hasattr(self.session, "fill"):
                self.session.fill(f.selector, val)
            elif f.kind == "file" and hasattr(self.session, "upload"):
                self.session.upload(f.selector, val)

        for selector, path in step.uploads.items():
            if hasattr(self.session, "upload"):
                self.session.upload(selector, path)

        self._solve_questions()

        if step.next_selector and hasattr(self.session, "click"):
            self.session.click(step.next_selector)
            if hasattr(self.session, "wait_for"):
                self.session.wait_for(step.next_selector, timeout_s=self.timeout_s)

    def build_cover_letter(self, profile: dict[str, Any], job: dict[str, Any]) -> str | None:
        return (profile.get("attachments") or {}).get("cover_template")

    def collect_answers(self, steps: list[Step]) -> dict[str, str]:
        out: dict[str, str] = {}
        for s in steps:
            for f in s.fields:
                if f.label and f.value is not None:
                    out[f.label] = str(f.value)
        return out

    def _click_submit(self) -> None:
        for sel in self.submit_selectors():
            if hasattr(self.session, "has") and self.session.has(sel):
                self.session.click(sel)
                return
        for sel in self.submit_selectors():
            if hasattr(self.session, "has") and self.session.has(sel, wait_timeout_s=3):
                self.session.click(sel)
                return
        raise UnsupportedForm(f"{self.name}: no submit button found")

    def submit_selectors(self) -> tuple[str, ...]:
        return (
            "button[type=submit]",
            "button:has-text('Submit application')",
            "button:has-text('Submit Application')",
            "button:has-text('Submit')",
            "button:has-text('Easy Apply')",
            "button:has-text('Quick apply')",
            "button:has-text('Apply now')",
            "button:has-text('Apply')",
            "input[type=submit]",
        )


_HANDLERS: list[type[BaseHandler]] = []


def register(cls: type[BaseHandler]) -> type[BaseHandler]:
    _HANDLERS.append(cls)
    return cls


_FALLBACK: type[BaseHandler] | None = None


def set_fallback(cls: type[BaseHandler]) -> None:
    """The handler for sites no other handler claims (the application agent)."""
    global _FALLBACK
    _FALLBACK = cls


def pick_handler(url: str, html: str = "") -> type[BaseHandler]:
    for cls in _HANDLERS:
        if cls.detect(url, html):
            return cls
    return _FALLBACK or FallbackHandler


class FallbackHandler(BaseHandler):
    name = "custom"
    url_patterns = ()
    html_markers = ()

    def iter_steps(self, profile: dict[str, Any], job: dict[str, Any]):
        # This used to fill nothing and then click the page's first "Submit"/"Apply" button, on any site.
        raise UnsupportedForm("no handler knows this site and the application agent is switched off (agent.enabled: false)")
        yield  # pragma: no cover  (keeps this a generator)

    def success_selector(self) -> str | None:
        return None
