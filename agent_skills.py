"""Skills: short, reusable procedures for kinds of page the application agent does not recognise on its own.

A skill is a Markdown file, ``skills/<name>/SKILL.md`` (yours go in ``<paths.source_dir>/skills/<name>/SKILL.md``):

    ---
    name: apply-on-company-site
    description: A job page that sends you to the employer's own careers site to apply.
    when: ["apply (on|at|via) (the )?(company|employer)", "external application"]
    ---
    1. Prefer the button that leads to the employer's own careers site or its application system.
    2. ...

Select, load, follow, check (the shape the lecture describes):

  * select  code compares ``when`` (regular expressions, case-insensitive) with the page's title, text and button labels and picks
            the best match. No model call: a small model asked to pick among skills is a coin toss, a pattern is not.
  * load    the procedure text (capped) is added to the prompt of the model that is choosing a button on that page.
  * follow  the model still answers with a number from the menu the code built. A skill can make it choose better; it cannot add an
            action, widen where the agent may go, or press anything the agent's own rules forbid. A page cannot load a skill either:
            page text can only decide WHICH of your skills matches, never what a skill says.
  * check   the trace of every step records which skill was loaded, so a wrong turn is easy to trace to its procedure.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BODY_CHARS = 700                      # a small model has a small window: one skill, cut here
BUILTIN_DIR = Path(__file__).resolve().parent / "skills"


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    when: tuple[re.Pattern[str], ...]
    body: str
    source: str = ""

    def score(self, text: str) -> int:
        """How many of this skill's ``when`` patterns the page text matches."""
        return sum(1 for pattern in self.when if pattern.search(text))


_skills: list[Skill] = []


def parse(raw: str, source: str = "") -> Skill | None:
    """The skill in one SKILL.md, or None if it has no usable header (a bad file is skipped, never fatal)."""
    m = re.match(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", raw.strip() + "\n", re.DOTALL)
    if not m:
        return None
    try:
        import yaml

        head = yaml.safe_load(m.group(1)) or {}
    except Exception:
        return None
    if not isinstance(head, dict):
        return None
    name = str(head.get("name") or "").strip()
    when = head.get("when")
    when = [when] if isinstance(when, str) else when
    body = m.group(2).strip()
    if not name or not isinstance(when, list) or not body:
        return None
    patterns: list[re.Pattern[str]] = []
    for item in when:
        try:
            patterns.append(re.compile(str(item), re.IGNORECASE))
        except re.error:
            continue                                                    # one bad pattern does not discard the skill
    if not patterns:
        return None
    if len(body) > BODY_CHARS:
        body = body[:BODY_CHARS].rsplit("\n", 1)[0].rstrip()
    return Skill(name, " ".join(str(head.get("description") or "").split()), tuple(patterns), body, source)


def load_dir(folder: Path) -> list[Skill]:
    found: list[Skill] = []
    try:
        files = sorted(folder.glob("*/SKILL.md"))
    except OSError:
        return found
    for path in files:
        try:
            skill = parse(path.read_text(encoding="utf-8"), str(path))
        except (OSError, UnicodeDecodeError):
            continue
        if skill:
            found.append(skill)
    return found


def load_from(cfg: dict[str, Any] | None) -> list[Skill]:
    """The built-in skills plus yours (one of yours with the same name replaces the built-in); empty when ``agent.skills`` is false."""
    global _skills
    _skills = []
    if ((cfg or {}).get("agent") or {}).get("skills", True):
        by_name = {s.name: s for s in load_dir(BUILTIN_DIR)}
        mine = Path(str(((cfg or {}).get("paths") or {}).get("source_dir") or "me")) / "skills"
        by_name.update({s.name: s for s in load_dir(mine)})
        _skills = sorted(by_name.values(), key=lambda s: s.name)
    return _skills


def available() -> list[Skill]:
    return list(_skills)


def set_skills(skills: list[Skill]) -> None:
    """For tests."""
    global _skills
    _skills = list(skills)


def best(text: str, skills: list[Skill] | None = None) -> Skill | None:
    """The skill whose patterns match ``text`` best (ties: the name that sorts first), or None when none match."""
    ranked = [(-s.score(text), s.name, s) for s in (_skills if skills is None else skills)]
    ranked = [r for r in ranked if r[0] < 0]
    return min(ranked, key=lambda r: (r[0], r[1]))[2] if ranked else None


def prompt_block(skill: Skill) -> str:
    return (
        f"PROCEDURE \"{skill.name}\" (the candidate's own guidance for this kind of page; it helps you choose a menu number, "
        f"it does not add actions and does not override the rules above):\n{skill.body}"
    )
