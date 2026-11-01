"""`pending.py` (what is waiting for you, on one screen) and the boot desk (`desk.ps1`, `setup.ps1 -EnableDesk`).

The briefing reads the same files as the dashboard and must never write them. The PowerShell scripts are checked for the three
mistakes that actually bit while building them: a stray option segment that opened an extra terminal tab, `tui.bat` not found
(cmd does not search the current folder on a hardened Windows), and stopping a process because a pid file named it after a reboot.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import re
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pending
from tests.test_p0_safety import tmp_cwd
from tests.test_tui_data import NOW, add_app, project, runs, seed_fit_decisions
from tui import data

ROOT = Path(__file__).resolve().parent.parent
APPLY = "no automated submit: external_application_required"
CUT_OFF = "interrupted: outcome unknown, verify manually"


def screen(tmp, ollama=lambda _endpoint: [data.LoadedModel("qwen2.5:7b", 4.5, 4.5, None)], width=100):
    snap = data.snapshot(tmp, now=NOW, with_ollama=False, ollama=ollama)
    return pending.render(snap, data.parked_for_you(tmp), color=False, width=width, now=NOW)


# --------------------------------------------------------------------------- the query

def test_parked_for_you_lists_only_what_a_person_must_finish_newest_first():
    with tmp_cwd() as tmp:
        store = project(tmp)
        _, new = runs(store)
        add_app(store, new, "p1", "needs_review", timedelta(hours=-5), title="Old apply", company="Aco", message=APPLY)
        add_app(store, new, "p2", "needs_review", timedelta(hours=-4), title="Cut off", company="Bco", message=CUT_OFF)
        add_app(store, new, "p3", "needs_review", timedelta(hours=-3), title="Odd one", company="Cco", message="something unexpected")
        add_app(store, new, "p4", "needs_review", timedelta(hours=-2), title="New apply", company="Dco", message=APPLY)
        add_app(store, new, "q1", "needs_review", timedelta(hours=-2), code="needs_answers", message="2 required question(s) need your answer")
        add_app(store, new, "f1", "needs_review", timedelta(hours=-2), code="fit_disagreement", message="the fit models disagree")
        add_app(store, new, "s1", "submitted", timedelta(hours=-1))
        add_app(store, new, "x1", "failed", timedelta(hours=-1), message="no apply button found")

        rows = data.parked_for_you(tmp)

        assert [(r.job_id, r.kind) for r in rows] == [("p4", "apply"), ("p3", "other"), ("p2", "verify"), ("p1", "apply")]     # newest first; questions and disputes are listed elsewhere
        assert rows[0].title == "New apply" and rows[0].company == "Dco" and rows[0].url.startswith("https://")
        assert rows[1].detail == "something unexpected"
        assert [r.job_id for r in data.parked_for_you(tmp, limit=2)] == ["p4", "p3"]
        store.close()
    assert data.parked_for_you(Path("nowhere-at-all")) == []                              # no project, no database: an empty list, not a crash


# --------------------------------------------------------------------------- the screen

def test_the_screen_lists_everything_waiting_with_the_command_to_deal_with_it():
    with tmp_cwd() as tmp:
        store = project(tmp, "agent:\n  probation: {enabled: true, clean_runs_to_trust: 3}\n")
        _, new = runs(store)
        store.acquire_lock(pid=os.getpid())
        store.add_pending_questions("j1", [{"label": "How many years of PHP do you have?", "kind": "text", "options": [], "reason": "no fact"}])
        store.add_pending_questions("j2", [{"label": "How many years of PHP do you have?", "kind": "text", "options": [], "reason": "no fact"}])
        store.record_clean_run("lever", "x1", 3)
        store.approve_site("greenhouse")
        seed_fit_decisions(store, new)                                                    # one disputed job, one veto waiting for a second opinion
        add_app(store, new, "p1", "needs_review", timedelta(hours=-5), title="Apply by hand", company="Aco", message=APPLY)
        add_app(store, new, "p2", "needs_review", timedelta(hours=-4), title="Check by hand", company="Bco", message=CUT_OFF)
        add_app(store, new, "u1", "unconfirmed", timedelta(hours=-6))
        add_app(store, new, "u2", "unconfirmed", timedelta(hours=-7))

        text = screen(tmp)

        assert "AUTO-APPLIER" in text and "daemon  running" in text and "ollama  up (qwen2.5:7b)" in text and "1 submitted  ·  0/25 of the daily cap" in text
        needs = text.split("NEEDS YOU")[1].split("WAITING ON THE MACHINE")[0]
        assert "1 question the forms asked" in needs and "How many years of PHP" in needs and "×2 jobs" in needs and 'answers.py set "<question>" "<answer>" --requeue' in needs
        assert "1 job where the fit models disagree" in needs and "Role d1" in needs and "fit_review.py list" in needs
        assert "1 job to apply for yourself" in needs and "Apply by hand @ Aco" in needs and "https://" in needs
        assert "1 job to check by hand" in needs and "Check by hand @ Bco" in needs
        assert "1 site on probation" in needs and "lever  1/3 clean runs" in needs and "sites.py approve lever" in needs and "• greenhouse" not in needs     # trusted sites are not waiting
        waiting = text.split("WAITING ON THE MACHINE")[1]
        assert "1 veto waiting for a second opinion" in waiting and "2 applications sent but never confirmed" in waiting
        assert "Nothing is waiting for you" not in text
        assert "tui.bat" in text and "-Logs" in text                                      # where to look next
        store.close()


def test_a_quiet_project_says_so_and_a_missing_or_stopped_one_says_how_to_start():
    with tmp_cwd() as tmp:
        store = project(tmp)
        runs(store)
        quiet = screen(tmp)
        assert "Nothing is waiting for you." in quiet and "STOPPED" in quiet and "setup.bat -All -Background" in quiet
        assert quiet.split("WAITING ON THE MACHINE")[1].split("live view")[0].strip() == "nothing"
        assert "not answering" in screen(tmp, ollama=lambda _e: None)                      # Ollama down at boot is visible, not silent
        assert "no model loaded" in screen(tmp, ollama=lambda _e: [])
        store.close()
    assert "No state.db yet" in screen(Path("nowhere-at-all"))


def test_long_lists_are_cut_and_nothing_overflows_a_narrow_pane():
    with tmp_cwd() as tmp:
        store = project(tmp)
        _, new = runs(store)
        for i in range(pending.SHOWN + 4):
            add_app(store, new, f"a{i}", "needs_review", timedelta(minutes=-i), title="A very long job title " * 4, company="Quite a long company name Inc", message=APPLY)
        text = screen(tmp, width=60)
        assert f"{pending.SHOWN + 4} jobs to apply for yourself" in text and "… and 4 more" in text
        assert text.count("• ") == pending.SHOWN                                          # the rest are counted, not listed
        assert max(len(line) for line in text.splitlines()) <= 60, "a narrow pane must not wrap the layout apart"
        assert "…" in text                                                                # long titles and links are clipped
        header = [ln for ln in text.splitlines() if "to apply for yourself" in ln or ln.startswith("     (")]
        assert header and all(ln.startswith(" ") for ln in text.split("to apply for yourself")[1].splitlines()[1:2]), "a wrapped line keeps a hanging indent"
        assert pending.wrap("  alpha beta gamma delta", 14) == ["  alpha beta", "    gamma", "    delta"] and pending.wrap("short", 50) == ["short"]
        assert pending.wrap("  \x1b[1mab cd\x1b[0m ef gh", 12) == ["  \x1b[1mab cd\x1b[0m ef", "    gh"]       # colour codes never count as width
        store.close()


# --------------------------------------------------------------------------- the command

def test_the_command_prints_once_never_writes_and_watch_redraws_until_interrupted():
    with tmp_cwd() as tmp:
        store = project(tmp)
        _, new = runs(store)
        add_app(store, new, "p1", "needs_review", timedelta(hours=-5), title="Apply by hand", company="Aco", message=APPLY)
        store.close()
        digest = lambda: hashlib.sha256((tmp / "state.db").read_bytes()).hexdigest()      # noqa: E731
        before = digest()

        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(data, "ollama_models", return_value=[]):
            assert pending.main(["--root", str(tmp)]) == 0
        assert "Apply by hand @ Aco" in out.getvalue() and "\x1b[" not in out.getvalue()      # not a terminal: no colour codes

        sleeps = []

        def stop_on_second(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                raise KeyboardInterrupt

        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(data, "ollama_models", return_value=[]), patch.object(pending.time, "sleep", stop_on_second):
            assert pending.main(["--root", str(tmp), "--watch", "60"]) == 0
        assert out.getvalue().count("NEEDS YOU") == 2 and sleeps == [60, 60]               # drawn, waited, drawn again, Ctrl+C is a clean exit
        assert digest() == before, "the briefing must only read"


def test_colour_is_only_used_on_a_real_terminal_that_wants_it():
    snap = data.Snapshot(taken_at=NOW, root=Path("."), db_ok=True)
    snap.daemon.state = "stopped"
    assert "\x1b[" not in pending.render(snap, [], color=False, now=NOW)
    assert "\x1b[31m" in pending.render(snap, [], color=True, now=NOW)                    # the STOPPED warning is red
    src = Path(pending.__file__).read_text(encoding="utf-8")
    assert "NO_COLOR" in src and "isatty" in src


# --------------------------------------------------------------------------- the PowerShell side

def test_the_desk_script_is_valid_ascii_and_free_of_the_launch_bugs_found_while_building_it():
    desk = (ROOT / "desk.ps1").read_text(encoding="utf-8")
    setup = (ROOT / "setup.ps1").read_text(encoding="utf-8")
    desk.encode("ascii")                                                                   # Windows PowerShell 5.1 reads BOM-less UTF-8 as ANSI
    assert "-w new --maximized new-tab" in desk, "options must sit in the same segment as the first command or wt opens an extra default tab"
    assert not re.search(r'"-w new --maximized"\s*,', desk)
    assert "cmd.exe /c .\\tui.bat" in desk and "cmd.exe /c tui.bat" not in desk, "cmd does not search the current folder on a hardened Windows"
    assert desk.index("Test-Ollama") < desk.index("-Pane Daemon") and "setup.ps1" in desk.replace("$Setup", "setup.ps1")
    assert desk.find("& $Setup @flags") > desk.find("Waiting for Ollama"), "the daemon is started only after Ollama has been waited for"
    # a pid file survives a reboot and Windows reuses the number: every kill by pid must first check the process is ours
    lines = setup.splitlines()
    kills = [i for i, ln in enumerate(lines) if re.search(r"Stop-Process -Id \$(targetPid|lockProc)", ln)]
    assert kills and all(any("Test-OwnProcess" in lines[j] for j in range(max(0, i - 6), i + 1)) for i in kills), "Stop-Process on an unverified pid"
    assert "Get-Process -Id $existingPid" not in setup and "AutoApplierDesk.vbs" in setup and "[switch]$SkipChecks" in setup and "-or $SkipChecks" in setup


def test_enabling_the_desk_replaces_the_old_login_entry_and_touches_nothing_else():
    exe = shutil.which("powershell.exe") or shutil.which("powershell")
    if not exe:
        return
    import tempfile

    def run(env, *flags):
        r = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "setup.ps1"), *flags],
                           capture_output=True, text=True, timeout=120, env=env)
        assert r.returncode == 0, r.stdout + r.stderr
        return r.stdout

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as fake:
        startup = Path(fake) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"          # APPDATA points here: the real folder is never touched
        startup.mkdir(parents=True)
        (startup / "AutoJobApplierAll.vbs").write_text("old entry", encoding="utf-8")
        (startup / "Other.lnk").write_text("someone else's", encoding="utf-8")
        env = {**os.environ, "APPDATA": fake}

        out = run(env, "-EnableDesk")
        assert "ENABLED" in out and "Removed the old hidden login entry" in out
        assert sorted(p.name for p in startup.iterdir()) == ["AutoApplierDesk.vbs", "Other.lnk"]             # the old entry is gone, a stranger's is not
        vbs = (startup / "AutoApplierDesk.vbs").read_text(encoding="ascii")
        assert vbs.splitlines()[0] == 'Set objShell = CreateObject("Wscript.Shell")'
        assert f'-File ""{ROOT / "desk.ps1"}""' in vbs and "-WindowStyle Hidden" in vbs and vbs.rstrip().endswith(", 0, False") and "-DaemonOnly" not in vbs
        assert "Desk at login: ENABLED" in run(env, "-Status")

        run(env, "-EnableDesk", "-DaemonOnly")
        assert '-File ""' in (startup / "AutoApplierDesk.vbs").read_text(encoding="ascii") and "-DaemonOnly" in (startup / "AutoApplierDesk.vbs").read_text(encoding="ascii")

        assert "DISABLED" in run(env, "-DisableDesk") and sorted(p.name for p in startup.iterdir()) == ["Other.lnk"]
        assert "Already disabled" in run(env, "-DisableDesk")                                                  # idempotent


def test_both_scripts_parse_in_windows_powershell():
    exe = shutil.which("powershell.exe") or shutil.which("powershell")
    if not exe:
        return
    for name in ("desk.ps1", "setup.ps1"):
        script = (
            "$e = $null; $t = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
            f"'{ROOT / name}', [ref]$t, [ref]$e); if ($e.Count) {{ $e | ForEach-Object {{ $_.Message }}; exit 1 }}"
        )
        r = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"{name} does not parse: {r.stdout} {r.stderr}"
