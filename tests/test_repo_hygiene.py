"""The repository is public. Personal data and AI-assistant working files must stay out of it: .gitignore keeps them out, and
nothing already tracked may match a rule (a file that is both tracked and ignored is a leak waiting for the next `git add -f`).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GIT = shutil.which("git")

PERSONAL = [
    "profile.json", "facts.yaml", ".api_token", ".env", "state.db", "state.db-wal", "jobs.json", "daemon.log", ".daemon.pid",
    ".browser_profile/cookies.sqlite", "applications/job-1/trace/step-1.png",
    "me/resumes/resume-backend.md", "me/derived/experience.yaml", "me/experience/notes.md",
]
ASSISTANT_FILES = [
    "GEMINI.md", "gemini.md", "sub/dir/GEMINI.md", ".gemini/settings.json", ".geminiignore", ".agent/rules.md", ".agents/plan.md", ".antigravity/state.json",
    "CLAUDE.md", "CLAUDE.local.md", ".claude/settings.local.json", "AGENTS.md", ".codex/config.toml",
    ".cursorrules", ".cursor/rules/style.mdc", ".windsurfrules", ".windsurf/rules/a.md", ".clinerules", ".roo/rules/a.md", ".roomodes", ".kiro/steering/a.md",
    ".trae/rules/a.md", ".aider.chat.history.md", ".aider.input.history", ".continue/config.json", ".copilot/a.md",
    ".github/copilot-instructions.md", ".github/instructions/a.instructions.md", ".github/prompts/a.prompt.md",
    "notes.prompt.md", "PROMPT.md", "PROMPTS.md", "prompts/first-attempt.md", "memory-bank/progress.md", ".specify/memory/constitution.md",
]
PROJECT_FILES = [
    "README.md", "SETUP.md", "config.yaml", "agent.py", "stage3_apply/ats_handlers/agent_handler.py", "tests/test_p7_source.py", "source.py",
    "docs/agents-overview.md", ".github/workflows/ci.yml",         # "agent" in a name is fine; so is a workflow next to ignored Copilot files
]


def is_ignored(path: str) -> bool:
    return subprocess.run([GIT, "check-ignore", "-q", path], cwd=ROOT, capture_output=True).returncode == 0


def in_a_repository() -> bool:
    return bool(GIT) and subprocess.run([GIT, "rev-parse", "--is-inside-work-tree"], cwd=ROOT, capture_output=True).returncode == 0


def test_personal_files_and_the_source_of_truth_are_ignored():
    if not in_a_repository():
        return
    leaked = [p for p in PERSONAL if not is_ignored(p)]
    assert not leaked, f"not ignored, so `git add .` would publish them: {leaked}"


def test_ai_assistant_and_vibe_coding_files_are_ignored_at_any_depth():
    if not in_a_repository():
        return
    open_ = [p for p in ASSISTANT_FILES if not is_ignored(p)]
    assert not open_, f"assistant files that would be committed: {open_}"


def test_real_project_files_are_not_swallowed_by_those_rules():
    if not in_a_repository():
        return
    hidden = [p for p in PROJECT_FILES if is_ignored(p)]
    assert not hidden, f"project files wrongly ignored: {hidden}"


def test_nothing_tracked_matches_an_ignore_rule():
    if not in_a_repository():
        return
    out = subprocess.run([GIT, "ls-files", "-ci", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    assert not out, f"tracked although ignored (remove with `git rm --cached`): {out}"
    tracked = subprocess.run([GIT, "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.splitlines()
    assert not [t for t in tracked if t in ("profile.json", "facts.yaml", ".api_token", "state.db") or t.startswith(("me/", ".browser_profile/", "applications/"))]
    markdown = sorted(t for t in tracked if t.lower().endswith((".md", ".mdc")))
    assert markdown == ["README.md", "SETUP.md"], f"unexpected Markdown in the repository: {markdown}"


def test_the_gitignore_names_the_tools_it_means_to_keep_out():
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    lines = {ln.strip() for ln in text.splitlines()}
    for pattern in ("GEMINI.md", ".gemini/", "CLAUDE.md", ".claude/", "AGENTS.md", ".cursorrules", ".windsurfrules", ".github/copilot-instructions.md", "*.prompt.md", "/me/"):
        assert pattern in lines, f".gitignore no longer lists {pattern}"
    assert "git add -f" in text, "the file should say how to publish one on purpose"
