"""Find and fill the questions on an application form.

``EXTRACT_JS`` runs inside the page and returns every visible question (text boxes, selects, radio groups,
checkboxes, comboboxes) with its label, options, required flag and current value, and tags each control with
``data-aa-id`` so it can be filled reliably afterwards. The scope is the top-most open dialog (LinkedIn Easy
Apply, Workday pop-ups) or else the first form.

Everything else here is plain Python: label cleanup, stable question keys, and matching an intended answer to
one of the options a control actually offers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from job_text import collapse_repeated_phrase

JS_PRELUDE = r"""
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const isVisible = (el) => {
    if (!el) return false;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return false;
    return el.getClientRects().length > 0;
  };
  const textOf = (el) => clean(el ? (el.innerText || el.textContent || '') : '');
  // A label's own words, without the controls nested inside it (a <select> would drag in all its options).
  const labelText = (l) => {
    if (!l) return '';
    const c = l.cloneNode(true);
    c.querySelectorAll('input, select, textarea, button, script, style').forEach((x) => x.remove());
    // Separate sibling elements with a space, or two spans become one glued word ("Question?Question?").
    c.querySelectorAll('*').forEach((x) => x.append(' '));
    return clean(c.textContent);
  };
  const byIds = (ids) => (ids || '').split(/\s+/).filter(Boolean)
    .map((i) => labelText(document.getElementById(i))).filter(Boolean).join(' ');
  const PLACEHOLDER = /^(select|choose|please|pick|--|—|-\s*$)/i;
  // Every element matching `selector`, including inside open shadow roots (web components hide their inputs there).
  const deepAll = (root, selector) => {
    const out = Array.from(root.querySelectorAll(selector));
    root.querySelectorAll('*').forEach((e) => { if (e.shadowRoot) out.push(...deepAll(e.shadowRoot, selector)); });
    return out;
  };
  const groupLabelOf = (el) => {
    const fs = el.closest('fieldset');
    if (fs) { const lg = fs.querySelector('legend'); if (lg) return labelText(lg); }
    const grp = el.closest('[role="radiogroup"], [role="group"]');
    if (grp) { const t = byIds(grp.getAttribute('aria-labelledby')) || clean(grp.getAttribute('aria-label')); if (t) return t; }
    let p = el.parentElement;
    for (let i = 0; p && i < 4; i++, p = p.parentElement) {
      const cand = p.querySelector(':scope > label, :scope > legend, :scope > .label, :scope > h3, :scope > h4, :scope > p');
      if (cand && !cand.contains(el) && labelText(cand)) return labelText(cand);
    }
    return '';
  };
  const labelFor = (el) => {
    let t = byIds(el.getAttribute('aria-labelledby'));
    if (!t && el.labels && el.labels.length) t = Array.from(el.labels).map(labelText).filter(Boolean).join(' ');
    if (!t) t = clean(el.getAttribute('aria-label'));
    if (!t) t = groupLabelOf(el);
    if (!t) t = clean(el.getAttribute('placeholder')) || clean(el.getAttribute('name'));
    return t;
  };
  const isRequired = (el, label) =>
    !!el.required || el.getAttribute('aria-required') === 'true' || /\*/.test(label) || /\(required\)/i.test(label);
