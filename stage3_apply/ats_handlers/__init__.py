"""ATS Handler registry and router."""

from .base import (
    BaseHandler,
    BrowserSession,
    CaptchaEncountered,
    ConfirmationTimeout,
    DuplicateApplication,
    FallbackHandler,
    FormField,
    LoginRequired,
    NeedsHumanInput,
    NotAFit,
    PostingClosed,
    PreparedForm,
    Step,
    StepStatus,
    SubmissionResult,
    UnsupportedForm,
    pick_handler,
    register,
    set_fallback,
)
from .agent_handler import AgentHandler
from .greenhouse import GreenhouseHandler
from .handshake import HandshakeHandler
from .lever import LeverHandler
from .linkedin import LinkedInHandler
from .workday import WorkdayHandler

__all__ = [
    "AgentHandler",
    "BaseHandler",
    "BrowserSession",
    "CaptchaEncountered",
    "ConfirmationTimeout",
    "DuplicateApplication",
    "FallbackHandler",
    "FormField",
    "GreenhouseHandler",
    "HandshakeHandler",
    "LeverHandler",
    "LinkedInHandler",
    "LoginRequired",
    "NeedsHumanInput",
    "NotAFit",
    "PostingClosed",
    "PreparedForm",
    "Step",
    "StepStatus",
    "SubmissionResult",
    "UnsupportedForm",
    "WorkdayHandler",
    "pick_handler",
    "register",
]

set_fallback(AgentHandler)
