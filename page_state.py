"""What the application agent sees: a compact, frame-aware snapshot of the current page.

The browser-side script gathers the visible text, every clickable thing, file inputs and visible error messages
from the page and all its iframes (and open shadow roots). Python then decides what each button *is*
(``classify_action``) and what kind of page this is (``classify_page``). Both are deterministic: the model only
ever chooses between actions this module has already judged safe, so a page can not talk it into anything else.
"""

from __future__ import annotations

import hashlib
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from form_questions import JS_PRELUDE, Question, parse_questions

_SNAPSHOT_BODY = r"""
  const base = (opts && opts.base) || 1;
  deepAll(document, '[data-aa-act], [data-aa-file]').forEach((e) => {
    e.removeAttribute('data-aa-act');
    e.removeAttribute('data-aa-file');
  });

  const dialogs = deepAll(document, '[role="dialog"], [aria-modal="true"], dialog[open]').filter(isVisible);
  const scope = dialogs.length ? dialogs[dialogs.length - 1]
    : (document.querySelector('main, [role="main"]') || document.body);
  const text = ((scope && scope.innerText) || '').replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n').trim().slice(0, 6000);

  const inChrome = (el) => !!el.closest('nav, header, footer, [role="navigation"], [role="banner"], [role="contentinfo"]');
  const actions = [];
  let n = 0;
  for (const el of deepAll(document, 'button, a[href], [role="button"], [role="link"], input[type="submit"], input[type="button"]')) {
    if (!isVisible(el) || el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
    if (dialogs.length && !scope.contains(el)) continue;          // a dialog is open: the page behind it is not actionable
    const label = clean(el.getAttribute('aria-label') || el.innerText || el.value || el.getAttribute('title') || '').slice(0, 90);
    if (!label) continue;
    const id = base + n++;
    el.setAttribute('data-aa-act', String(id));
    actions.push({
      id, label, tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || '', type: (el.getAttribute('type') || '').toLowerCase(),
      href: el.tagName === 'A' ? el.href : '', target: el.getAttribute('target') || '',
      in_form: !!el.closest('form, [role="dialog"]'), chrome: inChrome(el) && !el.closest('form, [role="dialog"], dialog'),
    });
    if (actions.length >= 60) break;
  }

  const files = deepAll(document, 'input[type="file"]').map((el, i) => {
    const id = base + i;
    el.setAttribute('data-aa-file', String(id));
    const label = labelFor(el) || '';
    return { id, label, name: el.name || '', accept: el.getAttribute('accept') || '', multiple: !!el.multiple,
             required: isRequired(el, label), has_file: !!(el.files && el.files.length) };
  });

  const errors = [];
  const errorNodes = deepAll(document, '[role="alert"], [aria-live="assertive"], .error, .errors, .error-message, [class*="error-message" i], ' +
    '[class*="field-error" i], [class*="invalid-feedback" i], .field_with_errors, .artdeco-inline-feedback--error, [aria-invalid="true"]');
  for (const e of errorNodes) {
    if (!isVisible(e)) continue;
    const t = clean(e.getAttribute('aria-invalid') === 'true' ? ('Invalid: ' + (labelFor(e) || 'a field')) : textOf(e)).slice(0, 140);
    if (t.length >= 3 && !errors.includes(t)) errors.push(t);
    if (errors.length >= 6) break;
  }

  return {
    url: location.href, title: document.title, text, actions, files, errors,
    password: deepAll(document, 'input[type="password"]').some(isVisible),
    captcha: deepAll(document, 'iframe').some((f) => /recaptcha|hcaptcha|turnstile|captcha/i.test(f.src || '') && isVisible(f)),
  };
"""

SNAPSHOT_JS = "(opts) => {" + JS_PRELUDE + _SNAPSHOT_BODY + "}"


@dataclass
class Action:
    id: int
    label: str
    tag: str = "button"
    role: str = ""
    type: str = ""
    href: str = ""
    target: str = ""
    in_form: bool = False
    chrome: bool = False
    frame: int = 0
    category: str = "other"


@dataclass
class FileField:
    id: int
    label: str = ""
    name: str = ""
    accept: str = ""
    multiple: bool = False
    required: bool = False
    has_file: bool = False
    frame: int = 0

    @property
    def kind(self) -> str:
        words = f"{self.label} {self.name}".lower()
        if re.search(r"cover|letter", words):
            return "cover_letter"
        if re.search(r"resume|résumé|\bcv\b|curricul", words):
            return "resume"
        return "other"


