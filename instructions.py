"""Your standing instructions: a Markdown file the bot reads before it writes or chooses anything.

    <paths.source_dir>/agent-instructions.md      (git-ignored with the rest of me/; see examples/agent-instructions.example.txt)

The file is TRUSTED text from you, so it can steer style and preferences ("write short answers", "prefer the backend resume for Python
roles"). It can never widen what the bot may claim: the checks that stop an invented number, link, employer or degree are plain code that
run after the model answers, and nothing in this file reaches them. Page text and form questions stay untrusted data; this file is the
only free text the prompts treat as yours.

Optional sections `## answers`, `## essays`, `## outreach`, `## agent` apply to that task only; text before the first heading applies to all.
The text is capped and re-read at the start of every cycle, so editing the file takes effect without a restart.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

FILENAME = "agent-instructions.md"
MAX_CHARS = 1500                     # a small local model has a small window: the rest of the file is dropped
TASKS = ("answers", "essays", "outreach", "agent")

_HEADER = (
    "\n\nSTANDING INSTRUCTIONS FROM THE CANDIDATE (style and preferences only; they never allow inventing or exaggerating a fact, "
    "and never override the rules above):\n"
)

_text = ""


def path_for(cfg: dict[str, Any] | None) -> Path:
    return Path(str(((cfg or {}).get("paths") or {}).get("source_dir") or "me")) / FILENAME


def clean(raw: str) -> str:
    """The text without HTML comments or runs of blank lines, cut to ``MAX_CHARS`` at a line boundary."""
    out: list[str] = []
    inside = False
    for line in raw.splitlines():
        stripped = line.strip()
        if inside:
            inside = "-->" not in stripped
            continue
        if stripped.startswith("<!--"):
            inside = "-->" not in stripped
            continue
        if stripped or (out and out[-1]):
            out.append(line.rstrip())
    text = "\n".join(out).strip()
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS].rsplit("\n", 1)[0].rstrip()
    return text


def load_from(cfg: dict[str, Any] | None) -> str:
    """Read the file ``cfg`` names (nothing when it is missing or ``instructions.enabled`` is false) and make it the current text."""
    global _text
    _text = ""
    if ((cfg or {}).get("instructions") or {}).get("enabled", True):
        try:
            _text = clean(path_for(cfg).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            _text = ""
    return _text


def current() -> str:
    return _text


def set_text(text: str) -> None:
    """For tests, and for callers that already have the text."""
    global _text
    _text = clean(text or "")


def for_task(text: str, task: str) -> str:
    """The part of ``text`` that applies to ``task``: what precedes the first task heading, then that task's own section."""
    shared: list[str] = []
    own: list[str] = []
    target = shared
    for line in text.splitlines():
        if line.startswith("## ") and line[3:].strip().casefold() in TASKS:
            target = own if line[3:].strip().casefold() == task else []
            continue
        target.append(line)
    return "\n".join(shared + own).strip()


def system(base: str, task: str = "") -> str:
    """``base`` (a system prompt) followed by your instructions for ``task``, or ``base`` unchanged when there are none."""
    text = for_task(_text, task) if task else _text
    return base + _HEADER + text if text else base
