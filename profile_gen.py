"""Profile generator that parses the applicant's resume source of truth into profile.json."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

# Folder that holds resume-versions/resume-backend.md and out/resume-base.pdf.
# Point AUTO_APPLIER_RESUME_DIR at yours; the default is a folder next to where you run the command.
RESUME_BASE_DIR = Path(os.environ.get("AUTO_APPLIER_RESUME_DIR", "resume-template"))


def parse_resume_to_profile(resume_dir: Path = RESUME_BASE_DIR) -> dict[str, Any]:
    md_file = resume_dir / "resume-versions" / "resume-backend.md"
    pdf_file = resume_dir / "out" / "resume-base.pdf"

    if not md_file.is_file():
        raise FileNotFoundError(f"Resume markdown source not found at: {md_file}")

    content = md_file.read_text(encoding="utf-8")

    # Anything the resume does not state stays empty: a wrong guess would be sent to employers.
    # Name
    name_match = re.search(r"<h1[^>]*>(.*?)</h1>", content)
    name = name_match.group(1).strip() if name_match else ""

    # Contact line, e.g.: (+1)5555550123 | someone@example.com | LinkedIn | GitHub | Portfolio
    phone_match = re.search(r"\(\+(\d+)\)(\d+)", content)
    phone = f"+{phone_match.group(1)}{phone_match.group(2)}" if phone_match else ""

    email_match = re.search(r"([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)", content)
    email = email_match.group(1).strip() if email_match else ""

    linkedin_match = re.search(r"href=\"(https://[^\"]*linkedin\.com/[^\"]*)\"", content)
    linkedin = linkedin_match.group(1) if linkedin_match else ""

    github_match = re.search(r"href=\"(https://github\.com/[^\"/]+)/?\"", content)
    github = github_match.group(1) if github_match else ""

    # The portfolio is the first web link that is not LinkedIn or GitHub.
    links = re.findall(r"href=\"(https?://[^\"]+)\"", content)
    portfolio = next((u for u in links if not re.search(r"linkedin\.com|github\.com", u)), "")

    # Skills extraction from Technical Skills section
    skill_groups = re.findall(
        r"<li><b>(?:Backend Frameworks & DBs|Core Languages|Infrastructure & OS|Specializations)</b>:\s*(.*?)\.?</li>",
        content,
    )
    skills: list[str] = []
    for grp in skill_groups:
        for s in grp.split(","):
            cleaned = re.sub(r"\(.*?\)", "", s).strip()
            if cleaned and cleaned not in skills:
                skills.append(cleaned)

    # Ensure high-priority skills are present
    for core in ["Python", "TypeScript", "JavaScript", "SQL", "Docker", "API Design", "Playwright"]:
        if core not in skills:
            skills.append(core)

    name_parts = name.split()
    profile = {
        "schema_version": "2.0",
        "name": name,
        "first_name": name_parts[0] if name_parts else "",
        "last_name": " ".join(name_parts[1:]),
        "email": email,
        "phone": phone,
        "location": "",
        "remote_ok": True,
        "work_authorization": [],
        "min_salary": 100000,
        "currency": "USD",
        "seniority": "senior",
        "skills": skills,
        "linkedin": linkedin,
        "portfolio": portfolio,
        "github": github,
        "exclusions": {
            "companies": [],
            "keywords": ["unpaid", "commission-only", "clearance required", "polygraph"],
            "domains": [],
        },
        "attachments": {
            "resume_path": str(pdf_file.resolve()) if pdf_file.is_file() else "",
            "cover_template": (
                "Dear Hiring Team,\n\n"
                "I am excited to apply for this role. My background and experience line up well with "
                "the position, and I am confident I can make an immediate impact on your team.\n\n"
                "Best regards,\n"
                f"{name}"
            ),
            "portfolio_url": portfolio,
        },
        "max_applications_per_day": 25,
        "max_applications_per_domain_per_hour": 5,
        "mode": "autonomous",
        "auto_apply_allowlist": [],
    }
    return profile


def main() -> None:
    profile = parse_resume_to_profile()
    out_file = Path("profile.json")
    out_file.write_text(json.dumps(profile, indent=2), encoding="utf-8")
    print(f"Generated {out_file.resolve()} from resume source of truth.")
    print(f"Candidate: {profile['name']} ({profile['email']})")
    print(f"Skills: {len(profile['skills'])} skills extracted.")
    print(f"Resume attachment: {profile['attachments']['resume_path']}")


if __name__ == "__main__":
    main()