@dataclass
class PageSnapshot:
    url: str = ""
    title: str = ""
    text: str = ""
    actions: list[Action] = field(default_factory=list)
    files: list[FileField] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    password: bool = False
    captcha: bool = False
    page_type: str = "unknown"

    def signature(self) -> str:
        """Stable identity of what is on screen, so a loop (the same page again) can be recognised."""
        path = urllib.parse.urlparse(self.url).path
        parts = [path, self.title, re.sub(r"\s+", " ", self.text[:300]).lower(),
                 "|".join(sorted(a.label.lower() for a in self.actions)), "|".join(sorted(q.label.lower() for q in self.questions))]
        return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:16]

    def required_gaps(self) -> list[str]:
        """Required things that are still empty: questions without a value and required file inputs without a file."""
        gaps = [q.label or "(unlabelled field)" for q in self.questions if q.required and not q.value]
        gaps += [f"upload: {f.label or f.name or 'file'}" for f in self.files if f.required and not f.has_file]
        return gaps


# --------------------------------------------------------------------------- what is a button?

_NEVER = re.compile(
    r"\b(delete|remove|withdraw|discard|sign ?out|log ?out|deactivate|unsubscribe|create (an? )?(account|profile)|register|sign ?up|"
    r"join( now| us| our)?|download|print|share|refer(ral)?|job alerts?|create alert|subscribe|follow|report|flag|feedback|contact us|"
    r"help center|faq|privacy( policy)?|terms|cookie (policy|settings)|manage (cookies|preferences)|save (this )?job|save for later|"
    r"save (as )?draft|continue later|talent (network|community)|apply with (linkedin|indeed|google|facebook|seek|xing|apple)|"
    r"sign in( with)?|continue with (google|linkedin|facebook|apple|email)|log ?in( with)?|talk to|chat( with)?|watch|learn more|"
    r"view (all )?jobs|see (all )?jobs|search jobs|careers home)\b|^help$",
    re.IGNORECASE,
)
_COOKIE_REJECT = re.compile(
    r"\b(reject( all)?|decline( all)?|deny( all)?|only (necessary|essential|required)|(necessary|essential|required) (cookies )?only|refuse( all)?)\b",
    re.IGNORECASE,
)
_REVIEW_AND_SUBMIT = re.compile(r"review (and|&|\+) submit", re.IGNORECASE)
_SUBMIT = re.compile(r"\b(submit|send (my |your )?application|finish application|complete (my |your )?application|confirm (and|&) submit)\b", re.IGNORECASE)
_UPLOAD = re.compile(r"\b(upload|attach|choose (a )?file|browse|select (a )?file|add (a |your )?(resume|cv|file))\b", re.IGNORECASE)
_NEXT = re.compile(
    r"^(next( step)?|continue|save (and|&) continue|proceed|go to next( step)?|continue (to|with) [\w ]+|next:? [\w ]+|forward)\W*$",
    re.IGNORECASE,
)
_REVIEW = re.compile(r"\breview\b", re.IGNORECASE)
_APPLY = re.compile(
    r"\b(apply( now| today| here| online)?|apply (for|to) (this|the) (job|position|role|opening)|start (your |an? |my )?application|"
    r"begin (your )?application|i'?m interested|express (your )?interest|easy apply)\b",
    re.IGNORECASE,
)
_BACK = re.compile(r"^(back|previous|go back|cancel|close|dismiss|no|exit|skip)\W*$", re.IGNORECASE)

SAFE_CATEGORIES = ("apply", "next", "review", "submit", "other")


