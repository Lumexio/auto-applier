"""Serve the web interface:  python -m gui [--open] [--port 8765]

Everything that is waiting for you, on one page: the questions the forms asked (with an answer drafted from your own
resumes when there is one), fit decisions the models disagreed about, sites on probation, jobs you have to finish by hand.
It listens on 127.0.0.1 only. Starting it twice is fine: the second start just opens the first one.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import webbrowser
from pathlib import Path

from gui.server import APP_NAME, DeskServer
from gui.service import Desk

DEFAULT_PORT = 8765


def already_serving(port: int) -> bool:
    """True when this program is already answering on ``port`` (as opposed to some other program)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=2) as reply:
            return json.loads(reply.read().decode("utf-8")).get("app") == APP_NAME
    except (OSError, ValueError):
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"default {DEFAULT_PORT}")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent.parent), help="the project folder (default: this one)")
    parser.add_argument("--open", action="store_true", help="open it in your default browser")
    parser.add_argument("--demo", action="store_true", help="look around on made-up data in a temporary folder (nothing real is touched)")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    root = args.root
    if args.demo:
        from gui import demo

        root = str(demo.build())
        print(f"Made-up data in {root}")
    try:
        server = DeskServer(Desk(root), port=args.port)
    except OSError:
        if already_serving(args.port):
            url = f"http://127.0.0.1:{args.port}/"
            print(f"Already running at {url}")
            if args.open:
                webbrowser.open(url)
            return 0
        print(f"Port {args.port} is taken by another program. Try: python -m gui --port {args.port + 1}")
        return 1

    print(f"Auto-Applier desk: {server.url}  (this computer only; Ctrl+C stops it)")
    if args.open:
        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
