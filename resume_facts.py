"""Facts computed from your own resumes: the dated jobs and projects in them, and the technologies they show.

The source of truth is a folder you own (``paths.source_dir``, ``me/`` by default, never committed):

    me/resumes/      your resume versions (.md / .html in the resume-template layout, or plain Markdown / text)
    me/experience/   notes about your work in your own words (same readers)
    me/derived/      written by this module, never by hand: experience.yaml with every number and where it came from

Nothing here uses a model. Years come from the dates written next to each job; a framework implies its language (Laravel ->
PHP, Vue / React -> JavaScript); a technology the resumes list but no dated job names counts as your career length
(``source.experience.undated_skills``). The result is a *layer under facts.yaml*: a value you state yourself always wins, and
every derived value carries a provenance note, so the answers recorded for each application say where they came from.
"""

from __future__ import annotations

import hashlib
import html
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from stage2_filter.relevance import skill_in

DEFAULT_SOURCE_CFG: dict[str, Any] = {
    "sync_from": None,                    # the folder `python source.py sync` copies your resumes from; set it in config.local.yaml
    "experience": {
        "count": "jobs",                 # whose dates make up "years of experience": jobs | jobs_and_projects
        "undated_skills": "career",      # a listed technology no dated job names: career (= your job years) | off
    },
}
RESUME_SUFFIXES = (".md", ".markdown", ".html", ".htm", ".txt")
UNREAD_SUFFIXES = (".pdf", ".docx", ".doc")            # not read: the PDFs are built from the Markdown / HTML versions

_DASH = "-" + chr(0x2013) + chr(0x2014)
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_POINT = rf"(?:{_MONTH}\s+)?\d{{4}}"
_RANGE = re.compile(rf"(?P<a>{_POINT})\s*(?:[{_DASH}]|\bto\b)\s*(?P<b>{_POINT}|present|current|now|today)\b", re.IGNORECASE)
_MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}

# What a framework or library says about the language underneath: using Vue is using JavaScript. Documented stack, not a guess.
# Only applied to technologies a resume itself shows (lists or puts in bold), so a plain word like "express" proves nothing.
IMPLIES: dict[str, tuple[str, ...]] = {
    "Vue.js": ("JavaScript",), "React.js": ("JavaScript",), "Electron.js": ("JavaScript",), "Node.js": ("JavaScript",),
    "Next.js": ("React.js", "JavaScript"), "Nuxt.js": ("Vue.js", "JavaScript"), "Express": ("Node.js", "JavaScript"),
    "Nest.js": ("Node.js", "TypeScript", "JavaScript"), "Angular": ("TypeScript", "JavaScript"), "Svelte": ("JavaScript",),
    "TypeScript": ("JavaScript",), "Jest": ("JavaScript",), "React Testing Library": ("React.js", "JavaScript"), "Three.js": ("JavaScript",),
    "IndexedDB": ("JavaScript",),
    "Laravel": ("PHP",), "Blade": ("Laravel", "PHP"), "PHPUnit": ("PHP",), "Symfony": ("PHP",), "WordPress": ("PHP",),
    "Django": ("Python",), "Flask": ("Python",), "FastAPI": ("Python",), "Pandas": ("Python",), "NumPy": ("Python",),
    "Spring": ("Java",), "Spring Boot": ("Spring", "Java"), "Rails": ("Ruby",), "ASP.NET": ("C#",),
}
CANONICAL = {
    "reactjs": "React.js", "react": "React.js", "vuejs": "Vue.js", "vue": "Vue.js", "nestjs": "Nest.js", "nodejs": "Node.js", "node": "Node.js",
    "postgres": "PostgreSQL", "postgresql": "PostgreSQL", "electronjs": "Electron.js", "electron": "Electron.js", "threejs": "Three.js",
    "nextjs": "Next.js", "nuxtjs": "Nuxt.js", "js": "JavaScript", "javascript": "JavaScript", "ts": "TypeScript", "typescript": "TypeScript",
    "mongo": "MongoDB", "mongodb": "MongoDB", "k8s": "Kubernetes",
}
_PROTECTED = ("C/C++", "CI/CD", "TCP/IP", "UI/UX", "I/O", "A/B")
_PROFICIENCY = r"native|fluent|professional|conversational|basic|intermediate|advanced|beginner|bilingual|proficient|elementary"
_SPOKEN_ITEM = re.compile(rf"[A-Za-z]+\s*\((?:{_PROFICIENCY})[^)]*\)", re.IGNORECASE)
_HUMAN_LANGUAGES = {"english", "spanish", "french", "german", "italian", "portuguese", "japanese", "chinese", "mandarin", "cantonese", "korean",
                    "russian", "arabic", "hindi", "dutch", "swedish", "polish", "turkish"}
