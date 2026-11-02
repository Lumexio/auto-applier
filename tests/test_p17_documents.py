"""Resumes saved as Word (.docx) or PDF are read like the Markdown ones, with the same safeguards.

Everything uses a fictional resume (Alex Rivera, Northwind Labs, Contoso Systems). Word files are built here with the standard library; PDFs are written by
hand (one page of Helvetica text), so no tool outside the project is needed to make them.
"""

from __future__ import annotations

import contextlib
import io
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch

import doc_text
import resume_facts as rf
import source
from evidence import passages_from_file
from tests.test_p0_safety import tmp_cwd
from tests.test_p7_source import TODAY, cfg_for, derived_in, put

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

SUMMARY = ("Backend engineer with seven years of experience building billing and data platforms, comfortable across the stack and "
           "happiest when a slow query turns into a fast one.")
LINES = [
    ("", "Alex Rivera"),
    ("", "alex@example.test | (+1) 555 0100"),
    ("Heading1", "SUMMARY"),
    ("", SUMMARY),
    ("Heading1", "Experience"),
    ("", "Senior Backend Engineer - Northwind Labs   Mar 2019 - Dec 2022"),
    ("List", "Built the billing API with Django and PostgreSQL, cutting latency by 30%."),
    ("", "Software Engineer - Contoso Systems   Jan 2023 - Present"),
    ("List", "Shipped a dashboard in React.js and TypeScript on a Node.js backend."),
    ("Heading1", "Skills"),
    ("", "Core Languages: Python, JavaScript, TypeScript, Go, Java"),
    ("", "Tools: Docker, Git, PostgreSQL, Django"),
    ("Heading1", "Education"),
    ("", "Example University - B.S. Computer Science 2010 - 2014"),
]


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def make_docx(path: Path, lines=LINES, table=None) -> Path:
    paras = []
    for style, text in lines:
        ppr = ""
        if style.startswith("Heading"):
            ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>'
        elif style == "List":
            ppr = '<w:pPr><w:pStyle w:val="ListParagraph"/><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>'
        paras.append(f"<w:p>{ppr}<w:r><w:t>{_esc(text)}</w:t></w:r></w:p>")
    tbl = ""
    if table:
        rows = "".join("<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{_esc(c)}</w:t></w:r></w:p></w:tc>" for c in row) + "</w:tr>" for row in table)
        tbl = f"<w:tbl>{rows}</w:tbl>"
    xml = f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W}"><w:body>{"".join(paras)}{tbl}</w:body></w:document>'
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", xml)
    return path


def make_pdf(path: Path, lines=LINES, compress: bool = False) -> Path:
    """One page of Helvetica text, one line per line. Bullets are written as '-' (the standard PDF fonts have no portable bullet)."""
    shown = []
    for style, text in lines:
        shown.append(("- " if style == "List" else "") + text)
    body = "BT /F1 10 Tf 12 TL 40 800 Td " + " T* ".join(f"({s.replace(chr(92), chr(92) * 2).replace('(', chr(92) + '(').replace(')', chr(92) + ')')}) Tj" for s in shown) + " ET"
    stream = body.encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))
    return path


def markdown_of(lines=LINES) -> str:
    out = []
    for style, text in lines:
        out.append(f"## {text.title() if text.isupper() else text}" if style.startswith("Heading") else (f"- {text}" if style == "List" else text))
    return "\n".join(out) + "\n"


def _jobs(data: dict):
    return [(j["title"], j["company"], j["dates"], j["years"]) for j in data["jobs"]]


def _have_pypdf() -> bool:
    try:
        import pypdf  # noqa: F401
        return True
    except ImportError:
        return False


# --------------------------------------------------------------------------- the same facts from every format

