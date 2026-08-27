"""Cleaning — item 3.2: dedupe, dead-turn drop, encoding repair over the
normalized `*.json` artifacts `normalization` (3.3) already produces.

**Content-level PII scrub is not here.** `PiiPolicy`/`RedactionPreview` are
still types with no implementation (`docs/WORKING.md` item 2.15) — only
key-based masking exists, at the spool layer, not this one. Implementing it
here would be inventing the missing primitive under a different name;
wiring it in is one call once 2.15 lands, not this stage's job today.
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Tuple

from odyssey.hashing import content_hash

__all__ = [
    "CleanResult",
    "dedupe_journeys",
    "drop_dead_turns",
    "repair_encoding",
    "clean_dir",
]


@dataclass(frozen=True)
class CleanResult:
    written: List[Path] = field(default_factory=list)
    duplicates_dropped: int = 0
    dead_turns_dropped: int = 0
    encoding_repairs: int = 0

    @property
    def count(self) -> int:
        return len(self.written)


def _is_dead(message: Dict[str, Any]) -> bool:
    """A message with nothing worth training on — same definition as
    `odyssey.primitives.Message.is_empty()`, applied to the dict form
    `normalization` writes to disk rather than a rebuilt dataclass, since
    there is no `Journey`-from-dict loader in odyssey-core to rebuild one."""
    return not (
        message.get("content")
        or message.get("tool_calls")
        or message.get("tool_response")
    )


def drop_dead_turns(journey: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    """Drop a step whose own new turn — the cumulative delta versus the
    previous step — is entirely dead messages, splicing it out of every
    later step's cumulative history too.

    `Step.messages` is cumulative (every step holds the whole conversation
    up to its own turn), so a naive per-message filter would corrupt the
    prefix invariant later steps depend on. Reconstructing each kept step's
    message list from only the kept deltas, in order, is what keeps that
    invariant intact after a mid-stream drop.
    """
    steps = journey.get("steps") or []
    kept_messages: List[Dict[str, Any]] = []
    new_steps: List[Dict[str, Any]] = []
    prev_len = 0
    dropped = 0
    for step in steps:
        messages = step.get("messages") or []
        delta = messages[prev_len:]
        prev_len = len(messages)
        if delta and all(_is_dead(m) for m in delta):
            dropped += 1
            continue
        kept_messages.extend(delta)
        new_steps.append({**step, "messages": list(kept_messages)})

    return {**journey, "steps": new_steps}, dropped


_FORBIDDEN_CONTROL = {chr(c) for c in range(0x00, 0x20) if c not in (0x09, 0x0A, 0x0D)}


def repair_encoding(journey: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    """Unicode-normalize (NFC) and strip stray C0 control characters from
    every message's `content`/`reasoning`. Returns the repaired journey and
    how many strings actually changed."""
    changed = 0
    steps = journey.get("steps") or []
    new_steps = []
    for step in steps:
        messages = step.get("messages") or []
        new_messages = []
        for message in messages:
            new_message = dict(message)
            for key in ("content", "reasoning"):
                text = message.get(key)
                if not isinstance(text, str):
                    continue
                repaired = unicodedata.normalize(
                    "NFC", "".join(c for c in text if c not in _FORBIDDEN_CONTROL)
                )
                if repaired != text:
                    changed += 1
                new_message[key] = repaired
            new_messages.append(new_message)
        new_steps.append({**step, "messages": new_messages})
    return {**journey, "steps": new_steps}, changed


def dedupe_journeys(paths: List[Path]) -> Tuple[List[Path], List[Path]]:
    """Split ``paths`` (sorted first, so the result is deterministic) into
    ``(kept, dropped)`` by exact content — the first occurrence of a given
    ``telemetry.data.content_hash`` (or a freshly computed one, if a
    fixture carries none) wins."""
    seen: Dict[str, Path] = {}
    kept: List[Path] = []
    dropped: List[Path] = []
    for path in sorted(paths):
        journey = json.loads(path.read_text(encoding="utf-8"))
        telemetry = journey.get("telemetry") or {}
        h = (telemetry.get("data") or {}).get("content_hash") or content_hash(journey)
        if h in seen:
            dropped.append(path)
        else:
            seen[h] = path
            kept.append(path)
    return kept, dropped


def clean_dir(journeys_dir: Path | str, out_dir: Path | str) -> CleanResult:
    """Run dedupe, dead-turn drop, and encoding repair over a directory of
    normalized journeys, writing the survivors to ``out_dir``."""
    src = Path(journeys_dir)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    paths = sorted(src.glob("*.json"))
    kept, dropped = dedupe_journeys(paths)

    written: List[Path] = []
    dead_turns = 0
    repairs = 0
    for path in kept:
        journey = json.loads(path.read_text(encoding="utf-8"))
        journey, dead = drop_dead_turns(journey)
        journey, changed = repair_encoding(journey)
        dead_turns += dead
        repairs += changed

        dest = out / path.name
        tmp = dest.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(journey, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        tmp.replace(dest)
        written.append(dest)

    return CleanResult(
        written=written,
        duplicates_dropped=len(dropped),
        dead_turns_dropped=dead_turns,
        encoding_repairs=repairs,
    )