_SECTION_WORDS = {
    "jobs": ("experience", "employment", "work history", "career"),
    "projects": ("project",),
    "skills": ("skill", "technolog", "tech stack"),
    "summary": ("summary", "profile", "about", "objective"),
}


# --------------------------------------------------------------------------- dates

def month_index(text: str, *, end: bool, today: date) -> int | None:
    """A month as year*12+month-1. 'Mar 2024' -> that month; '2021' -> January (start) or December (end); 'Present' -> this month."""
    t = text.strip().lower()
    if t in ("present", "current", "now", "today"):
        return today.year * 12 + today.month - 1
    m = re.match(rf"(?:({_MONTH})\s+)?(\d{{4}})$", t)
    if not m:
        return None
    month = _MONTHS.get(m.group(1)[:3]) if m.group(1) else (12 if end else 1)
    return int(m.group(2)) * 12 + (month or 1) - 1


def parse_range(text: str, today: date) -> tuple[int, int] | None:
    """(first month, month after the last) of the first date range in ``text``, or None."""
    m = _RANGE.search(text)
    if not m:
        return None
    a, b = month_index(m.group("a"), end=False, today=today), month_index(m.group("b"), end=True, today=today)
    if a is None or b is None or b < a:
        return None
    return a, b + 1


def union_months(ranges: list[tuple[int, int]]) -> int:
    total, cur_s, cur_e = 0, None, None
    for s, e in sorted(ranges):
        if cur_e is None or s > cur_e:
            total += (cur_e - cur_s) if cur_e is not None else 0
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    return total + ((cur_e - cur_s) if cur_e is not None else 0)


def years(ranges: list[tuple[int, int]]) -> float:
    return round(union_months(ranges) / 12, 1)


def span(rng: tuple[int, int]) -> str:
    def f(i: int) -> str:
        return f"{i // 12}-{i % 12 + 1:02d}"
    return f"{f(rng[0])}..{f(rng[1] - 1)}"


# --------------------------------------------------------------------------- reading a resume

@dataclass
class Entry:
    kind: str                       # job | project
    title: str
    company: str
    rng: tuple[int, int]
    text: str
    bullets: list[str] = field(default_factory=list)
    bold: list[str] = field(default_factory=list)       # technologies the entry puts in bold (template resumes)
    source: str = ""

    @property
    def key(self) -> str:
        return f"{self.kind}:{_norm(self.title if self.kind == 'project' else (self.company or self.title))}"


@dataclass
class Resume:
    name: str
    entries: list[Entry] = field(default_factory=list)
    listed: list[str] = field(default_factory=list)      # technologies in the skills section
    summary: str = ""


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


_BLOCK_TAG = re.compile(r"</?(?:div|li|ul|ol|p|br|h[1-6]|tr|td|table|section)\b[^>]*>", re.IGNORECASE)


def plain(fragment: str) -> str:
    """Text of an HTML fragment: block tags separate words, inline tags (<b>, <a>) do not, so '<b>Django</b>,' stays 'Django,'."""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", _BLOCK_TAG.sub(" ", fragment)))).strip()


def canonical(term: str) -> str:
    return CANONICAL.get(re.sub(r"[^a-z0-9+#]", "", term.lower()), term.strip())


