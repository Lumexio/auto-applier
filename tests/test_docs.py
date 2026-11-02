"""The documentation must not promise what the code does not have: every link inside README.md and SETUP.md leads somewhere, and every
`setup.bat -Flag` they tell a person to type is a flag setup.ps1 really takes.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ("README.md", "SETUP.md")


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _slug(heading: str) -> str:
    """How GitHub turns a heading into the anchor that links point at."""
    text = heading.replace("`", "").strip().lower()
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def _anchors(name: str) -> set[str]:
    found: dict[str, int] = {}
    anchors: set[str] = set()
    in_fence = False
    for line in _read(name).splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        m = None if in_fence else re.match(r"#{1,6}\s+(.*\S)", line)
        if m:
            slug = _slug(m.group(1))
            n = found.get(slug, 0)
            found[slug] = n + 1
            anchors.add(slug if n == 0 else f"{slug}-{n}")
    return anchors


def _links(name: str) -> list[str]:
    text = re.sub(r"```.*?```", "", _read(name), flags=re.DOTALL)           # a link-looking thing inside a code block is not a link
    return re.findall(r"\]\(([^)\s]+)\)", text)


def test_every_link_in_the_guides_leads_somewhere():
    anchors = {name: _anchors(name) for name in DOCS}
    broken, checked = [], 0
    for name in DOCS:
        for target in _links(name):
            if re.match(r"[a-z]+://|mailto:", target):
                continue
            path, _, fragment = target.partition("#")
            checked += 1
            doc = path or name
            if path and not (ROOT / path).exists():
                broken.append(f"{name}: {target} (no such file)")
            elif fragment and doc in anchors and fragment not in anchors[doc]:
                broken.append(f"{name}: {target} (no such heading)")
    assert not broken, "\n".join(broken)
    assert checked >= 30, f"only {checked} links were looked at: the check is not seeing the guides' links"


def test_the_headings_that_the_tables_of_contents_name_exist():
    for name in DOCS:
        anchors = _anchors(name)
        assert anchors, f"{name} has no headings at all"
    assert "what-the-mvp-includes" in _anchors("README.md")
    assert {"1-requirements", "13-troubleshooting"} <= _anchors("SETUP.md")


def test_every_setup_flag_in_the_guides_is_a_flag_setup_takes():
    script = _read("setup.ps1")
    block = script[script.index("param ("):script.index(")\n\n$ErrorActionPreference")]
    takes = {name.lower() for name in re.findall(r"\[(?:switch|string)\]\$(\w+)", block)}
    assert {"all", "background", "desk", "stopall", "nogui", "browser"} <= takes, takes
    unknown, seen = [], 0
    for name in DOCS:
        for line in _read(name).splitlines():
            for m in re.finditer(r"(?:setup\.bat|setup\.ps1|\./setup)\b([^`\n]*)", line):
                for flag in re.findall(r"(?<![\w-])-([A-Z][A-Za-z]+)\b", m.group(1)):
                    seen += 1
                    if flag.lower() not in takes:
                        unknown.append(f"{name}: setup ... -{flag}")
    assert not unknown, "\n".join(unknown)
    assert seen >= 15, f"only {seen} setup flags were looked at: the check is not seeing the guides' commands"


def test_the_test_count_the_guides_state_is_not_larger_than_the_suite():
    """A count in the prose goes stale as tests are added; it must at least never promise more than exist."""
    declared = 0
    for path in (ROOT / "tests").glob("test_*.py"):
        declared += len(re.findall(r"^def test_", path.read_text(encoding="utf-8"), flags=re.MULTILINE))
    for name in DOCS:
        for stated in re.findall(r"\b(\d{3,4}) (?:automated )?tests\b", _read(name)):
            assert int(stated) <= declared, f"{name} says {stated} tests, the suite defines {declared}"