def test_a_word_file_gives_the_same_jobs_and_years_as_the_markdown_it_was_made_from():
    with tmp_cwd() as a:
        put(a, "resume.md", markdown_of())
        want = derived_in(a)
    assert [j[0] for j in _jobs(want)] == ["Senior Backend Engineer", "Software Engineer"], "the Markdown baseline itself must read two jobs"
    with tmp_cwd() as b:
        (b / "me" / "resumes").mkdir(parents=True)
        make_docx(b / "me" / "resumes" / "resume.docx")
        got = derived_in(b)
    assert _jobs(got) == _jobs(want)
    assert got["experience"]["total_years"] == want["experience"]["total_years"]
    assert got["experience"]["years_by_skill"] == want["experience"]["years_by_skill"]
    assert got["experience"]["years_by_skill"].get("Django") == 3.8                       # Mar 2019 - Dec 2022 = 46 months
    assert got.get("unread") in (None, [])


def test_a_pdf_gives_the_same_jobs_and_years_as_the_markdown_it_was_made_from():
    if not _have_pypdf():
        return
    with tmp_cwd() as a:
        put(a, "resume.md", markdown_of())
        want = derived_in(a)
    with tmp_cwd() as b:
        (b / "me" / "resumes").mkdir(parents=True)
        make_pdf(b / "me" / "resumes" / "resume.pdf")
        got = derived_in(b)
    assert _jobs(got) == _jobs(want) and _jobs(got), got
    assert got["experience"]["years_by_skill"] == want["experience"]["years_by_skill"]


def test_the_facts_other_than_jobs_survive_the_trip_too():
    with tmp_cwd() as b:
        (b / "me" / "resumes").mkdir(parents=True)
        make_docx(b / "me" / "resumes" / "resume.docx", table=[["Languages", "English (Professional), Spanish (Native)"]])
        got = derived_in(b)
    assert got["education"]["highest_degree"].lower().startswith("bachelor") or "B.S." in str(got["education"]), got["education"]
    assert got["languages"].get("english_level") == "Professional", got.get("languages")


# --------------------------------------------------------------------------- a printout is not a second resume

def test_a_pdf_or_word_file_beside_its_text_version_is_not_read_a_second_time():
    with tmp_cwd() as tmp:
        put(tmp, "resume-base.md", markdown_of())
        make_docx(tmp / "me" / "resumes" / "resume-base.docx")
        (tmp / "me" / "resumes" / "resume-base.pdf").write_bytes(b"%PDF-1.4 not even a real one")
        make_docx(tmp / "me" / "resumes" / "resume-frontend.docx")                                   # no text twin: this one is a resume in its own right
        names = [p.name for p in rf.source_files(cfg_for(tmp))]
        assert names == ["resume-base.md", "resume-frontend.docx"], names
        data = derived_in(tmp)
        assert len(data["jobs"]) == 2 and data["experience"]["total_years"] == 7.3, data["jobs"]    # 46 + 42 months, each job once
        assert data.get("unread") in (None, []), "the unreadable printout beside its text version is not even tried"