def split_terms(fragment: str) -> list[str]:
    """'Jest/React Testing Library, JavaScript (ES6+) and Docker.' -> ['Jest', 'React Testing Library', 'JavaScript', 'Docker']"""
    t = html.unescape(re.sub(r"<[^>]+>", " ", fragment))
    t = _SPOKEN_ITEM.sub(" , ", t)                                   # "English (Professional)" is a language, not a technology
    t = re.sub(r"\([^)]*\)", " ", t)                                 # "Arch Linux (System & Kernel Tuning)" -> "Arch Linux"
    for i, p in enumerate(_PROTECTED):
        t = t.replace(p, f"\x00{i}\x00")
    out: list[str] = []
    for item in re.split(r"[,;]|\sand\s|\s&\s|(?<!\s)/(?!\s)", t):
        item = re.sub(r"\x00(\d)\x00", lambda m: _PROTECTED[int(m.group(1))], item)
        item = re.sub(r"^\s*(?:and|or|etc\.?)\s+", "", item.strip()).strip(" .:-*_`")
        if (item and len(item) <= 40 and re.search(r"[A-Za-z]", item) and "%" not in item and item.lower() not in _HUMAN_LANGUAGES
                and not re.match(r"^\d[\d.,]*\s*(?:x\b|times\b|\s)", item)):                    # "2x faster", "3 times": a metric, not a technology (but "3D", "2FA" are)
            term = canonical(item)
            if term not in out:
                out.append(term)
    return out


def _section_kind(heading: str) -> str:
    h = heading.lower()
    for name, words in _SECTION_WORDS.items():
        if any(w in h for w in words):
            return name
    return "other"


_ITEM = re.compile(
    r'class="(?P<cls>carrer-item|career-item|project-item)">\s*<div>(?P<head>.*?)</div>\s*<div class="date-item">(?P<date>.*?)</div>\s*</div>', re.S)


def _bold_technologies(fragment: str) -> list[str]:
    """Bold terms inside bullets, except the opening verb of each bullet ('Spearheaded ...') and metrics ('by 40%')."""
    out: list[str] = []
    for li in re.findall(r"<li>(.*?)</li>", fragment, re.S):
        bolds = re.findall(r"<b>(.*?)</b>", li, re.S)
        lead = li.lstrip().startswith("<b>")
        for i, b in enumerate(bolds):
            if i == 0 and lead:
                continue
            out.extend(split_terms(b))
    return out


def parse_template(text: str, name: str, today: date) -> Resume:
    """The resume-template layout: <div class="carrer-item"> / <div class="project-item"> blocks with a date-item div."""
    resume = Resume(name)
    matches = list(_ITEM.finditer(text))
    for i, m in enumerate(matches):
        rng = parse_range(plain(m.group("date")), today)
        if rng is None:
            continue
        head = m.group("head")
        first_bold = re.search(r"<b>(.*?)</b>", head, re.S)
        title = plain(first_bold.group(1)) if first_bold else plain(head)[:60]
        if m.group("cls") == "project-item":
            kind, company, body = "project", "", head
            techs = [t for b in re.findall(r"<b>(.*?)</b>", head, re.S)[1:] for t in split_terms(b)]
        else:
            stop = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            nxt = re.search(r"<h2", text[m.end():stop])
            body = text[m.end(): m.end() + nxt.start()] if nxt else text[m.end():stop]
            kind, company = "job", plain(head)[len(title):].lstrip(" ,").split(",")[0].strip()
            techs = _bold_technologies(body)
        resume.entries.append(Entry(kind, title, company, rng, (plain(head) + " " + plain(body)).strip(),
                                    bullets=[plain(b) for b in re.findall(r"<li>(.*?)</li>", body, re.S)], bold=techs, source=name))
    for section in re.split(r"(?=<h2)", text):
        head = re.match(r"<h2[^>]*>(.*?)</h2>", section, re.S)
        if head and _section_kind(plain(head.group(1))) == "skills":
            for li in re.findall(r"<li>(.*?)</li>", section, re.S):
                resume.listed.extend(t for t in split_terms(li.split(":", 1)[1] if ":" in li else li) if t not in resume.listed)
    for p in re.findall(r"<p[^>]*>(.*?)</p>", text.split("<h2", 1)[0], re.S):
        flat = plain(p)
        if len(flat) > 120 and "@" not in flat:
            resume.summary = flat
            break
    return resume


