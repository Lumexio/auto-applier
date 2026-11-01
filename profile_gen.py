"""Profile generator: turns your resume into profile.json, and never invents a detail the resume does not state.

    python profile_gen.py                              find your resume (see below) and write profile.json
    python profile_gen.py --resume path/to/resume.md   use this file, or a folder that holds your resume versions
    python profile_gen.py --location "Lisbon, Portugal" --authorized PT --min-salary 60000 --currency EUR --seniority mid --remote yes

A resume cannot say where you live, where you may work, the pay you need, your level or whether remote is fine. Those stay empty
unless you pass them here (or edit profile.json), and the command lists what is still empty: a wrong guess would be sent to
employers. The generated profile starts in ``dry_run`` (forms are filled in, nothing is submitted); change ``mode`` in profile.json
when you have watched it work. An existing profile.json is never overwritten without --force.

Where it looks for the resume when you do not say: $AUTO_APPLIER_RESUME_DIR (default ./resume-template), then me/resumes.
It reads the resume-template layout (resume-versions/*.md) as well as plain Markdown or HTML.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

from resume_facts import RESUME_SUFFIXES, parse_resume, split_terms

# Folder that holds your resume versions. Point AUTO_APPLIER_RESUME_DIR at yours; the default is a folder next to where you run the command.
RESUME_BASE_DIR = Path(os.environ.get("AUTO_APPLIER_RESUME_DIR", "resume-template"))
SOURCE_RESUMES = Path("me") / "resumes"                 # the source-of-truth folder (paths.source_dir in config.yaml)
SENIORITY = ("junior", "mid", "senior", "lead", "staff", "principal")
MODES = ("dry_run", "assisted", "autonomous")

_EMAIL = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
_PHONE_TEMPLATE = re.compile(r"\(\+(\d+)\)\s*(\d+)")
_PHONE_ANY = re.compile(r"(?<![\w.])(\+\d[\d\s().-]{7,}\d|\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4})(?![\w.])")     # "+351 912 345 678" or "555-555-0123", never "2015 - 2021"


def _contact(text: str) -> dict[str, str]:
    """The details a resume's header carries: name, email, phone, links. Anything not there stays ''."""
    name_match = re.search(r"<h1[^>]*>(.*?)</h1>", text, re.S) or re.search(r"^#\s+(.+?)\s*$", text, re.M)
    name = re.sub(r"<[^>]+>", "", name_match.group(1)).strip() if name_match else ""

    phone = ""
    if m := _PHONE_TEMPLATE.search(text):                                     # "(+1)5555550123", as the resume template writes it
        phone = f"+{m.group(1)}{m.group(2)}"
    elif m := _PHONE_ANY.search(text):                                       # "+1 555 555 0123", "(555) 555-0123"
        phone = ("+" if m.group(0).lstrip().startswith("+") else "") + re.sub(r"\D", "", m.group(0))

    email_match = _EMAIL.search(text)
    email = email_match.group(0).rstrip(".") if email_match else ""
    urls = re.findall(r"https?://[^\s\"'<>)\]]+", text)
    linkedin = next((u for u in urls if "linkedin.com" in u), "")
    github = next((u.rstrip("/") for u in urls if re.fullmatch(r"https://github\.com/[^/\s]+/?", u)), "")       # the profile link, not a project link
    portfolio = next((u for u in urls if not re.search(r"linkedin\.com|github\.com", u)), "")                  # the first other web link
    return {"name": name, "email": email, "phone": phone, "linkedin": linkedin, "github": github, "portfolio": portfolio}


def _skills(text: str, path: Path) -> list[str]:
    """Technologies the resume lists under 'label: a, b, c' lines (its skills section, whatever the labels are called)."""
    listed = parse_resume(path, date.today()).listed
    if listed:
        return listed
    items = re.findall(r"<li>\s*<b>[^<]{2,60}</b>\s*:\s*(.*?)</li>", text, re.S)                                # resume-template / HTML lines
    items += re.findall(r"^\s*[-*]\s*\*\*[^*\n]{2,60}\*\*\s*:\s*(.+)$", text, re.M)                              # "- **Languages**: Go, Rust"
    out: list[str] = []
    for item in items:
        for term in split_terms(item):
            if term not in out:
                out.append(term)
    return out


def _resume_files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in RESUME_SUFFIXES and not p.name.startswith("."))


def _contact_score(path: Path) -> int:
    return sum(bool(v) for v in _contact(path.read_text(encoding="utf-8", errors="replace")).values())


def _pdf_for(folder: Path, root: Path) -> Path | None:
    for where in (root / "out", folder, root):
        pdfs = sorted(where.glob("*.pdf")) if where.is_dir() else []
        if pdfs:
            return next((p for p in pdfs if "base" in p.name.lower()), pdfs[0])
    return None


def locate_resume(source: Path | str | None = None) -> tuple[Path, Path | None]:
    """(the resume file the details are read from, a PDF of the resume if there is one). Raises FileNotFoundError, listing where it looked."""
    roots = [Path(source)] if source else [RESUME_BASE_DIR, SOURCE_RESUMES]
    for root in roots:
        if root.is_file():
            return root, _pdf_for(root.parent, root.parent)
        legacy = root / "resume-versions" / "resume-backend.md"
        if legacy.is_file():                                                        # the resume-template layout
            return legacy, _pdf_for(legacy.parent, root)
        for folder in (root / "resume-versions", root):
            files = _resume_files(folder)
            if files:
                return max(files, key=_contact_score), _pdf_for(folder, root)        # the version that carries the most contact details
    raise FileNotFoundError(
        "Resume markdown source not found at: " + ", ".join(str(r) for r in roots)
        + ". Pass --resume <file or folder>, set AUTO_APPLIER_RESUME_DIR, or put your resume (.md / .html) in me/resumes (python source.py init).")


