"""Redaction before any LLM call.

Two layers:
1. Terms the user marks (organisation names, people, sites, figures), each with a label.
2. Automatic patterns: email addresses, Singapore phone numbers, NRIC/FIN numbers.

Each distinct term becomes a stable placeholder such as [ORG_1]. The placeholder map
stays in the local database and is used to restore names after extraction.

Limits (by design, stated plainly): rule-based redaction does not catch terms the user
did not list, misspellings, or indirect identifiers. Review the redacted preview before
sending it to an external model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

AUTO_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")),
    ("NRIC", re.compile(r"\b[STFGM]\d{7}[A-Z]\b")),
    ("PHONE", re.compile(r"(?:\+65[\s-]?)?\b[689]\d{3}[\s-]?\d{4}\b")),
]

_LABEL_RE = re.compile(r"[^A-Z0-9]+")


@dataclass
class Term:
    text: str
    label: str = "TERM"


@dataclass
class RedactionResult:
    text: str
    mapping: dict[str, str]  # placeholder -> original


def _label(raw: str) -> str:
    lab = _LABEL_RE.sub("_", (raw or "TERM").upper()).strip("_")
    return lab or "TERM"


def redact(text: str, terms: Iterable[Term | dict[str, str] | str] = (), auto: bool = True) -> RedactionResult:
    norm_terms: list[Term] = []
    for t in terms:
        if isinstance(t, str):
            t = Term(t)
        elif isinstance(t, dict):
            t = Term(t.get("text", ""), t.get("label", "TERM"))
        if t.text and t.text.strip():
            norm_terms.append(Term(t.text.strip(), _label(t.label)))

    mapping: dict[str, str] = {}
    by_original: dict[str, str] = {}
    counters: dict[str, int] = {}

    def placeholder(original: str, label: str) -> str:
        key = original.lower()
        if key in by_original:
            return by_original[key]
        counters[label] = counters.get(label, 0) + 1
        ph = f"[{label}_{counters[label]}]"
        mapping[ph] = original
        by_original[key] = ph
        return ph

    out = text
    # Patterns first: a user term inside an email address must not break the email match.
    if auto:
        for label, pattern in AUTO_PATTERNS:
            out = _sub_outside_placeholders(
                out, pattern, lambda m, label=label: placeholder(m.group(0), label))
    # Longest first so "Tidewater Logistics" wins over "Tidewater".
    for t in sorted(norm_terms, key=lambda x: len(x.text), reverse=True):
        pattern = re.compile(r"(?<!\w)" + re.escape(t.text) + r"(?!\w)", re.IGNORECASE)
        out = _sub_outside_placeholders(out, pattern, lambda m, t=t: placeholder(t.text, t.label))
    return RedactionResult(out, mapping)


_PLACEHOLDER_RE = re.compile(r"\[[A-Z0-9_]+_\d+\]")


def _sub_outside_placeholders(text: str, pattern: re.Pattern[str], repl) -> str:
    """Apply a substitution only to text between existing placeholders."""
    parts = []
    last = 0
    for m in _PLACEHOLDER_RE.finditer(text):
        parts.append(pattern.sub(repl, text[last:m.start()]))
        parts.append(m.group(0))
        last = m.end()
    parts.append(pattern.sub(repl, text[last:]))
    return "".join(parts)


def unredact_text(text: str, mapping: dict[str, str]) -> str:
    if not mapping or not isinstance(text, str):
        return text
    for ph in sorted(mapping, key=len, reverse=True):
        text = text.replace(ph, mapping[ph])
    return text


def unredact(obj: Any, mapping: dict[str, str]) -> Any:
    """Restore placeholders in every string of a nested structure."""
    if isinstance(obj, str):
        return unredact_text(obj, mapping)
    if isinstance(obj, list):
        return [unredact(x, mapping) for x in obj]
    if isinstance(obj, dict):
        return {k: unredact(v, mapping) for k, v in obj.items()}
    return obj
