"""Which kinds of application site may be submitted to unattended -- and the CLI to manage that.

A *site type* is an ATS platform (Greenhouse, Lever, Workday, ...) or, for anything else, the company's domain. A
site type is on **probation** until it has earned trust: the application is filled in and a review bundle is saved
(form screenshot, answers, fit assessment), but nothing is submitted. A site type becomes trusted when

  * you approve it (``python sites.py approve greenhouse``),
  * it has been filled in cleanly for ``clean_runs_to_trust`` different jobs (dry runs count), or
  * one application there has already been confirmed as submitted.

    python sites.py list                  every site type seen, its trust state, and what is waiting on it
    python sites.py approve <site>        trust it, and release the jobs that were parked on probation
    python sites.py revoke <site>         put it back on probation
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.parse
from pathlib import Path

# (host suffix, site type): the platforms whose application flow is the same for every employer.
ATS_HOSTS = (
    ("greenhouse.io", "greenhouse"), ("lever.co", "lever"), ("ashbyhq.com", "ashby"), ("myworkdayjobs.com", "workday"),
    ("myworkday.com", "workday"), ("smartrecruiters.com", "smartrecruiters"), ("workable.com", "workable"),
    ("icims.com", "icims"), ("taleo.net", "taleo"), ("jobvite.com", "jobvite"), ("bamboohr.com", "bamboohr"),
    ("recruitee.com", "recruitee"), ("teamtailor.com", "teamtailor"), ("breezy.hr", "breezy"), ("personio.de", "personio"),
    ("personio.com", "personio"), ("linkedin.com", "linkedin"), ("joinhandshake.com", "handshake"),
)
_SECOND_LEVEL = {"co", "com", "org", "net", "gov", "edu", "ac"}

DEFAULT_PROBATION = {"enabled": True, "clean_runs_to_trust": 3}


def site_fingerprint(url: str) -> str:
    """'https://boards.greenhouse.io/acme/jobs/1' -> 'greenhouse';  'https://jobs.acme.co.uk/x' -> 'acme.co.uk'."""
    host = (urllib.parse.urlparse(url or "").hostname or "").lower().strip(".")
    if not host:
        return "unknown"
    for suffix, name in ATS_HOSTS:
        if host == suffix or host.endswith("." + suffix):
            return name
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", host) or ":" in host:      # an IP address is its own site, not "0.1"
        return host
    labels = host.split(".")
    if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def main(argv: list[str] | None = None) -> int:
    from state import StateStore  # imported here so `import sites` stays dependency-free for the agent

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="config.yaml")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    for name in ("approve", "revoke"):
        sub.add_parser(name).add_argument("site")
    args = parser.parse_args(argv)

    cfg: dict = {}
    if Path(args.config).is_file():
        import yaml

        cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    needed = {**DEFAULT_PROBATION, **(cfg.get("agent", {}).get("probation") or {})}["clean_runs_to_trust"]
    store = StateStore(cfg.get("daemon", {}).get("state_db", "state.db"))
    try:
        if args.cmd == "list":
            rows = store.list_sites()
            if not rows:
                print("No site has been seen yet.")
                return 0
            print(f"{'site':26s} {'state':10s} {'clean runs':>10s} {'submitted':>9s}  parked on probation")
            for r in rows:
                state = "trusted" if store.site_is_trusted(r["site_key"], needed) else "probation"
                print(f"{r['site_key']:26s} {state:10s} {r['clean_runs']:>6d}/{needed:<3d} {r['confirmed_submissions']:>9d}  {r['parked']}")
            print("\nReview what a site would submit in applications/<job id>/ (form_snapshot.png, answers.json, fit.json), then:"
                  "\n  python sites.py approve <site>")
            return 0
        site = args.site.strip().lower()
        if args.cmd == "approve":
            store.approve_site(site)
            released = store.release_probation(site)
            print(f"'{site}' is trusted. {released} job(s) that were waiting on probation can be queued again.")
        else:
            store.revoke_site(site)
            print(f"'{site}' is back on probation.")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