"""

_EXTRACT_BODY = r"""
  const base = (opts && opts.base) || 1;
  deepAll(document, '[data-aa-id]').forEach((e) => {
    e.removeAttribute('data-aa-id');
    e.removeAttribute('data-aa-opt');
  });

  // Scope: the top-most open dialog; else the visible <form> with the most controls (the first form on a page is
  // often a site-search box); else the whole page.
  const dialogs = deepAll(document, '[role="dialog"], [aria-modal="true"], dialog[open]').filter(isVisible);
  const formScore = (f) => deepAll(f, 'input:not([type="hidden"]), select, textarea').filter(isVisible).length;
  const forms = Array.from(document.querySelectorAll('form')).filter(isVisible).sort((a, b) => formScore(b) - formScore(a));
  const root = dialogs.length ? dialogs[dialogs.length - 1] : (forms.length && formScore(forms[0]) > 0 ? forms[0] : document.body);

  const out = [];
  const groups = new Map();
  let nextId = base;

  for (const el of deepAll(root, 'input, select, textarea')) {
    const tag = el.tagName.toLowerCase();
    const type = tag === 'input' ? (el.getAttribute('type') || 'text').toLowerCase() : tag;
    if (['hidden', 'submit', 'button', 'image', 'reset', 'file', 'search', 'range', 'color', 'password'].includes(type)) continue;
    if (el.disabled) continue;
    const ownLabel = el.labels && el.labels[0];
    if (!(isVisible(el) || isVisible(ownLabel) || isVisible(el.closest('label')))) continue;

    if (type === 'radio' || type === 'checkbox') {
      const key = type + ':' + (el.name || ('anon' + out.length));
      let q = groups.get(key);
      if (!q) {
        q = { id: nextId++, kind: type, input_type: type, label: '', options: [], required: false, value: '', maxlength: null, name: el.name || '', members: [] };
        groups.set(key, q);
        out.push(q);
      }
      const optLabel = labelFor(el) || el.value;
      el.setAttribute('data-aa-id', String(q.id));
      el.setAttribute('data-aa-opt', String(q.members.length));
      q.members.push({ el, text: clean((ownLabel ? labelText(ownLabel) : '') || labelText(el.closest('label')) || el.getAttribute('aria-label') || el.value) });
      q.options.push(q.members[q.members.length - 1].text || optLabel);
      if (isRequired(el, optLabel)) q.required = true;
      continue;
    }

    const label = labelFor(el);
    const q = { id: nextId++, kind: 'text', input_type: type, label, options: [], required: isRequired(el, label), value: '',
                maxlength: el.maxLength > 0 ? el.maxLength : null, name: el.name || '' };
    el.setAttribute('data-aa-id', String(q.id));
    if (tag === 'select') {
      q.kind = 'select';
      const opts2 = Array.from(el.options).map((o) => ({ text: clean(o.textContent), value: o.value }));
      q.options = opts2.filter((o) => o.value !== '' && !PLACEHOLDER.test(o.text)).map((o) => o.text);
      const chosen = el.selectedOptions && el.selectedOptions[0];
      q.value = chosen && chosen.value !== '' && !PLACEHOLDER.test(clean(chosen.textContent)) ? clean(chosen.textContent) : '';
    } else if (tag === 'textarea') {
      q.kind = 'textarea';
      q.value = el.value;
    } else {
      q.value = el.value;
      if (el.getAttribute('role') === 'combobox' || el.getAttribute('aria-autocomplete')) q.kind = 'combobox';
    }
    out.push(q);
  }

  for (const q of out) {
    if (q.members) {
      const checked = q.members.filter((m) => m.el.checked).map((m) => m.text);
      q.value = checked.join(', ');
      if (q.kind === 'checkbox' && q.members.length > 1) {
        q.kind = 'checkbox_group';
        q.label = groupLabelOf(q.members[0].el);
      } else if (q.kind === 'checkbox') {
        q.label = labelFor(q.members[0].el);
        q.options = [];
      } else {
        q.label = groupLabelOf(q.members[0].el) || labelFor(q.members[0].el);
      }
      q.required = q.required || /\*/.test(q.label);
      delete q.members;
    }
  }
  return out;