def parse_resume_to_profile(
    resume_dir: Path | str | None = None,
    *,
    location: str = "",
    authorized: list[str] | None = None,
    min_salary: int | None = None,
    currency: str = "",
    seniority: str = "",
    remote: bool | None = None,
    mode: str = "dry_run",
    exclude_companies: list[str] | None = None,
    exclude_keywords: list[str] | None = None,
) -> dict[str, Any]:
    resume_file, pdf_file = locate_resume(resume_dir)
    content = resume_file.read_text(encoding="utf-8", errors="replace")

    # Anything the resume does not state stays empty: a wrong guess would be sent to employers.
    contact = _contact(content)
    name = contact["name"]
    name_parts = name.split()
    skills = _skills(content, resume_file)                                           # only what the resume lists: nothing is added for you

    return {
        "schema_version": "2.0",
        "name": name,
        "first_name": name_parts[0] if name_parts else "",
        "last_name": " ".join(name_parts[1:]),
        "email": contact["email"],
        "phone": contact["phone"],
        "location": location,
        "remote_ok": remote,
        "work_authorization": [a.strip().upper() for a in (authorized or []) if a.strip()],
        "min_salary": min_salary,
        "currency": currency.upper(),
        "seniority": seniority.lower(),
        "skills": skills,
        "linkedin": contact["linkedin"],
        "portfolio": contact["portfolio"],
        "github": contact["github"],
        "exclusions": {
            "companies": list(exclude_companies or []),
            "keywords": list(exclude_keywords or []),
            "domains": [],
        },
        "attachments": {
            "resume_path": str(pdf_file.resolve()) if pdf_file else "",
            "cover_template": (
                "Dear Hiring Team,\n\n"
                "I am excited to apply for this role. My background and experience line up well with "
                "the position, and I am confident I can make an immediate impact on your team.\n\n"
                "Best regards,\n"
                f"{name}"
            ),
            "portfolio_url": contact["portfolio"],
        },
        "max_applications_per_day": 25,
        "max_applications_per_domain_per_hour": 5,
        "mode": mode,
        "auto_apply_allowlist": [],
    }


_GAPS = (
    ("name", "your name (the resume has no title line)", "edit profile.json"),
    ("email", "your email", "edit profile.json"),
    ("phone", "your phone number", "edit profile.json"),
    ("location", "where you live", '--location "City, Country"'),
    ("work_authorization", "the countries you may work in", "--authorized MX,US"),
    ("min_salary", "the lowest pay you would take", "--min-salary 80000 --currency USD"),
    ("seniority", "your level (it decides whether junior roles are skipped)", "--seniority " + "|".join(SENIORITY)),
    ("remote_ok", "whether remote work is fine", "--remote yes|no"),
)


def describe_gaps(profile: dict[str, Any]) -> list[str]:
    """What the profile still does not say, as 'what: how to give it'. Empty when nothing is missing."""
    return [f"{what}: {how}" for key, what, how in _GAPS if profile.get(key) in ("", None, [])]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--resume", help="a resume file, or a folder that holds your resume versions")
    ap.add_argument("--out", default="profile.json")
    ap.add_argument("--force", action="store_true", help="overwrite an existing profile.json")
    ap.add_argument("--location", default="", help='where you live, e.g. "Lisbon, Portugal"')
    ap.add_argument("--authorized", default="", help="countries you may work in, comma separated ISO codes, e.g. PT,ES")
    ap.add_argument("--min-salary", type=int, default=None, help="the lowest pay you would take (a number)")
    ap.add_argument("--currency", default="", help="currency of --min-salary, e.g. EUR")
    ap.add_argument("--seniority", choices=SENIORITY, default="")
    ap.add_argument("--remote", choices=("yes", "no"), default=None)
    ap.add_argument("--mode", choices=MODES, default="dry_run", help="dry_run (default) fills forms in but never submits")
    ap.add_argument("--exclude-company", action="append", default=[], help="skip postings from this company (repeatable)")
    ap.add_argument("--exclude-keyword", action="append", default=[], help="skip postings that contain this word or phrase (repeatable)")
    args = ap.parse_args(argv)

    out_file = Path(args.out)
    if out_file.exists() and not args.force:
        print(f"{out_file} already exists and is not touched. Use --force to replace it, or edit it directly.")
        return 1
    try:
        profile = parse_resume_to_profile(
            args.resume, location=args.location, authorized=args.authorized.split(","), min_salary=args.min_salary, currency=args.currency,
            seniority=args.seniority, remote=None if args.remote is None else args.remote == "yes", mode=args.mode,
            exclude_companies=args.exclude_company, exclude_keywords=args.exclude_keyword)
    except FileNotFoundError as e:
        print(e)
        return 1
    out_file.write_text(json.dumps(profile, indent=2), encoding="utf-8")
    print(f"Wrote {out_file.resolve()} from your resume.")
    print(f"Candidate: {profile['name'] or '(no name found)'} ({profile['email'] or 'no email found'})")
    print(f"Skills: {len(profile['skills'])} found in the resume's skills section.")
    print(f"Resume attachment: {profile['attachments']['resume_path'] or '(no PDF found; applications that need an upload will park)'}")
    print(f"Mode: {profile['mode']}" + ("  (forms are filled in, nothing is submitted; set \"mode\" to \"autonomous\" in profile.json when you trust it)" if profile["mode"] == "dry_run" else ""))
    gaps = describe_gaps(profile)
    if gaps:
        print("\nStill empty, because the resume does not say (add with the option shown, or edit profile.json):")
        for g in gaps:
            print(f"  - {g}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