def parse_markdown(text: str, name: str, today: date) -> Resume:
    """Plain Markdown / text: '## Experience', then an entry per line that carries a date range, its bullets below it."""
    resume = Resume(name)
    section = "other"
    entry: Entry | None = None
    para: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        heading = re.match(r"^#{1,6}\s+(.*)", stripped)
        rng = parse_range(stripped, today)
        if heading and not rng:
            section, entry, para = _section_kind(heading.group(1)), None, []
            continue
        if section in ("jobs", "projects") and rng and (heading or not stripped.startswith(("-", "*")) or stripped.startswith("**")):
            head = _RANGE.sub("", re.sub(r"^#{1,6}\s+|[*_`]", "", stripped)).strip(" -|:,()" + _DASH)
            parts = [p.strip() for p in re.split(rf"\s+[{_DASH}|@]\s+|,\s+|\s+at\s+", head) if p.strip()]
            entry = Entry("job" if section == "jobs" else "project", parts[0] if parts else head, parts[1] if len(parts) > 1 else "", rng, head, source=name)
            resume.entries.append(entry)
            continue
        if entry is not None and stripped:
            entry.text += " " + stripped
            if stripped.startswith(("-", "*")):
                entry.bullets.append(re.sub(r"^[-*]\s*|[*_`]", "", stripped).strip())
        elif section == "skills" and stripped:
            body = stripped.split(":", 1)[1] if ":" in stripped else stripped
            resume.listed.extend(t for t in split_terms(re.sub(r"^[-*]\s*|\*\*", "", body)) if t not in resume.listed)
        elif section == "summary" and stripped:
            para.append(stripped)
            resume.summary = " ".join(para)
    return resume


def parse_resume(path: Path, today: date) -> Resume:
    text = path.read_text(encoding="utf-8", errors="replace")
    if "carrer-item" in text or "career-item" in text or "project-item" in text:
        return parse_template(text, path.name, today)
    return parse_markdown(text, path.name, today)


# --------------------------------------------------------------------------- deriving the facts

def source_dir(cfg: dict[str, Any]) -> Path | None:
    """The folder named by ``paths.source_dir`` in config.yaml, or None: with nothing configured no resume is ever read."""
    value = (cfg.get("paths") or {}).get("source_dir")
    return Path(value) if value else None


def source_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    given = cfg.get("source") or {}
    exp = {**DEFAULT_SOURCE_CFG["experience"], **(given.get("experience") or {})}
    return {**DEFAULT_SOURCE_CFG, **given, "experience": exp}


def source_files(cfg: dict[str, Any]) -> list[Path]:
    root = source_dir(cfg)
    files: list[Path] = []
    if root is None:
        return files
    for sub in ("resumes", "experience"):
        d = root / sub
        if d.is_dir():
            files.extend(sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() in RESUME_SUFFIXES and not p.name.startswith(".")))
    return files


def fingerprint(files: list[Path], policy: dict[str, Any], today: date) -> str:
    h = hashlib.sha256()
    for p in files:
        h.update(p.parent.name.encode() + b"/" + p.name.encode() + b"\0" + hashlib.sha256(p.read_bytes()).digest())
    h.update(repr(sorted(policy.items())).encode())
    h.update(today.strftime("%Y-%m").encode())                   # "Present" moves forward: re-derive monthly
    return h.hexdigest()[:20]


