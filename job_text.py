"""Text helpers shared by the scrapers and stage 2: HTML -> text, title cleanup, ages and salaries."""

from __future__ import annotations

import html as html_lib
import re
from datetime import datetime, timedelta, timezone

_SCRIPT_STYLE = re.compile(r"<(script|style)\b.*?</\1>", re.I | re.S)
_BLOCK_TAG = re.compile(r"</?(?:p|br|li|ul|ol|div|h[1-6]|tr|section|article|header|footer)\b[^>]*>", re.I)
_ANY_TAG = re.compile(r"<[^>]+>")
_HAS_MARKUP = re.compile(r"<[a-zA-Z/!][^>]*>|&(?:lt|gt|amp|nbsp|#\d+|#x[0-9a-fA-F]+|[a-zA-Z]+);")
_META_SEPARATOR = re.compile(r"\s+[•·∙]\s+|\s+(?=\$\d)")


def looks_like_markup(text: str) -> bool:
    return bool(text) and bool(_HAS_MARKUP.search(text))


def html_to_text(raw: str | None) -> str:
    """HTML to readable plain text, one block per line.

    Also copes with entity-escaped HTML (``&lt;p&gt;...``), which is how Greenhouse delivers job content.
    """
    if not raw:
        return ""
    text = raw
    if "<" not in text and "&lt;" in text:
        text = html_lib.unescape(text)
    text = _SCRIPT_STYLE.sub(" ", text)
    text = _BLOCK_TAG.sub("\n", text)
    text = _ANY_TAG.sub(" ", text)
    text = html_lib.unescape(text)
    lines = (re.sub(r"[ \t ]+", " ", ln).strip() for ln in text.splitlines())
    return "\n".join(ln for ln in lines if ln)


def text_lines(fragment: str) -> list[str]:
    """Visible text nodes of an HTML fragment, in order, one per line (tags are node boundaries)."""
    text = _SCRIPT_STYLE.sub(" ", fragment)
    text = _ANY_TAG.sub("\n", text)
    text = html_lib.unescape(text)
    return [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines() if ln.strip()]


def dedupe_consecutive(lines: list[str]) -> list[str]:
    """LinkedIn renders every title twice (visible + screen-reader copy); drop immediate repeats."""
    out: list[str] = []
    for ln in lines:
        if out and out[-1].casefold() == ln.casefold():
            continue
        out.append(ln)
    return out


def collapse_repeated_phrase(text: str) -> str:
    """'Data Engineer Data Engineer' -> 'Data Engineer'."""
    n = len(text)
    if n > 2 and n % 2 == 1 and text[n // 2] == " ":
        left, right = text[: n // 2], text[n // 2 + 1:]
        if left.casefold() == right.casefold():
            return left
    return text


def clean_title(raw: str | None, max_len: int = 140) -> str:
    """Unescape, collapse whitespace, drop trailing card metadata ('... • Remote', '... $25/hr') and cap the length."""
    text = re.sub(r"\s+", " ", html_lib.unescape(raw or "")).strip()
    text = _META_SEPARATOR.split(text, maxsplit=1)[0].strip()
    text = collapse_repeated_phrase(text)
    return text[:max_len].rstrip()


_AGE = re.compile(
    r"(?<![\w.])(\d+)\s*\+?\s*(minute|min|hour|hr|h|day|d|week|wk|w|month|mo|year|yr|y)s?\.?\s+ago\b",
    re.I,
)
_AGE_UNITS = {
    "minute": timedelta(minutes=1), "min": timedelta(minutes=1),
    "hour": timedelta(hours=1), "hr": timedelta(hours=1), "h": timedelta(hours=1),
    "day": timedelta(days=1), "d": timedelta(days=1),
    "week": timedelta(weeks=1), "wk": timedelta(weeks=1), "w": timedelta(weeks=1),
    "month": timedelta(days=30), "mo": timedelta(days=30),
    "year": timedelta(days=365), "yr": timedelta(days=365), "y": timedelta(days=365),
}


def parse_posted_age(text: str | None, now: datetime | None = None) -> str | None:
    """'Posted 4 months ago' / '3wk ago' / '12 hours ago' -> ISO timestamp, or None."""
    m = _AGE.search(text or "")
    if not m:
        return None
    now = now or datetime.now(timezone.utc)
    return (now - int(m.group(1)) * _AGE_UNITS[m.group(2).lower()]).isoformat()


_AMOUNT = r"\$\s*(?P<{n}>\d[\d,]*(?:\.\d+)?)\s*(?P<{n}k>[kK])?"
_UNIT = r"(?:\s*(?:/|per\s+)\s*(?P<{u}>hr|hour|mo|month|yr|year|annum|h|y)\b)?"
_SALARY = re.compile(
    _AMOUNT.format(n="a") + _UNIT.format(u="ua")
    + r"(?:\s*(?:-|–|—|to)\s*\$?\s*(?P<b>\d[\d,]*(?:\.\d+)?)\s*(?P<bk>[kK])?" + _UNIT.format(u="ub") + r")?",
    re.I,
)
_ANNUALISE = {"hr": 2080, "hour": 2080, "h": 2080, "mo": 12, "month": 12, "yr": 1, "year": 1, "y": 1, "annum": 1}


def parse_salary(text: str | None) -> tuple[int, int] | None:
    """First USD amount or range in ``text`` as annual (min, max), e.g. '$25/hr - $30/hr' -> (52000, 62400).

    Bare numbers without a unit are only trusted when they are clearly annual (>= 10,000 once 'k' is applied).
    """
    for m in _SALARY.finditer(text or ""):
        def num(val: str | None, k: str | None) -> float | None:
            if val is None:
                return None
            n = float(val.replace(",", ""))
            return n * 1000 if k else n

        low = num(m.group("a"), m.group("ak"))
        high = num(m.group("b"), m.group("bk")) if m.group("b") else low
        if low is None or high is None:
            continue
        unit = (m.group("ub") or m.group("ua") or "").lower()
        if unit:
            factor = _ANNUALISE[unit]
        elif low >= 10_000:
            factor = 1
        else:
            continue  # "$20" with no unit: could be anything
        lo, hi = sorted((low * factor, high * factor))
        return int(round(lo)), int(round(hi))
    return None
