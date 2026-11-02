"""Your source of truth: the resumes and notes the application bot trusts, and what it computes from them.

    python source.py init             create the folders (paths.source_dir in config.yaml, me/ by default; never committed)
    python source.py sync [--from P]  copy your resume versions in from the resume-template folder (source.sync_from)
    python source.py show [--all]     what the bot believes: years of work, years per technology, and why
    python source.py check            do your resume versions agree with each other, and with the years they claim?
    python source.py derive           recompute now (this also happens by itself whenever a resume or the month changes)

A value you write in facts.yaml always wins over what is computed here.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import date
from pathlib import Path
from typing import Any

import doc_text
import resume_facts as rf

README = """# Your source of truth

The application bot trusts what is in this folder, and nothing it makes up.

    resumes/      your resume versions: .md or .html (the resume-template layout), plain Markdown / text, Word (.docx) or PDF.
                  `python source.py sync` copies them in from your resume-template folder. A PDF or Word file that sits beside a text
                  version of the same name (resume-base.pdf beside resume-base.md) is its printout and is not read a second time.
                  A PDF or Word file has no headings of its own, so Markdown is the safer source when you have both; PDF needs pypdf.
    experience/   anything else about your work, in your own words (.md): a role, a project, a technology you used.
                  Put a date range on a heading ("## Acme, Backend Engineer (Mar 2019 - Present)") and it counts like a job.
    derived/      written by the program (experience.yaml). Do not edit: change a resume, or state the value in facts.yaml.