def merge_versions(entries: list[Entry]) -> tuple[list[Entry], list[str]]:
    """One set of entries per job / project across all resume versions. Versions that disagree on the dates: the shorter is used."""
    grouped: dict[str, list[Entry]] = {}
    for e in entries:
        grouped.setdefault(e.key, []).append(e)
    merged: list[Entry] = []
    conflicts: list[str] = []
    for group in grouped.values():
        by_file: dict[str, list[tuple[int, int]]] = {}
        for e in group:
            by_file.setdefault(e.source, []).append(e.rng)
        versions = {tuple(sorted(r)) for r in by_file.values()}
        chosen = min(versions, key=lambda v: union_months(list(v)))
        if len(versions) > 1:
            conflicts.append(f"{group[0].company or group[0].title}: " + " vs ".join(
                sorted(f"{', '.join(span(r) for r in v)} ({', '.join(sorted(f for f, rs in by_file.items() if tuple(sorted(rs)) == v))})" for v in versions)))
        first = max(group, key=lambda e: len(e.text))
        text = " ".join(dict.fromkeys(e.text for e in group))
        bullets = list(dict.fromkeys(b for e in group for b in e.bullets))
        bold = list(dict.fromkeys(t for e in group for t in e.bold))
        sources = ", ".join(sorted({e.source for e in group}))
        merged.extend(Entry(first.kind, first.title, first.company, rng, text, bullets, bold, sources) for rng in chosen)
    return merged, conflicts


def _implied(skill: str) -> list[str]:
    seen: list[str] = []
    queue = list(IMPLIES.get(skill, ()))
    while queue:
        s = queue.pop(0)
        if s not in seen:
            seen.append(s)
            queue.extend(IMPLIES.get(s, ()))
    return seen


def spoken_languages(files: list[Path]) -> tuple[dict[str, str], dict[str, str]]:
    """The languages you say you speak, as ``english_level: Professional`` (first resume that states it wins), and where each was read.

    Only the written form "English (Professional)" counts, and only for a human language: a line like "Go (Intermediate)" is a skill.
    """
    levels: dict[str, str] = {}
    where: dict[str, str] = {}
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for found in _SPOKEN_ITEM.finditer(plain(text)):
            m = re.match(r"([A-Za-z]+)\s*\(([^)]*)\)", found.group(0))
            if m and m.group(1).casefold() in _HUMAN_LANGUAGES and f"{m.group(1).casefold()}_level" not in levels:
                key = f"{m.group(1).casefold()}_level"
                levels[key] = m.group(2).strip().capitalize()
                where[f"languages.{key}"] = f"written as '{found.group(0)}' in {path.parent.name}/{path.name}"
    return levels, where


