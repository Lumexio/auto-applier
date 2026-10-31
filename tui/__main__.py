"""python -m tui [--project DIR] [--once] [--demo] [--selftest]"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import data, render


def print_once(snap: data.Snapshot, console: Console | None = None) -> None:
    """The whole dashboard as plain text, once. Needs only `rich`, so it works without Textual."""
    console = console or Console()
    console.print(render.render_header(snap))
    for title, text in (("Today", render.render_today(snap)), ("Latest cycle", render.render_funnel(snap)), ("Models", render.render_models(snap)),
                        ("Now", render.render_now(snap)), ("Needs you", render.render_needs(snap))):
        console.print(Panel(text, title=title, title_align="left", border_style="blue"))
    table = Table(title="Recent applications", title_justify="left", header_style="bold")
    for col in ("When", "Job", "Company", "Site", "Result", "Detail"):
        table.add_column(col)
    for row in snap.recent[:15]:
        table.add_row(*render.app_cells(row, snap.taken_at))
    console.print(table)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tui", description="Terminal dashboard for the auto-applier (read-only).")
    ap.add_argument("--project", default=str(Path(__file__).resolve().parent.parent), help="the auto-applier folder (default: this one)")
    ap.add_argument("--once", action="store_true", help="print one snapshot as text and exit (no Textual needed)")
    ap.add_argument("--demo", action="store_true", help="show made-up data in a temporary folder instead of the real project")
    ap.add_argument("--selftest", action="store_true", help="drive the dashboard headlessly against demo data and report PASS/FAIL")
    ap.add_argument("--shots", help="with --selftest: also save SVG screenshots of each screen into this folder")
    args = ap.parse_args(argv)

    root = Path(args.project)
    snapshot_fn = None
    if args.demo or args.selftest:
        from . import demo
        root, snapshot_fn = demo.build(), demo.demo_snapshot
    if args.once:
        print_once(snapshot_fn(root) if snapshot_fn else data.snapshot(root))
        return 0
    try:
        from .app import AutoApplierApp
    except ImportError as e:
        print(f"Textual is not available here ({e}).\nStart the dashboard with tui.bat, which keeps Textual in its own environment.\nA text-only snapshot follows.\n")
        print_once(snapshot_fn(root) if snapshot_fn else data.snapshot(root))
        return 1
    if args.selftest:
        from .selftest import run
        return run(root, snapshot_fn, Path(args.shots) if args.shots else None)
    AutoApplierApp(root, snapshot_fn).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
