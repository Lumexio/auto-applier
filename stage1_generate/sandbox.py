"""AST security validator and isolated sandbox runner for generated scraper code."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ASTSecurityError(Exception):
    pass


class ASTPolicyVisitor(ast.NodeVisitor):
    FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__", "breakpoint"}
    FORBIDDEN_MODULES = {"subprocess", "shutil", "pty", "commands"}

    def __init__(self) -> None:
        self.errors: list[str] = []

    def visit_Call(self, node: ast.Call) -> None:
        # Check func name e.g. eval()
        if isinstance(node.func, ast.Name):
            if node.func.id in self.FORBIDDEN_CALLS:
                self.errors.append(f"Forbidden function call: {node.func.id}")
        # Check attribute calls e.g. os.system()
        elif isinstance(node.func, ast.Attribute):
            attr_name = node.func.attr
            if attr_name in ("system", "popen", "spawn", "execv", "execl"):
                self.errors.append(f"Forbidden OS execution method: {attr_name}")
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name in self.FORBIDDEN_MODULES:
                self.errors.append(f"Forbidden imported module: {alias.name}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module and node.module.split(".")[0] in self.FORBIDDEN_MODULES:
            self.errors.append(f"Forbidden module imported from: {node.module}")
        self.generic_visit(node)


def ast_policy_ok(code: str) -> bool:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    visitor = ASTPolicyVisitor()
    visitor.visit(tree)
    return len(visitor.errors) == 0


@dataclass
class SandboxResult:
    jobs: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    jobs_found: int = 0

    def matches_expected(self) -> bool:
        return self.jobs_found > 0


def run_in_sandbox(code: str, html_fixture: str = "", timeout_seconds: int = 30) -> SandboxResult:
    if not ast_policy_ok(code):
        return SandboxResult(errors=["Code failed AST security policy inspection."])

    # Construct execution harness
    harness = f"""
import json, sys

# Inject fixture html if provided
HTML = {json.dumps(html_fixture)}

{code}

if __name__ == "__main__":
    if "scrape" in globals() and callable(globals()["scrape"]):
        results = scrape(HTML)
        print("---OUTPUT_START---")
        print(json.dumps(results))
        print("---OUTPUT_END---")
    else:
        sys.exit(1)
"""

    with tempfile.TemporaryDirectory() as tmpdir:
        script_file = Path(tmpdir) / "run_scraper.py"
        script_file.write_text(harness, encoding="utf-8")

        try:
            proc = subprocess.run(
                [sys.executable, str(script_file)],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return SandboxResult(errors=[f"Sandbox execution timed out after {timeout_seconds}s."])

        if proc.returncode != 0:
            err = proc.stderr.strip() or f"Process exited with code {proc.returncode}"
            return SandboxResult(errors=[err])

        output = proc.stdout
        if "---OUTPUT_START---" in output and "---OUTPUT_END---" in output:
            json_blob = output.split("---OUTPUT_START---")[1].split("---OUTPUT_END---")[0].strip()
            try:
                jobs = json.loads(json_blob)
                return SandboxResult(jobs=jobs, jobs_found=len(jobs))
            except json.JSONDecodeError as e:
                return SandboxResult(errors=[f"Failed to parse scraper JSON output: {e}"])

        return SandboxResult(errors=["No output marker emitted by scraper."])