Years of experience are computed from the dates next to each job. A framework implies its language (Laravel -> PHP, Vue -> JavaScript).
A technology your resumes list but no dated job names counts as your career length (source.experience.undated_skills in config.yaml).
`python source.py show` prints every number and where it came from; `python source.py check` looks for contradictions.
"""


def load_config(path: str = "config.yaml") -> dict[str, Any]:
    """config.yaml with config.local.yaml (yours, never committed) merged on top."""
    import yaml

    from config_loader import load_config as load_merged

    if not Path(path).is_file():
        raise SystemExit(f"Could not read {path}: no such file")
    try:
        return load_merged(path)
    except (OSError, ValueError, yaml.YAMLError) as e:
        raise SystemExit(f"Could not read {path}: {e}")


def need_source_dir(cfg: dict[str, Any]) -> Path:
    root = rf.source_dir(cfg)
    if root is None:
        raise SystemExit("paths.source_dir is not set in config.yaml (use: source_dir: \"me\" under paths:)")
    return root


def cmd_init(cfg: dict[str, Any], _args: argparse.Namespace) -> int:
    root = need_source_dir(cfg)
    for sub in ("resumes", "experience"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    readme = root / "README.md"
    if not readme.exists():
        readme.write_text(README, encoding="utf-8")
    print(f"Created {root}{'/' if not str(root).endswith('/') else ''} (resumes/, experience/). Next: python source.py sync   (or copy your resumes into {root / 'resumes'})")
    return 0


def cmd_sync(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    root = need_source_dir(cfg)
    given = args.origin or rf.source_cfg(cfg)["sync_from"]
    if not given:
        raise SystemExit("Say where your resumes are: python source.py sync --from <folder>   (or set source.sync_from in config.local.yaml)")
    origin = Path(given)
    if not origin.is_dir():
        raise SystemExit(f"{origin} does not exist. Pass the folder your resumes are in: python source.py sync --from <folder>")
    dest = root / "resumes"
    dest.mkdir(parents=True, exist_ok=True)
    wanted: list[tuple[Path, str]] = []
    for md in sorted((origin / "resume-versions").glob("*.md")) if (origin / "resume-versions").is_dir() else []:
        wanted.append((md, md.name))
    base = origin / "resume.html"
    if base.is_file():
        wanted.append((base, "resume-base.html"))
    for pdf in sorted((origin / "out").glob("*.pdf")) if (origin / "out").is_dir() else []:
        wanted.append((pdf, pdf.name))
    if not wanted:
        raise SystemExit(f"No resume files found under {origin} (looked for resume-versions/*.md, resume.html, out/*.pdf)")
    added = updated = same = 0
    for src, name in wanted:
        target = dest / name
        if not target.exists():
            added += 1
        elif target.read_bytes() != src.read_bytes():
            updated += 1
        else:
            same += 1
            continue
        shutil.copy2(src, target)
        print(f"  {'added  ' if added and not updated else 'copied '} {name}")
    print(f"Synced from {origin}: {added} added, {updated} updated, {same} unchanged. Nothing was deleted.")
    return cmd_derive(cfg, args)


def summarize(data: dict[str, Any]) -> None:
    exp = data.get("experience") or {}
    print(f"Computed from {len(data.get('sources') or [])} file(s): {exp.get('total_years')} years of work "
          f"(count: {(data.get('policy') or {}).get('count')}), {len(exp.get('years_by_skill') or {})} technologies with years.")
    for c in data.get("conflicts") or []:
        print(f"  ! versions disagree on a date, the shorter is used: {c}")
    for n in data.get("notices") or []:
        print(f"  ! {n}")
    for u in data.get("unread") or []:
        print(f"  ! could not read {u}")


def cmd_derive(cfg: dict[str, Any], _args: argparse.Namespace) -> int:
    need_source_dir(cfg)
    data = rf.ensure_derived(cfg, force=True)
    if not data:
        print(f"No resumes found in {rf.source_dir(cfg) / 'resumes'}. Run: python source.py sync   (or copy your resumes there)")
        return 1
    summarize(data)
    print(f"Written to {rf.derived_path(cfg)}")
    return 0


def cmd_show(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    need_source_dir(cfg)
    data = rf.ensure_derived(cfg)
    if not data:
        print("Nothing to show: no resumes found. Run: python source.py sync")
        return 1
    exp = data["experience"]
    prov = data.get("provenance") or {}
    pol = data.get("policy") or {}
    print(f"Source of truth: {', '.join(data.get('sources') or [])}")
    print(f"Policy: years count {pol.get('count')} dates; a technology listed but not in any dated job: {pol.get('undated_skills')}")
    print(f"\nWork: {exp['total_years']} years")
    for j in data.get("jobs") or []:
        print(f"  {j['dates']}  {j['years']:>4} y  {j['title']} @ {j['company']}")
    for p in data.get("projects") or []:
        print(f"  {p['dates']}  (project, {'counted' if pol.get('count') == 'jobs_and_projects' else 'not counted'}) {p['title']}")
    skills = list((exp.get("years_by_skill") or {}).items())
    shown = skills if args.all else skills[:30]
    print(f"\nYears by technology (a form gets the rounded number){'' if args.all else f', top {len(shown)} of {len(skills)}; --all for the rest'}:")
    for name, value in shown:
        print(f"  {name:<22} {value:>4}  -> {rf_round(value):>2}   {prov.get(f'experience.years_by_skill.{name}', '')[:96]}")
    edu = data.get("education") or {}
    if edu:
        print("\nEducation (of your highest finished degree; a form gets these words):")
        for key, value in edu.items():
            print(f"  {key:<24} {value}")
    counts = {k: v for k, v in exp.items() if k.startswith("companies_")}
    if counts:
        print("\nDifferent employers among the dated jobs:")
        for key, value in counts.items():
            print(f"  {key:<46} {value:>2}   {prov.get(f'experience.{key}', '')[:70]}")
    for c in data.get("conflicts") or []:
        print(f"\n! versions disagree on a date: {c}")
    for n in data.get("notices") or []:
        print(f"\n! {n}")
    print("\nYour facts.yaml wins over all of this. To override one, set it there, e.g.  experience: {years_by_skill: {Python: 3}}")
    return 0


def rf_round(value: Any) -> str:
    n = float(value)
    return str(int(n + 0.5)) if n >= 0.5 else "0"


def cmd_check(cfg: dict[str, Any], _args: argparse.Namespace) -> int:
    root = need_source_dir(cfg)
    data = rf.ensure_derived(cfg)
    if not data:
        print(f"No resumes in {root / 'resumes'}. Run: python source.py sync")
        return 1
    problems = 0
    career = data["experience"]["total_years"]
    print(f"{len(data['sources'])} source file(s); {career} years of work by the dates written next to each job.")
    for c in data.get("conflicts") or []:
        problems += 1
        print(f"  PROBLEM  two versions disagree on a date, the shorter is used: {c}")
    for n in data.get("notices") or []:
        problems += 1
        print(f"  PROBLEM  {n}")
    summary = (data.get("narrative") or {}).get("summary", "")
    for m in re.finditer(r"(\d+)\s*\+?\s*years", summary):
        claimed = int(m.group(1))
        if claimed > career + 0.5:
            problems += 1
            print(f"  PROBLEM  your resume says {claimed}+ years but the job dates add up to {career}. A form will get {rf_round(career)}; change the resume or the dates.")
        else:
            print(f"  ok       the resume says {claimed}+ years, the dates add up to {career}")
    undated = [k for k, why in (data.get("provenance") or {}).items() if "no dated job names it" in why]
    if undated:
        print(f"  note     {len(undated)} listed technologies have no dated job behind them and count as {career} years each: "
              f"{', '.join(k.removeprefix('experience.years_by_skill.') for k in undated[:12])}{'...' if len(undated) > 12 else ''}")
    for u in data.get("unread") or []:
        problems += 1
        print(f"  PROBLEM  could not read {u}")
    twins = sorted(p.name for p in (root / "resumes").glob("*") if p.suffix.lower() in rf.DOCUMENT_SUFFIXES and doc_text.twin_exists(p))
    if twins:
        print(f"  note     {', '.join(twins)} {'is' if len(twins) == 1 else 'are'} the printout of a text version beside {'it' if len(twins) == 1 else 'them'}, so only the text version is read")
    unread_kinds = sorted({p.suffix.lower() for p in (root / "resumes").glob("*") if p.suffix.lower() in rf.UNREAD_SUFFIXES})
    if unread_kinds:
        print(f"  note     {', '.join(unread_kinds)} files are not read (the old Word format); save them as .docx")
    print("All consistent." if not problems else f"{problems} problem(s).")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")       # type: ignore[attr-defined]
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    p = sub.add_parser("sync")
    p.add_argument("--from", dest="origin", default=None, help="the resume-template folder (default: source.sync_from)")
    sub.add_parser("derive")
    p = sub.add_parser("show")
    p.add_argument("--all", action="store_true")
    sub.add_parser("check")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    return {"init": cmd_init, "sync": cmd_sync, "derive": cmd_derive, "show": cmd_show, "check": cmd_check}[args.cmd](cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
