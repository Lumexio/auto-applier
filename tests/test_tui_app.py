"""The Textual dashboard, driven headlessly in ITS OWN environment (.venv-tui), where Textual is installed.

Skipped until tui.bat has been run once to create that environment; the logic it displays is covered by test_tui_data.py.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_the_dashboard_loads_lists_opens_jobs_and_the_log_in_a_real_textual_app():
    python = ROOT / ".venv-tui" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        pytest.skip("run tui.bat once to create the dashboard environment (.venv-tui)")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([str(python), "-m", "tui", "--selftest"], cwd=ROOT, capture_output=True, text=True, timeout=180, env=env)
    assert result.returncode == 0 and "FAIL" not in result.stdout, result.stdout + result.stderr
    assert "all passed" in result.stdout
