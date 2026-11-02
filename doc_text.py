"""Text out of a resume file, whatever it is saved as: Markdown, HTML, plain text, Word (.docx) or PDF.

The rest of the project reads resumes as Markdown-like text (headings, one line per dated job, "- " bullets). Markdown, HTML and text files already are
that. A Word file is unpacked with the standard library (it is a zip of XML). A PDF needs ``pypdf`` (``python -m pip install pypdf``); without it the PDF
is reported as unread, never guessed at.

Both formats lose the structure the Markdown versions have, so two things are restored in plain, predictable ways and nothing else is invented:

* a line that is only a section name ("Experience", "WORK HISTORY", "Skills:") becomes a ``## `` heading, as does anything Word styles as a heading;
* a leading bullet glyph (a dot, a square, an arrow ...) becomes ``- ``.

A file that yields almost no text (a scan, a picture of a resume, a password-protected PDF) raises :class:`UnreadableDocument` with the reason in plain
words, and the callers report it instead of carrying on with nothing.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

PLAIN_SUFFIXES = (".md", ".markdown", ".html", ".htm", ".txt")
DOCUMENT_SUFFIXES = (".docx", ".pdf")
MAX_DOCX_BYTES = 20_000_000          # unpacked size of word/document.xml: a resume is a few hundred KB, anything this large is not one
MAX_PDF_PAGES = 12
MIN_CHARS = 120                      # less than this is not a resume with a text layer
MIN_LETTER_SHARE = 0.55              # of the non-space characters: fonts without a text map come out as symbols and "(cid:12)" debris

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_BULLET = re.compile("^[\\u2022\\u25cf\\u25aa\\u25e6\\u25a0\\u25b6\\u2023\\u2043\\u2219\\u00b7\\uf0b7\\uf0a7\\uf076\\u27a2\\u2713]\\s*")
_NUMBERED = re.compile(r"^\d{1,2}[.)]\s+(?=[A-Z])")
SECTION_NAMES = frozenset(
    "experience|work experience|professional experience|relevant experience|employment|employment history|work history|career history|career|professional background"
    "|projects|personal projects|selected projects|side projects|key projects|notable projects"
    "|skills|technical skills|key skills|core skills|core competencies|technologies|tech stack|skills & technologies|skills and technologies|technical proficiencies"
    "|summary|professional summary|profile|professional profile|about|about me|objective|career objective"
    "|education|academic background|education & training|education and training|academic"
    "|languages|certifications|awards|interests|publications|volunteering|references|courses".split("|"))


class UnreadableDocument(Exception):
    """The file cannot be turned into text. ``str(e)`` says why, in words a person can act on (it does not repeat the file name)."""


def is_document(path: Path) -> bool:
    return path.suffix.lower() in DOCUMENT_SUFFIXES


def read(path: Path) -> str:
    """The text of a resume file. Raises UnreadableDocument for a Word or PDF file that cannot be read."""
    suffix = path.suffix.lower()
    if suffix == ".docx":
        text = _docx_text(path)
    elif suffix == ".pdf":
        text = _pdf_text(path)
    else:
        return path.read_text(encoding="utf-8", errors="replace")
    _check(text, path)
    return text


def twin_exists(path: Path) -> bool:
    """A PDF or Word file next to a Markdown / HTML / text file of the same name is that file's printout (``resume-backend.pdf`` built from
    ``resume-backend.md``): reading both would count every job twice, so only the text version is read."""
    return any(path.with_suffix(s).is_file() for s in PLAIN_SUFFIXES)


# --------------------------------------------------------------------------- shared

def _is_section_name(line: str) -> bool:
    """Only a line that is nothing but a section's name: "Senior Project Manager" names a job, not the projects section."""
    text = re.sub(r"\s+", " ", re.sub(r"[^a-z& ]", " ", line.lower())).strip()
    return text in SECTION_NAMES


def _tidy(lines: list[str]) -> str:
    out: list[str] = []
    for raw in lines:
        line = re.sub(r"[ \t ]+", " ", raw).strip()
        if not line:
            if out and out[-1] != "":
                out.append("")
            continue
        if line.startswith("#"):
            out.append(line)
            continue
        if _is_section_name(line):
            out.append("## " + line.rstrip(":").strip().title() if line.isupper() else "## " + line.rstrip(":").strip())
            continue
        bullet = _BULLET.sub("", line, count=1)
        if bullet != line:
            line = "- " + bullet
        out.append(line)
    return "\n".join(out).strip() + "\n"


def _check(text: str, path: Path) -> None:
    visible = re.sub(r"\s", "", text)
    if len(visible) < MIN_CHARS:
        raise UnreadableDocument("almost no text in it (a scan or a picture of a resume?); keep a Markdown or HTML version for the bot to read")
    letters = sum(c.isalpha() for c in visible)
    if letters / len(visible) < MIN_LETTER_SHARE:
        raise UnreadableDocument("comes out as symbols rather than words (its fonts have no text map); keep a Markdown or HTML version for the bot to read")


# --------------------------------------------------------------------------- Word

def _docx_text(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as z:
            info = z.getinfo("word/document.xml")
            if info.file_size > MAX_DOCX_BYTES:
                raise UnreadableDocument("too large to be a resume")
            root = ElementTree.fromstring(z.read(info))
    except UnreadableDocument:
        raise
    except KeyError:
        raise UnreadableDocument("not a Word (.docx) document") from None
    except (zipfile.BadZipFile, ElementTree.ParseError, OSError):
        raise UnreadableDocument("cannot be opened as a Word (.docx) document (an old .doc, or damaged?)") from None
    body = root.find(f"{_W}body")
    if body is None:
        raise UnreadableDocument("no text in it")
    lines: list[str] = []
    for block in body:
        if block.tag == f"{_W}p":
            lines.append(_paragraph(block))
        elif block.tag == f"{_W}tbl":
            for row in block.iter(f"{_W}tr"):
                cells = [" ".join(p for p in (_paragraph(par, plain=True) for par in cell.iter(f"{_W}p")) if p) for cell in row.findall(f"{_W}tc")]
                lines.append(" | ".join(c for c in cells if c))
    return _tidy(lines)


def _paragraph(p: ElementTree.Element, plain: bool = False) -> str:
    parts: list[str] = []
    for node in p.iter():
        if node.tag == f"{_W}t":
            parts.append(node.text or "")
        elif node.tag in (f"{_W}tab", f"{_W}br"):
            parts.append(" ")
    text = "".join(parts).strip()
    if plain or not text:
        return text
    style = p.find(f"{_W}pPr/{_W}pStyle")
    name = (style.get(f"{_W}val") if style is not None else "") or ""
    if re.match(r"(?i)^(heading|title)", name):
        return "## " + text
    if p.find(f"{_W}pPr/{_W}numPr") is not None or re.match(r"(?i)^listparagraph", name):
        return "- " + _BULLET.sub("", text, count=1)
    return text


# --------------------------------------------------------------------------- PDF

def _pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise UnreadableDocument("a PDF, and pypdf is not installed (python -m pip install pypdf); a Markdown or HTML version is read without it") from None
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            raise UnreadableDocument("password-protected")
        pages = [page.extract_text() or "" for page in reader.pages[:MAX_PDF_PAGES]]
    except UnreadableDocument:
        raise
    except Exception as e:                                           # pypdf raises many kinds of error on a damaged file
        raise UnreadableDocument(f"cannot be read as a PDF ({type(e).__name__})") from None
    return _tidy("\n".join(pages).splitlines())
