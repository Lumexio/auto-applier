"""Drives the dashboard headlessly (Textual's test pilot) against the demo project: does it load, show rows, open a job and the log?"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable

from textual.widgets import DataTable

from . import data
from .app import AutoApplierApp, JobScreen, LogScreen, VetoScreen


async def _scenario(root: Path, snapshot_fn: Callable | None, shots: Path | None) -> list[tuple[str, bool]]:
    results: list[tuple[str, bool]] = []

    def check(name: str, ok: bool) -> None:
        results.append((name, bool(ok)))

    app = AutoApplierApp(root, snapshot_fn)
    async with app.run_test(size=(170, 52)) as pilot:
        for _ in range(60):                                               # the first snapshot is read on a worker thread
            if app.snap is not None:
                break
            await pilot.pause(0.1)
        await pilot.pause(0.3)
        check("first snapshot arrived", app.snap is not None)
        table = app.query_one("#recent", DataTable)
        check("recent applications are listed", table.row_count == len(app.snap.recent) and table.row_count > 0)
        check("the job in progress is found", app.snap.now is not None and app.snap.now.live)
        if shots:
            shots.mkdir(parents=True, exist_ok=True)
            app.save_screenshot(filename="dashboard.svg", path=str(shots))

        await pilot.press("down", "enter")                                # open the second row
        await pilot.pause(0.4)
        check("Enter opens the job screen", isinstance(app.screen, JobScreen))
        if isinstance(app.screen, JobScreen):
            check("the job screen loaded the job", app.screen.detail.get("app") is not None)
            if shots:
                app.save_screenshot(filename="job.svg", path=str(shots))
            await pilot.press("escape")
            await pilot.pause(0.2)
        check("Escape returns to the dashboard", not isinstance(app.screen, (JobScreen, LogScreen)))

        await pilot.press("down", "enter")                                # the third row: a submitted job with a recorded agent flow
        await pilot.pause(0.4)
        if isinstance(app.screen, JobScreen):
            steps = app.screen.query_one("#steps", DataTable)
            check("a job's agent steps are listed", steps.row_count == len(app.screen.detail.get("steps") or []) and steps.row_count >= 3)
            if shots:
                app.save_screenshot(filename="job_with_steps.svg", path=str(shots))
            await pilot.press("escape")
            await pilot.pause(0.2)
        else:
            check("a job's agent steps are listed", False)

        await pilot.press("v")
        await pilot.pause(0.5)
        check("v opens the fit decisions", isinstance(app.screen, VetoScreen))
        if isinstance(app.screen, VetoScreen):
            veto_table = app.screen.query_one("#vetoes", DataTable)
            check("they are listed, the one that needs you first", veto_table.row_count == len(app.snap.vetoes) >= 4 and app.snap.vetoes[0].where == "disputed")
            if shots:
                app.save_screenshot(filename="vetoes.svg", path=str(shots))
            await pilot.press("a")                                                # copy the approve command: must not crash without a clipboard
            await pilot.press("enter")
            await pilot.pause(0.4)
            check("Enter on a veto opens the job with both models' opinions",
                  isinstance(app.screen, JobScreen) and ((app.screen.detail.get("fit") or {}).get("review") or {}).get("agreement") == "disputed")
            if shots and isinstance(app.screen, JobScreen):
                app.save_screenshot(filename="veto_job.svg", path=str(shots))
            await pilot.press("escape")
            await pilot.pause(0.2)
            await pilot.press("escape")
            await pilot.pause(0.2)
        check("Escape leaves the fit decisions", not isinstance(app.screen, (JobScreen, LogScreen, VetoScreen)))

        await pilot.press("l")
        await pilot.pause(0.4)
        check("l opens the daemon log", isinstance(app.screen, LogScreen))
        if shots and isinstance(app.screen, LogScreen):
            app.save_screenshot(filename="log.svg", path=str(shots))
        await pilot.press("escape")
        await pilot.pause(0.2)

        before = app.snap
        await pilot.press("r")
        await pilot.pause(0.8)
        check("r refreshes", app.snap is not None and app.snap is not before)
        await pilot.press("q")
    return results


def run(root: Path, snapshot_fn: Callable | None = None, shots: Path | None = None) -> int:
    results = asyncio.run(_scenario(root, snapshot_fn or (lambda r: data.snapshot(r)), shots))
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    failed = [n for n, ok in results if not ok]
    print("selftest:", "all passed" if not failed else f"{len(failed)} failed")
    return 1 if failed else 0
