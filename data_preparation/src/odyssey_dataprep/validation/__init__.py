"""Validation — item 3.6: schema assert, leakage check, drift, PII assert.

The one hard rule from `docs/adr/0003-single-cli-entrypoint.md`: a lineage
violation exits 3, the code CI greps for — `odyssey.cli._cmd_health`
already does this for a writer conflict; `odyssey data validate` (this
stage's CLI command) does it for a validation breach.

PII assert has two independent halves now. The key-based one checks that
`odyssey.spool`'s own redaction (`DEFAULT_REDACT_KEYS`/`REDACTED`) actually
reached the normalized artifact, reusing its exact matching rule
(`_is_secret`) rather than a re-derived one. The optional `content_rules`
half (item 2.15, `odyssey.pii`) additionally regex-scans `message.content`/
`.reasoning` — detection only, same as the rest of this stage: a violation
fails validation, nothing here mutates the journey.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from odyssey.pii import scan_pii
from odyssey.primitives import PiiRule
from odyssey.spool import DEFAULT_REDACT_KEYS, REDACTED, _is_secret

__all__ = [
    "ValidationResult",
    "validate_schema",
    "check_pii_redaction",
    "check_leakage",
    "compute_stats",
    "check_drift",
    "validate_dir",
]

_VALID_ROLES = {"user", "assistant", "system", "tool"}
_VALID_TRAINABLE_STATUS = {"trainable", "not_trainable", "superseded", "excluded"}


@dataclass(frozen=True)
class ValidationResult:
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_schema(journey: Dict[str, Any]) -> List[str]:
    """Structural checks on the shape `normalization`/`export.journey_to_dict`
    actually produce — not a general JSON Schema, there is no dependency
    for one and this project's own artifacts are the only input."""
    errors: List[str] = []
    if not isinstance(journey.get("task"), dict):
        errors.append("task: missing or not an object")
    steps = journey.get("steps")
    if not isinstance(steps, list):
        errors.append("steps: missing or not a list")
        return errors

    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            errors.append(f"steps[{i}]: not an object")
            continue
        status = step.get("trainable_status", "not_trainable")
        if status not in _VALID_TRAINABLE_STATUS:
            errors.append(f"steps[{i}].trainable_status: invalid value {status!r}")
        messages = step.get("messages")
        if not isinstance(messages, list):
            errors.append(f"steps[{i}].messages: missing or not a list")
            continue
        for j, message in enumerate(messages):
            if not isinstance(message, dict) or message.get("role") not in _VALID_ROLES:
                errors.append(f"steps[{i}].messages[{j}].role: invalid or missing")
    return errors


def _walk(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def check_pii_redaction(
    journey: Dict[str, Any],
    redact_keys: frozenset = DEFAULT_REDACT_KEYS,
    *,
    content_rules: Optional[Sequence[PiiRule]] = None,
) -> List[str]:
    """Every key ``odyssey.spool``'s own redaction would have masked must
    carry the redaction marker here too — reuses its exact matching rule
    (``_is_secret``), not a re-derived one, so this check can never be
    stricter or looser than what actually ran.

    ``content_rules`` (item 2.15) additionally regex-scans ``content``/
    ``reasoning`` on every message via ``odyssey.pii.scan_pii`` — omitted
    by default, since most journeys legitimately carry prose that looks
    like an email or a phone number as training data, not a leak.
    """
    errors = []
    for mapping in _walk(journey):
        for key, value in mapping.items():
            if _is_secret(str(key), redact_keys) and value not in (
                None,
                "",
                [],
                {},
                REDACTED,
            ):
                errors.append(f"unredacted key {key!r} (value: {value!r})")

    if content_rules:
        for step in journey.get("steps") or []:
            for message in step.get("messages") or []:
                if not isinstance(message, dict):
                    continue
                for field_name in ("content", "reasoning"):
                    text = message.get(field_name)
                    if not isinstance(text, str):
                        continue
                    preview = scan_pii(text, content_rules)
                    for rule, count in sorted(preview.total_rule_counts.items()):
                        errors.append(
                            f"unredacted {rule} in message.{field_name} "
                            f"({count} match(es))"
                        )
    return errors


def check_leakage(splits: Dict[str, List[str]]) -> List[str]:
    """Any journey id present in more than one split is a leak."""
    seen: Dict[str, str] = {}
    errors = []
    for split_name, ids in splits.items():
        for jid in ids:
            if jid in seen and seen[jid] != split_name:
                errors.append(f"{jid} appears in both {seen[jid]!r} and {split_name!r}")
            else:
                seen[jid] = split_name
    return errors


def compute_stats(journeys_dir: Path | str) -> Dict[str, float]:
    """Cheap distributional fingerprint of a corpus: mean steps and mean
    messages per journey. Enough to catch a curation run that accidentally
    halved the corpus or dropped every multi-turn journey."""
    turns: List[int] = []
    messages: List[int] = []
    for path in Path(journeys_dir).glob("*.json"):
        journey = json.loads(path.read_text(encoding="utf-8"))
        steps = journey.get("steps") or []
        turns.append(len(steps))
        messages.append(len((steps[-1] if steps else {}).get("messages") or []))
    if not turns:
        return {"journeys": 0, "mean_steps": 0.0, "mean_messages": 0.0}
    return {
        "journeys": len(turns),
        "mean_steps": statistics.fmean(turns),
        "mean_messages": statistics.fmean(messages),
    }


def check_drift(
    current: Dict[str, float], baseline: Dict[str, float], *, threshold: float = 0.2
) -> List[str]:
    """Flag any shared numeric stat that moved by more than ``threshold``
    (relative) versus ``baseline``."""
    errors = []
    for key in sorted(set(current) & set(baseline)):
        base = baseline[key]
        if base == 0:
            continue
        delta = abs(current[key] - base) / abs(base)
        if delta > threshold:
            errors.append(
                f"{key}: drifted {delta:.0%} (baseline {base}, current {current[key]})"
            )
    return errors


def validate_dir(
    journeys_dir: Path | str,
    *,
    splits: Optional[Dict[str, List[str]]] = None,
    baseline_stats: Optional[Dict[str, float]] = None,
    content_pii_rules: Optional[Sequence[PiiRule]] = None,
) -> ValidationResult:
    """Run schema + PII-redaction checks over every journey in
    ``journeys_dir``, plus leakage (if ``splits`` given) and drift (if
    ``baseline_stats`` given). ``content_pii_rules`` opts into the
    content-level scan (item 2.15) alongside the always-on key-based one.
    """
    errors: List[str] = []
    for path in sorted(Path(journeys_dir).glob("*.json")):
        journey = json.loads(path.read_text(encoding="utf-8"))
        errors.extend(f"{path.name}: {e}" for e in validate_schema(journey))
        errors.extend(
            f"{path.name}: {e}"
            for e in check_pii_redaction(journey, content_rules=content_pii_rules)
        )

    if splits is not None:
        errors.extend(check_leakage(splits))
    if baseline_stats is not None:
        errors.extend(check_drift(compute_stats(journeys_dir), baseline_stats))

    return ValidationResult(errors=errors)