def test_source_check_names_the_printouts_it_skipped_and_the_files_it_could_not_read():
    with tmp_cwd() as tmp:
        put(tmp, "resume-base.md", markdown_of())
        (tmp / "me" / "resumes" / "resume-base.pdf").write_bytes(b"%PDF-1.4")
        (tmp / "me" / "resumes" / "old.doc").write_bytes(b"\xd0\xcf\x11\xe0")
        (tmp / "me" / "resumes" / "scan.docx").write_bytes(b"not a zip at all")
        (tmp / "config.yaml").write_text(f"paths:\n  source_dir: '{(tmp / 'me').as_posix()}'\n", encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = source.main(["--config", str(tmp / "config.yaml"), "check"])
    text = out.getvalue()
    assert code == 1
    assert "resume-base.pdf is the printout of a text version beside it" in text, text
    assert "PROBLEM  could not read scan.docx: cannot be opened as a Word (.docx) document" in text, text
    assert ".doc files are not read (the old Word format)" in text, text


# --------------------------------------------------------------------------- what cannot be read is said, not guessed

def test_a_file_with_no_usable_text_is_reported_with_the_reason():
    with tmp_cwd() as tmp:
        d = tmp / "me" / "resumes"
        d.mkdir(parents=True)
        make_docx(d / "empty.docx", lines=[("", "Alex Rivera")])                                      # a picture of a resume has almost no words
        put(tmp, "resume.md", markdown_of())
        data = derived_in(tmp)
    assert any(u.startswith("empty.docx: almost no text in it") for u in data["unread"]), data["unread"]
    assert len(data["jobs"]) == 2, "one unreadable file must not hide the others"

    symbols = "".join(chr(0xE000 + i % 40) for i in range(400))
    with tmp_cwd() as tmp:
        d = tmp / "me" / "resumes"
        d.mkdir(parents=True)
        make_docx(d / "fonts.docx", lines=[("", symbols)])
        data = derived_in(tmp)
    assert any("comes out as symbols rather than words" in u for u in data["unread"]), data["unread"]


def test_a_pdf_without_pypdf_is_reported_with_the_command_that_fixes_it():
    with tmp_cwd() as tmp:
        d = tmp / "me" / "resumes"
        d.mkdir(parents=True)
        make_pdf(d / "resume.pdf")
        with patch.dict(sys.modules, {"pypdf": None}):                                               # an import of None raises ImportError
            data = derived_in(tmp)
    assert any(u.startswith("resume.pdf: a PDF, and pypdf is not installed (python -m pip install pypdf)") for u in data["unread"]), data
    assert not data.get("jobs")


def test_a_damaged_or_oversized_word_file_is_refused_not_trusted():
    with tmp_cwd() as tmp:
        bad = tmp / "bad.docx"
        bad.write_bytes(b"PK\x03\x04 truncated")
        try:
            doc_text.read(bad)
            raise AssertionError("a damaged file was read")
        except doc_text.UnreadableDocument as e:
            assert "cannot be opened as a Word (.docx) document" in str(e)
        big = make_docx(tmp / "big.docx")
        with patch.object(doc_text, "MAX_DOCX_BYTES", 100):
            try:
                doc_text.read(big)
                raise AssertionError("an oversized document was read")
            except doc_text.UnreadableDocument as e:
                assert "too large" in str(e)
        other = tmp / "other.docx"
        with zipfile.ZipFile(other, "w") as z:
            z.writestr("hello.txt", "this zip is not a Word document")
        try:
            doc_text.read(other)
            raise AssertionError("a zip that is not a Word document was read")
        except doc_text.UnreadableDocument as e:
            assert "not a Word (.docx) document" in str(e)


# --------------------------------------------------------------------------- restoring structure, and no more than that

def test_only_a_line_that_is_just_a_section_name_becomes_a_heading():
    text = doc_text._tidy([
        "WORK HISTORY", "Skills:", "Senior Project Manager", "Technology Lead", "About Us", "Education", "Profile Engineer - Acme  2020 - 2022",
        "Projects", "I led the projects for the platform team",
    ])
    lines = text.splitlines()
    assert lines[0] == "## Work History" and lines[1] == "## Skills" and "## Education" in lines and "## Projects" in lines
    for plain in ("Senior Project Manager", "Technology Lead", "About Us", "Profile Engineer - Acme 2020 - 2022", "I led the projects for the platform team"):
        assert plain in lines, (plain, lines)


def test_bullet_glyphs_become_dash_bullets_and_markdown_markers_are_left_alone():
    glyphs = [chr(c) for c in (0x2022, 0x25CF, 0x25AA, 0x25E6, 0x25A0, 0xF0B7)]
    text = doc_text._tidy([f"{g} Built the API" for g in glyphs] + ["**Bold start** of a line", "- already a dash", "Ordinary line"])
    lines = text.splitlines()
    assert lines[:6] == ["- Built the API"] * 6, lines
    assert lines[6:] == ["**Bold start** of a line", "- already a dash", "Ordinary line"]


def test_a_table_in_a_word_file_reads_row_by_row():
    with tmp_cwd() as tmp:
        path = make_docx(tmp / "t.docx", lines=[("", "x" * 150)], table=[["Core Languages", "Python, Go"], ["Tools", "Docker"]])
        text = doc_text.read(path)
    assert "Core Languages | Python, Go" in text and "Tools | Docker" in text


# --------------------------------------------------------------------------- the evidence ladder can quote them

def test_a_sentence_in_a_word_resume_can_be_quoted_as_evidence():
    with tmp_cwd() as tmp:
        path = make_docx(tmp / "resume.docx")
        found = passages_from_file(path, "resumes/resume.docx")
    texts = [p.text for p in found]
    assert any("Built the billing API with Django and PostgreSQL" in t for t in texts), texts
    contexts = {p.context for p in found}
    assert "Experience" in contexts and "Skills" in contexts, contexts
    assert not any("alex@example.test" in t for t in texts), "contact details are never evidence"


def test_the_stored_facts_are_rederived_when_a_word_file_changes():
    with tmp_cwd() as tmp:
        d = tmp / "me" / "resumes"
        d.mkdir(parents=True)
        make_docx(d / "resume.docx")
        cfg = cfg_for(tmp)
        first = rf.ensure_derived(cfg, TODAY)
        lines = [(s, t.replace("Mar 2019 - Dec 2022", "Mar 2019 - Dec 2020")) for s, t in LINES]
        make_docx(d / "resume.docx", lines=lines)
        second = rf.ensure_derived(cfg, TODAY)
    assert first["experience"]["total_years"] > second["experience"]["total_years"], (first["experience"], second["experience"])


def test_a_stored_resume_the_old_way_still_reads_the_same():
    """Markdown, HTML and text go through the same reader as before: nothing about them changed."""
    with tmp_cwd() as tmp:
        p = put(tmp, "r.md", "## Experience\nSoftware Engineer - Acme   Jan 2020 - Dec 2021\n- Built things\n")
        assert doc_text.read(p) == p.read_text(encoding="utf-8")
        assert doc_text.is_document(Path("a.PDF")) and doc_text.is_document(Path("b.docx")) and not doc_text.is_document(p)


def test_a_password_protected_pdf_is_refused_and_one_bad_file_does_not_stop_the_passages():
    if not _have_pypdf():
        return
    import pypdf
    from evidence import load_passages

    class Locked:
        is_encrypted = True
        pages: list = []

    with tmp_cwd() as tmp:
        d = tmp / "me" / "resumes"
        d.mkdir(parents=True)
        make_pdf(d / "locked.pdf")
        with patch.object(pypdf, "PdfReader", lambda *_a, **_k: Locked()):
            try:
                doc_text.read(d / "locked.pdf")
                raise AssertionError("a locked PDF was read")
            except doc_text.UnreadableDocument as e:
                assert str(e) == "password-protected"
        make_docx(d / "good.docx")
        (d / "broken.docx").write_bytes(b"not a zip")
        passages = load_passages(cfg_for(tmp))
    assert any("billing API" in p.text for p in passages), "the readable file's lines are still offered as evidence"


def test_word_styles_make_headings_and_bullets_even_when_the_words_are_not_a_known_section_name():
    filler = ("", "Backend engineer with seven years of experience building billing and data platforms, comfortable across the stack.")
    with tmp_cwd() as tmp:
        path = make_docx(tmp / "r.docx", lines=[filler, ("Heading2", "Open Source Work"), ("List", "Maintained a small parser library"), ("", "A plain line")])
        lines = doc_text.read(path).splitlines()
    assert "## Open Source Work" in lines, lines
    assert "- Maintained a small parser library" in lines, lines
    assert "A plain line" in lines and "- A plain line" not in lines