"""

EXTRACT_JS = "(opts) => {" + JS_PRELUDE + _EXTRACT_BODY + "}"


def clean_label(raw: str) -> str:
    text = re.sub(r"\s+", " ", raw or "").strip()
    text = re.sub(r"\(required\)", "", text, flags=re.IGNORECASE)
    text = text.replace("*", " ")
    text = re.sub(r"\s+", " ", text).strip(" :")
    return collapse_repeated_phrase(text)           # LinkedIn renders each label twice (visible + screen-reader copy)


def normalize_key(text: str) -> str:
    """Stable identity of a question across companies: lower-case words only."""
    t = re.sub(r"[^a-z0-9 ]+", " ", clean_label(text).casefold())
    t = re.sub(r"^\d+\s+", "", t)                    # '3. Are you ...' -> 'are you ...'
    return re.sub(r"\s+", " ", t).strip()


@dataclass
class Question:
    id: int
    kind: str                                       # text | textarea | select | radio | checkbox | checkbox_group | combobox
    label: str
    options: list[str] = field(default_factory=list)
    required: bool = False
    value: str = ""
    input_type: str = "text"
    maxlength: int | None = None
    name: str = ""
    frame: int = 0                                  # index into page.frames (forms are often embedded in an iframe)

    @property
    def key(self) -> str:
        return normalize_key(self.label)

    @property
    def has_options(self) -> bool:
        return self.kind in ("select", "radio", "checkbox_group") and bool(self.options)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "label": self.label, "options": self.options,
            "required": self.required, "input_type": self.input_type,
        }


def parse_questions(raw: Iterable[dict[str, Any]] | None) -> list[Question]:
    questions: list[Question] = []
    for item in raw or []:
        label = clean_label(str(item.get("label", "")))
        options = [clean_label(str(o)) for o in item.get("options", []) if str(o).strip()]
        questions.append(
            Question(
                id=int(item["id"]),
                kind=str(item.get("kind", "text")),
                label=label,
                options=options,
                required=bool(item.get("required")),
                value=str(item.get("value") or "").strip(),
                input_type=str(item.get("input_type") or "text"),
                maxlength=item.get("maxlength"),
                name=str(item.get("name") or ""),
                frame=int(item.get("frame") or 0),
            )
        )
    return questions


# --------------------------------------------------------------------------- choosing an option

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", str(text).casefold())).strip()


_DECLINE = re.compile(r"decline|prefer not|do not wish|don t wish|dont wish|not to (?:say|answer|disclose|identify)|rather not")
_DECLINE_WORDS = {"decline", "prefer not to say", "prefer not to answer", "decline to self identify", "i do not wish to answer"}


def _range_of(option: str) -> tuple[float, float] | None:
    t = _norm(option)
    m = re.search(r"(\d+)\s*(?:to|-)\s*(\d+)", option.replace("–", "-")) or re.search(r"(\d+)\s+to\s+(\d+)", t)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = re.search(r"(\d+)\s*\+|(?:more than|over|at least)\s+(\d+)", option.replace("–", "-").casefold())
    if m:
        return float(m.group(1) or m.group(2)), float("inf")
    m = re.search(r"(?:less than|under|fewer than)\s+(\d+)", option.casefold())
    if m:
        return 0.0, float(m.group(1)) - 0.001
    m = re.fullmatch(r"\s*(\d+)\s*(?:years?|yrs?)?\s*", option.casefold())
    if m:
        return float(m.group(1)), float(m.group(1))
    return None


def match_option(options: list[str], desired: Any) -> int | None:
    """Index of the option that means ``desired``, or None when nothing matches unambiguously."""
    if not options or desired is None or str(desired).strip() == "":
        return None
    if isinstance(desired, bool):
        desired = "yes" if desired else "no"
    d = _norm(desired)
    norm = [_norm(o) for o in options]

    for i, o in enumerate(norm):
        if o == d:
            return i

    if d in ("yes", "true", "y"):
        hits = [i for i, o in enumerate(norm) if re.match(r"yes\b", o)]
        return hits[0] if len(hits) == 1 else None
    if d in ("no", "false", "n"):
        hits = [i for i, o in enumerate(norm) if re.match(r"no\b(?! preference)", o)]
        return hits[0] if len(hits) == 1 else None
    if d in _DECLINE_WORDS or _DECLINE.search(d):
        hits = [i for i, o in enumerate(norm) if _DECLINE.search(o)]
        return hits[0] if hits else None

    try:                                            # "5" against "3-5 years", "5+ years", "Less than 2 years"
        number = float(str(desired).strip())
        in_range = [(i, r) for i, o in enumerate(options) if (r := _range_of(o)) and r[0] <= number <= r[1]]
        if in_range:                                # "3-5" and "5+" both contain 5: the narrower range is the answer
            return min(in_range, key=lambda t: t[1][1] - t[1][0])[0]
    except ValueError:
        pass

    hits = [i for i, o in enumerate(norm) if len(o) >= 3 and (d in o or o in d)]
    if len(hits) == 1:
        return hits[0]
    if hits:                                        # "Other" vs "Other (please specify)": prefer the closest
        return min(hits, key=lambda i: abs(len(norm[i]) - len(d)))
    return None
