"""The dashboard itself (Textual). All content comes from tui.data / tui.render; this file only lays it out and handles keys.

Read-only by design: nothing here writes to the project or controls the daemon.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import DataTable, Footer, RichLog, Static

from . import data, render

REFRESH_SECONDS = 2.0


def open_path(path: str | Path) -> bool:
    """Open a file or folder with the system's default program (a screenshot viewer, Explorer)."""
    path = str(path)
    if not os.path.exists(path):
        return False
    try:
        if os.name == "nt":
            os.startfile(path)                                    # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True
    except OSError:
        return False


class JobScreen(Screen):
    """One job: outcome, fit verdict, answers and the agent's step-by-step flow."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("o", "open_shot", "Open screenshot"),
        Binding("f", "open_folder", "Open folder"),
    ]

    def __init__(self, root: Path, job_id: str) -> None:
        super().__init__()
        self.root, self.job_id = root, job_id
        self.detail: dict = {}

    def compose(self) -> ComposeResult:
        yield Static(id="summary", classes="panel")
        yield DataTable(id="steps", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        self.detail = data.job_detail(self.root, self.job_id)
        self.query_one("#summary", Static).update(render.render_job(self.detail))
        self.query_one("#summary", Static).border_title = "Job"
        table = self.query_one("#steps", DataTable)
        table.border_title = "What the agent did (◆ = chosen by a model)"
        table.add_columns("#", "Page", "Action", "Decided by", "Why")
        for step in self.detail.get("steps") or []:
            table.add_row(*render.step_cells(step), key=str(step.get("step")))
        if not self.detail.get("steps"):
            table.add_row(Text(""), Text(""), Text("no agent steps recorded for this job", style="dim"), Text(""), Text(""))
        table.focus()

    def action_back(self) -> None:
        self.app.pop_screen()

    def _selected_step(self) -> str | None:
        table = self.query_one("#steps", DataTable)
        if not table.row_count:
            return None
        try:
            return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return None

    def action_open_shot(self) -> None:
        step = self._selected_step()
        shots = self.detail.get("shots") or []
        wanted = next((s for s in shots if step and Path(s).stem == f"step_{int(step):02d}"), None) if step and step.isdigit() else None
        if not (wanted or shots) or not open_path(wanted or shots[-1]):
            self.app.notify("No screenshot for this step.", severity="warning")

    def action_open_folder(self) -> None:
        if not open_path(self.detail.get("folder", "")):
            self.app.notify("This job has no folder yet.", severity="warning")


class LogScreen(Screen):
    """The tail of daemon.log, refreshed every couple of seconds."""

    BINDINGS = [Binding("escape", "back", "Back")]

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root

    def compose(self) -> ComposeResult:
        yield RichLog(id="log", wrap=True, highlight=False, markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#log", RichLog).border_title = "daemon.log"
        self.refresh_log()
        self.set_interval(REFRESH_SECONDS, self.refresh_log)

    def refresh_log(self) -> None:
        log = self.query_one("#log", RichLog)
        log.clear()
        for line in data.tail_lines(self.root / "daemon.log", 250):
            style = "red" if "Error" in line or "error" in line else "dim" if "Skipping" in line else "bold" if "Processing" in line else "cyan" if line.startswith("[Daemon]") else ""
            log.write(Text(line, style=style))

    def action_back(self) -> None:
        self.app.pop_screen()


class VetoScreen(Screen):
    """Every posting skipped for fit, or parked because the models disagree: the ones that need you first."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("a", "copy_approve", "Copy 'approve' command"),
        Binding("x", "copy_reject", "Copy 'reject' command"),
    ]

    def compose(self) -> ComposeResult:
        yield Static(
            Text("Skipped for fit, and parked where the models disagree. Enter shows the reasoning; the command to decide is "
                 "python fit_review.py approve|reject <job>, and a / x copy it for the selected row.", style="dim"),
            id="hint", classes="panel",
        )
        yield DataTable(id="vetoes", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#hint", Static).border_title = "Fit decisions"
        table = self.query_one("#vetoes", DataTable)
        table.border_title = "Needs you first, then the newest"
        table.add_columns("When", "Job", "Company", "Fit", "Where it stands", "Why")
        self.refresh_rows()
        self.set_interval(REFRESH_SECONDS, self.refresh_rows)
        table.focus()

    def refresh_rows(self) -> None:
        table = self.query_one("#vetoes", DataTable)
        snap = getattr(self.app, "snap", None)
        rows = snap.vetoes if snap else []
        keep = self._selected()
        table.clear()
        for i, row in enumerate(rows):
            table.add_row(*render.veto_cells(row, snap.taken_at), key=row.job_id)
            if row.job_id == keep:
                table.move_cursor(row=i)
        if not rows:
            table.add_row(Text(""), Text("nothing skipped for fit yet", style="dim"), Text(""), Text(""), Text(""), Text(""))

    def _selected(self) -> str | None:
        table = self.query_one("#vetoes", DataTable)
        if not table.row_count:
            return None
        try:
            return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return None

    def _copy(self, verb: str) -> None:
        job_id = self._selected()
        if not job_id:
            return
        command = f"python fit_review.py {verb} {job_id}"
        try:
            self.app.copy_to_clipboard(command)
            self.app.notify(f"Copied: {command}")
        except Exception:                                    # no clipboard here: show it so it can be typed
            self.app.notify(command)

    def action_copy_approve(self) -> None:
        self._copy("approve")

    def action_copy_reject(self) -> None:
        self._copy("reject")

    def action_back(self) -> None:
        self.app.pop_screen()


class AutoApplierApp(App):
    TITLE = "Auto-Applier"
    CSS = """
    Screen { layout: vertical; }
    #top { height: 1; padding: 0 1; background: $boost; }
    #row1 { height: 16; }
    .panel { border: round $primary-darken-1; padding: 0 1; }
    #today, #funnel, #models { width: 1fr; height: 100%; }
    #now { height: 6; }
    #recent { height: 1fr; border: round $primary-darken-1; }
    #needs { height: 10; }
    #hint { height: auto; }
    #vetoes { height: 1fr; border: round $primary-darken-1; }
    #summary { height: auto; max-height: 24; }
    #steps { height: 1fr; border: round $primary-darken-1; }
    RichLog { border: round $primary-darken-1; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh_now", "Refresh"),
        Binding("l", "show_log", "Daemon log"),
        Binding("v", "show_vetoes", "Fit vetoes"),
    ]

    def __init__(self, root: str | Path, snapshot_fn: Callable[[Path], data.Snapshot] | None = None) -> None:
        super().__init__()
        self.root = Path(root)
        self.snapshot_fn = snapshot_fn or (lambda r: data.snapshot(r))
        self.snap: data.Snapshot | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="top")
        with Horizontal(id="row1"):
            yield Static(id="today", classes="panel")
            yield Static(id="funnel", classes="panel")
            yield Static(id="models", classes="panel")
        yield Static(id="now", classes="panel")
        yield DataTable(id="recent", cursor_type="row", zebra_stripes=True)
        yield Static(id="needs", classes="panel")
        yield Footer()

    def on_mount(self) -> None:
        for widget_id, title in (("today", "Today"), ("funnel", "Latest cycle"), ("models", "Models"), ("now", "Now"), ("needs", "Needs you")):
            self.query_one(f"#{widget_id}").border_title = title
        table = self.query_one("#recent", DataTable)
        table.border_title = "Recent applications  (↑↓ select, Enter opens the job)"
        table.add_columns("When", "Job", "Company", "Site", "Result", "Detail")
        self.load()
        self.set_interval(REFRESH_SECONDS, self.load)

    # ---- data -----------------------------------------------------------------

    @work(thread=True, exclusive=True)
    def load(self) -> None:
        try:
            snap = self.snapshot_fn(self.root)
        except Exception as e:                                       # a bad moment (file mid-write) must not kill the dashboard
            self.call_from_thread(self.query_one("#top", Static).update, Text(f"could not read the project: {e}", style="red"))
            return
        self.call_from_thread(self.apply, snap)

    def apply(self, snap: data.Snapshot) -> None:
        self.snap = snap
        top = render.render_header(snap)
        top.append(f"   │   updated {datetime.now().strftime('%H:%M:%S')}", style="dim")
        self.query_one("#top", Static).update(top)
        self.query_one("#today", Static).update(render.render_today(snap))
        self.query_one("#funnel", Static).update(render.render_funnel(snap))
        self.query_one("#models", Static).update(render.render_models(snap))
        self.query_one("#now", Static).update(render.render_now(snap))
        self.query_one("#needs", Static).update(render.render_needs(snap))

        table = self.query_one("#recent", DataTable)
        selected = None
        if table.row_count:
            try:
                selected = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            except Exception:
                selected = None
        table.clear()
        for i, row in enumerate(snap.recent):
            table.add_row(*render.app_cells(row, snap.taken_at), key=row.job_id)
            if row.job_id == selected:
                table.move_cursor(row=i)

    # ---- actions --------------------------------------------------------------

    def action_refresh_now(self) -> None:
        self.load()

    def action_show_log(self) -> None:
        self.push_screen(LogScreen(self.root))

    def action_show_vetoes(self) -> None:
        if not isinstance(self.screen, (JobScreen, LogScreen, VetoScreen)):
            self.push_screen(VetoScreen())

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id in ("recent", "vetoes") and event.row_key.value and not isinstance(self.screen, (JobScreen, LogScreen)):
            self.push_screen(JobScreen(self.root, event.row_key.value))
