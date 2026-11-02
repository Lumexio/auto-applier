"""Which of your resumes goes with which job.

If you keep more than one version of your resume as a PDF (``resume-frontend.pdf`` beside ``resume-base.pdf``), the one that fits the posting is
attached instead of always the default. It is decided by plain keywords, no model, so it is predictable:

* the job's TITLE first: "Frontend Engineer" names the frontend kind, "Backend Engineer" the backend kind;
* only when the title names neither, the posting text, and then only if it clearly leans one way (at least 4 hits and twice the other kind's);
* anything unclear (a "Full Stack" title, a tie, a kind you have no PDF for) keeps your default, ``attachments.resume_path``.

Your versions are the ``resume-<name>.pdf`` files next to the default resume, or ``attachments.resumes`` ({name: path}) if you list them.
Keywords per name can be replaced in ``attachments.resume_rules`` ({name: [words]}). Switch it off with ``stage3.resume_per_job: false``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

DEFAULT_RULES: dict[str, list[str]] = {
    "frontend": ["frontend", "front-end", "front end", "react", "vue", "angular", "ui engineer", "ui developer", "web developer", "javascript", "typescript", "css"],
    "backend": ["backend", "back-end", "back end", "api", "microservice", "python", "django", "flask", "java", "golang", "node", "server-side",
                "distributed systems", "data engineer", "platform"],
}
_FULL_STACK = re.compile(r"full[\s-]?stack", re.IGNORECASE)
MIN_TEXT_HITS = 4


def _hits(text: str, words: list[str]) -> int:
    folded = text.lower()
    return sum(len(re.findall(rf"(?<![a-z0-9]){re.escape(w.lower())}(?![a-z0-9])", folded)) for w in words)


def available(profile: dict[str, Any]) -> dict[str, str]:
    """{name: path} of the resume PDFs that exist (names are lower case)."""
    att = profile.get("attachments") or {}
    listed = att.get("resumes")
    found: dict[str, str] = {}
    if isinstance(listed, dict):
        for name, path in listed.items():
            if path and Path(str(path)).is_file():
                found[str(name).lower()] = str(path)
        return found
    base = att.get("resume_path")
    if base and Path(str(base)).parent.is_dir():
        for pdf in sorted(Path(str(base)).parent.glob("resume-*.pdf")):
            found[pdf.stem[len("resume-"):].lower()] = str(pdf.resolve())
    return found


def choose(profile: dict[str, Any], job: dict[str, Any]) -> tuple[str, str] | None:
    """(name, path) of the resume to attach to this job, or None to keep the default."""
    att = profile.get("attachments") or {}
    versions = available(profile)
    if not versions:
        return None
    rules = {**DEFAULT_RULES, **{str(k).lower(): [str(w) for w in v] for k, v in (att.get("resume_rules") or {}).items() if isinstance(v, list)}}
    title, text = str(job.get("title") or ""), str(job.get("description") or "")
    if _FULL_STACK.search(title):
        return None
    by_title = {name: _hits(title, words) for name, words in rules.items()}
    named = {name: n for name, n in by_title.items() if n}
    winner = None
    if named:
        top = max(named.values())
        leaders = [name for name, n in named.items() if n == top]
        winner = leaders[0] if len(leaders) == 1 else None
    else:
        by_text = sorted(((_hits(text, words), name) for name, words in rules.items()), reverse=True)
        if by_text and by_text[0][0] >= MIN_TEXT_HITS and (len(by_text) == 1 or by_text[0][0] >= 2 * by_text[1][0]):
            winner = by_text[0][1]
    path = versions.get(winner or "")
    if not path:
        return None                                        # no PDF for the kind the posting wants: the default, never the other kind
    default = att.get("resume_path")
    if default and Path(str(default)).resolve() == Path(path).resolve():
        return None
    return winner, path


def for_job(profile: dict[str, Any], job: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """The profile to prepare this job with (a copy with the right resume), and a sentence saying which, or "" when the default stays."""
    pick = choose(profile, job)
    if pick is None:
        return profile, ""
    name, path = pick
    return {**profile, "attachments": {**(profile.get("attachments") or {}), "resume_path": path}}, f"{name} version ({Path(path).name})"
