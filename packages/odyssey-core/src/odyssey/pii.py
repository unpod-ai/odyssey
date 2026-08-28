"""Content-level PII detection/redaction (item 2.15).

``spool._is_secret`` (and ``redact_event``) mask by *key name* —
``api_key``, ``refresh_token``, and friends — and deliberately never touch
``message.content``: that is the training data, and blanket-redacting prose
would quietly destroy the corpus. This module is the other half: regex
matching against *content*, for callers who explicitly opt into it. It is
narrow and honest about being narrow — pattern matching over well-known PII
shapes (email/phone/credit-card/SSN), not named-entity recognition. A
phrase like "my colleague Sarah" carries no formal marker these regexes can
catch; that gap is not silently pretended away.

Two entry points, one per use case:

- :func:`scan_pii` — a dry-run report (:class:`~odyssey.primitives.
  RedactionPreview`): what would be flagged and where, without changing
  anything. What ``check_pii_redaction`` (data_preparation's validation
  stage) uses.
- :func:`redact_pii` — actually replace matches with a masked placeholder.
  What ``scrub_pii_content`` (data_preparation's cleaning stage) uses, and
  only when a caller opts in with a real :class:`~odyssey.primitives.
  PiiPolicy` — never applied by default, for the same reason
  ``redact_event`` never touches content.

Stdlib ``re`` only — no new dependency, consistent with ``odyssey-core``'s
``dependencies = []``.
"""

from __future__ import annotations

import re
from typing import Dict, List, Sequence

from odyssey.primitives import PiiRule, RedactionPreview

__all__ = ["scan_pii", "redact_pii"]

# How much surrounding text a sample keeps, each side of a match. Enough to
# be useful for a human reviewing a preview, small enough that the preview
# itself never becomes a second copy of the sensitive content.
_CONTEXT_CHARS = 12

# Deliberately conservative: international/extension-heavy phone shapes and
# non-US SSN-equivalents are real gaps, same "narrow but honest" trade-off
# the module docstring names. A pattern that over-matches (flags code, IDs,
# or version strings as PII) is worse than one that under-matches, since a
# false positive corrupts training data a caller trusted this to leave alone.
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?\d{1,2}[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}(?!\d)"
)
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
# 13-19 digits, optionally grouped by spaces or dashes -- Luhn does the real
# filtering (a bare 13-19 digit run alone is nowhere near specific enough).
_CREDIT_CARD_RE = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")

_PATTERNS: Dict[str, "re.Pattern[str]"] = {
    "EMAIL": _EMAIL_RE,
    "PHONE": _PHONE_RE,
    "CREDIT_CARD": _CREDIT_CARD_RE,
    "SSN": _SSN_RE,
}


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _matches(text: str, rule: PiiRule) -> List[re.Match]:
    pattern = _PATTERNS.get(rule)
    if pattern is None:
        return []
    found = list(pattern.finditer(text))
    if rule == "CREDIT_CARD":
        found = [m for m in found if _luhn_ok(re.sub(r"[ -]", "", m.group()))]
    return found


def scan_pii(text: str, rules: Sequence[PiiRule]) -> RedactionPreview:
    """Count and sample PII-shaped matches without changing ``text``.

    Samples carry masked context, never the raw match — the preview itself
    must not become a place PII leaks to. A caller wanting the real matches
    to act on them should call :func:`redact_pii` instead of parsing samples.
    """
    counts: Dict[str, int] = {}
    samples: List[Dict[str, object]] = []
    for rule in rules:
        found = _matches(text, rule)
        if not found:
            continue
        counts[rule] = len(found)
        for m in found[:3]:
            start = max(0, m.start() - _CONTEXT_CHARS)
            end = min(len(text), m.end() + _CONTEXT_CHARS)
            masked = text[start : m.start()] + f"[{rule}]" + text[m.end() : end]
            samples.append({"rule": rule, "context": masked})
    return RedactionPreview(total_rule_counts=counts, samples=samples)


def redact_pii(text: str, rules: Sequence[PiiRule]) -> str:
    """Replace every PII-shaped match with ``[REDACTED_<RULE>]``.

    Rules are applied in the given order; each rule's replacement happens
    over the *already-redacted* text from prior rules, so overlapping
    matches (a credit-card-shaped run of digits inside a longer phone-like
    string, say) cannot double-replace the same span inconsistently.
    """
    for rule in rules:
        pattern = _PATTERNS.get(rule)
        if pattern is None:
            continue
        if rule == "CREDIT_CARD":

            def _sub(m: "re.Match[str]") -> str:
                return (
                    f"[REDACTED_{rule}]"
                    if _luhn_ok(re.sub(r"[ -]", "", m.group()))
                    else m.group()
                )

            text = pattern.sub(_sub, text)
        else:
            text = pattern.sub(f"[REDACTED_{rule}]", text)
    return text