def derive(cfg: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """Everything the resumes in the source folder say, as a dict ready to be written to experience.yaml. {} when there are none."""
    today = today or date.today()
    files = source_files(cfg)
    if not files:
        return {}
    policy = source_cfg(cfg)["experience"]
    resumes: list[Resume] = []
    unread: list[str] = []
    for p in files:
        try:
            resumes.append(parse_resume(p, today))
        except Exception as e:                                    # a broken file must not hide the others
            unread.append(f"{p.name}: {type(e).__name__}")
    entries, conflicts = merge_versions([e for r in resumes for e in r.entries])
    jobs = [e for e in entries if e.kind == "job"]
    projects = [e for e in entries if e.kind == "project"]
    counted = jobs + (projects if policy["count"] == "jobs_and_projects" else [])
    others = [e for e in entries if e not in counted]
    career = years([e.rng for e in counted])

    listed = list(dict.fromkeys(t for r in resumes for t in r.listed))
    vocab = list(dict.fromkeys([*listed, *(t for e in entries for t in e.bold)]))        # only technologies the resumes themselves show
    found: dict[str, dict[str, Any]] = {}
    for e in entries:
        direct = [t for t in vocab if skill_in(e.text, t)]
        for t in direct:
            found.setdefault(t, {"direct": [], "implied": [], "via": {}})["direct"].append(e)
        for t in direct:
            for s in _implied(t):
                if s not in direct:
                    slot = found.setdefault(s, {"direct": [], "implied": [], "via": {}})
                    slot["implied"].append(e)
                    slot["via"].setdefault(id(e), set()).add(t)

    by_skill: dict[str, float] = {}
    provenance: dict[str, str] = {}
    for skill, slot in found.items():
        used = list({id(e): e for e in slot["direct"] + slot["implied"]}.values())
        in_counted = [e for e in used if e in counted]
        in_others = [e for e in used if e in others]
        path = f"experience.years_by_skill.{skill}"
        if in_counted:
            by_skill[skill] = years([e.rng for e in in_counted])
            names = ", ".join(dict.fromkeys(e.company or e.title for e in in_counted))
            via = sorted({t for e in in_counted for t in slot["via"].get(id(e), ())})
            provenance[path] = (f"named in dated work: {names}" if any(e in slot["direct"] for e in in_counted)
                                else f"implied by {', '.join(via)} in dated work: {names}")
        elif in_others:
            own = years([e.rng for e in in_others])
            by_skill[skill] = min(own, career) if career else own
            provenance[path] = f"dated projects only ({', '.join(dict.fromkeys(e.title for e in in_others))}), capped at your {career} job years"
    if policy["undated_skills"] == "career" and career:
        for skill in listed:
            if skill not in by_skill:
                by_skill[skill] = career
                provenance[f"experience.years_by_skill.{skill}"] = (
                    f"listed in your resumes' skills but no dated job names it: counted as your {career}-year career (source.experience.undated_skills)")
    provenance["experience.total_years"] = (
        f"dates of {len(counted)} {'job' if policy['count'] == 'jobs' else 'job/project'}(s): "
        + ", ".join(f"{e.company or e.title} {span(e.rng)}" for e in sorted(counted, key=lambda e: e.rng)))

    languages, language_notes = spoken_languages(files)
    provenance.update(language_notes)
    summary = next((r.summary for r in resumes if r.summary), "")
    highlights = list(dict.fromkeys(b for e in sorted(jobs, key=lambda e: -e.rng[1]) for b in e.bullets))[:8]
    narrative: dict[str, Any] = {}
    if summary:
        narrative["summary"] = summary
        provenance["narrative.summary"] = "the summary paragraph of your resume"
    if highlights:
        narrative["highlights"] = highlights
        provenance["narrative.highlights"] = "bullets of your dated jobs, newest first"
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fingerprint": fingerprint(files, policy, today),
        "policy": dict(policy),
        "sources": [f"{p.parent.name}/{p.name}" for p in files],
        "unread": unread,
        "conflicts": conflicts,
        "experience": {"total_years": career, "years_by_skill": dict(sorted(by_skill.items(), key=lambda kv: (-kv[1], kv[0])))},
        "narrative": narrative,
        "languages": languages,
        "provenance": provenance,
        "jobs": [{"title": e.title, "company": e.company, "dates": span(e.rng), "years": years([e.rng])} for e in sorted(jobs, key=lambda e: e.rng)],
        "projects": [{"title": e.title, "dates": span(e.rng)} for e in sorted(projects, key=lambda e: e.rng)],
    }


# --------------------------------------------------------------------------- the file on disk

def derived_path(cfg: dict[str, Any]) -> Path:
    return (source_dir(cfg) or Path("me")) / "derived" / "experience.yaml"


def read_derived(cfg: dict[str, Any]) -> dict[str, Any]:
    import yaml

    try:
        data = yaml.safe_load(derived_path(cfg).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def write_derived(cfg: dict[str, Any], data: dict[str, Any]) -> Path:
    import yaml

    p = derived_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    header = ("# Written by resume_facts.py from the files in ../resumes and ../experience. Do not edit: change a resume, or state the value in\n"
              "# facts.yaml (what you write there always wins over this file).\n")
    fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=120))
    os.replace(tmp, p)
    return p


def ensure_derived(cfg: dict[str, Any], today: date | None = None, force: bool = False) -> dict[str, Any]:
    """The derived facts, re-derived (and written) when a source file, the policy or the month changed. {} when there are no resumes."""
    today = today or date.today()
    files = source_files(cfg)
    if not files:
        return {}
    current = read_derived(cfg)
    if not force and current.get("fingerprint") == fingerprint(files, source_cfg(cfg)["experience"], today):
        return current
    data = derive(cfg, today)
    if data:
        write_derived(cfg, data)
    return data