def classify_action(a: Action, page_has_form: bool, cookie_banner: bool = False) -> str:
    """Decide what clicking this would do. Anything destructive, off-purpose or account-related is ``never``."""
    label = a.label.strip()
    href = (a.href or "").strip().lower()
    if href.startswith(("mailto:", "tel:", "javascript:", "blob:", "data:")) or re.search(r"\.(pdf|docx?|zip)(\?|$)", href):
        return "never"
    if cookie_banner and not a.in_form:
        if _COOKIE_REJECT.search(label):
            return "cookie_reject"
        if re.search(r"\b(accept|allow|agree|got it|ok|okay)\b", label, re.IGNORECASE):
            return "cookie_accept"
    if _NEVER.search(label):
        return "never"
    if _UPLOAD.search(label) and not _SUBMIT.search(label):
        return "upload"
    if _REVIEW_AND_SUBMIT.search(label):
        return "review"
    if _SUBMIT.search(label):
        return "submit"
    if _NEXT.match(label):
        return "next"
    if _REVIEW.search(label):
        return "review"
    if _APPLY.search(label):
        # "Apply" on a page that already shows the form, inside that form, is the submit button.
        return "submit" if (page_has_form and a.in_form) else "apply"
    if _BACK.match(label):
        return "back"
    return "other"


# --------------------------------------------------------------------------- what kind of page is this?

_CONFIRMED = re.compile(
    r"thank(s| you) for (applying|your (application|interest|submission))|(your )?application (has been |was )?(successfully )?(received|submitted|sent)|"
    r"we('ve| have) received your application|successfully (applied|submitted)|application complete",
    re.IGNORECASE,
)
_CLOSED = re.compile(
    r"no longer (accepting|available|open)|(position|job|role|posting|vacancy|opening) (has been |is |was )?(filled|closed|expired|removed)|"
    r"this (job|position|role|posting) (is )?(closed|expired|unavailable)|applications? (are |is )?(now )?closed",
    re.IGNORECASE,
)
_ALREADY = re.compile(r"(you('ve| have) )?already (applied|submitted)|duplicate application", re.IGNORECASE)
_LOGIN_TEXT = re.compile(r"(sign|log) in to (continue|apply|your account)|create an account to apply|please (sign|log) in|sign in or create", re.IGNORECASE)


def classify_page(snap: PageSnapshot) -> str:
    """confirmed | already_applied | closed | captcha | login | form | job_page | unknown  (first match wins)."""
    text = snap.text
    if snap.captcha:
        return "captcha"
    has_form = bool(snap.questions or snap.files)
    if _CONFIRMED.search(text) and not has_form and not any(a.category == "submit" for a in snap.actions):
        return "confirmed"
    if _ALREADY.search(text) and not has_form:
        return "already_applied"
    if _CLOSED.search(text) or re.search(r"\b404\b|not found", snap.title, re.IGNORECASE):
        return "closed"
    if snap.password or (_LOGIN_TEXT.search(text) and len(snap.questions) <= 2):
        return "login"
    if has_form:
        return "form"
    if any(a.category == "apply" for a in snap.actions):
        return "job_page"
    return "unknown"


def build_snapshot(raw_frames: list[dict[str, Any]], questions: list[Question]) -> PageSnapshot:
    """Merge the per-frame results (main frame first) into one classified snapshot."""
    snap = PageSnapshot(questions=questions)
    texts: list[str] = []
    for fr in raw_frames:
        index = int(fr.get("frame", 0))
        if index == 0:
            snap.url, snap.title = str(fr.get("url", "")), str(fr.get("title", ""))
        texts.append(str(fr.get("text", "")))
        snap.password = snap.password or bool(fr.get("password"))
        snap.captcha = snap.captcha or bool(fr.get("captcha"))
        for e in fr.get("errors", []):
            if e not in snap.errors:
                snap.errors.append(e)
        snap.actions += [Action(frame=index, **{k: v for k, v in a.items() if k in Action.__dataclass_fields__}) for a in fr.get("actions", [])]
        snap.files += [FileField(frame=index, **{k: v for k, v in f.items() if k in FileField.__dataclass_fields__}) for f in fr.get("files", [])]
    snap.text = "\n".join(t for t in texts if t.strip())[:8000]

    has_form = bool(snap.questions or snap.files)
    banner = bool(re.search(r"cookie|consent", snap.text, re.IGNORECASE))
    for a in snap.actions:
        a.category = classify_action(a, has_form, banner)
    snap.page_type = classify_page(snap)
    return snap


def take_snapshot(session: Any) -> PageSnapshot:
    """Snapshot the session's page and all its frames. A session that cannot do this yields an empty, unknown page."""
    raw = getattr(session, "snapshot_raw", None)
    frames = raw() if callable(raw) else []
    extract = getattr(session, "extract_questions", None)
    try:
        questions = parse_questions(extract()) if callable(extract) else []
    except Exception:
        questions = []
    return build_snapshot(frames, questions)
