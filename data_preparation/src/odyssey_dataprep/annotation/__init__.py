"""Annotation — item 3.4: human-in-loop queue adapters over normalized
journeys. `Signal`/`Reward`/`build_reward_from_scalar` already exist in
odyssey-core and are populated by the SDK; what was missing is a way to
hand a batch of journeys to a human reviewer and apply the decisions that
come back. No external queue system — a local JSONL file is the queue,
consistent with this project's "no dependency nothing imports" rule.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from odyssey.builders.reward import build_reward_from_scalar

__all__ = ["ApplyResult", "build_queue", "apply_reviews"]


def _preview(journey: Dict[str, Any]) -> str:
    """First user message and last assistant message, truncated — enough
    for a reviewer to recognise the journey without opening the file."""
    messages = (journey.get("steps") or [{}])[-1].get("messages") or []
    user = next((m.get("content") for m in messages if m.get("role") == "user"), None)
    assistant = next(
        (m.get("content") for m in reversed(messages) if m.get("role") == "assistant"),
        None,
    )
    return f"user: {(user or '')[:120]!r} -> assistant: {(assistant or '')[:120]!r}"


def build_queue(journeys_dir: Path | str, queue_path: Path | str) -> int:
    """Write one JSONL line per journey under review:
    ``{"journey_id", "content_hash", "preview"}``. Returns how many were
    queued."""
    src = Path(journeys_dir)
    entries = []
    for path in sorted(src.glob("*.json")):
        journey = json.loads(path.read_text(encoding="utf-8"))
        task = journey.get("task") or {}
        telemetry = journey.get("telemetry") or {}
        entries.append(
            {
                "journey_id": task.get("conversation_id")
                or task.get("id")
                or path.stem,
                "content_hash": (telemetry.get("data") or {}).get("content_hash"),
                "preview": _preview(journey),
            }
        )

    out = Path(queue_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    return len(entries)


@dataclass(frozen=True)
class ApplyResult:
    applied: List[Path] = field(default_factory=list)
    skipped: List[str] = field(
        default_factory=list
    )  # journey_ids with no matching file

    @property
    def count(self) -> int:
        return len(self.applied)


def _load_decisions(decisions_path: Path | str) -> Dict[str, Dict[str, Any]]:
    decisions: Dict[str, Dict[str, Any]] = {}
    with open(decisions_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            decision = json.loads(line)
            decisions[decision["journey_id"]] = decision
    return decisions


def apply_reviews(
    journeys_dir: Path | str, decisions_path: Path | str, out_dir: Path | str
) -> ApplyResult:
    """Apply a reviewer's decisions -- ``{"journey_id", "approved",
    "score": Optional[float], "notes": Optional[str]}`` per JSONL line --
    onto the matching journeys.

    A ``score`` becomes the journey's ``reward``
    (:func:`build_reward_from_scalar`, reused rather than re-derived); the
    decision itself lands under ``telemetry.data.annotation`` so a
    downstream stage (validation, splitting) can filter on
    ``approved`` without re-deriving it.
    """
    decisions = _load_decisions(decisions_path)
    src = Path(journeys_dir)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    applied: List[Path] = []
    matched_ids = set()
    for path in sorted(src.glob("*.json")):
        journey = json.loads(path.read_text(encoding="utf-8"))
        task = journey.get("task") or {}
        jid = task.get("conversation_id") or task.get("id") or path.stem
        decision = decisions.get(jid)
        if decision is None:
            continue
        matched_ids.add(jid)

        if decision.get("score") is not None:
            reward = build_reward_from_scalar(float(decision["score"]))
            journey["reward"] = dataclasses.asdict(reward)

        telemetry = dict(
            journey.get("telemetry") or {"source": "annotation", "data": {}}
        )
        data = dict(telemetry.get("data") or {})
        data["annotation"] = {
            "approved": bool(decision.get("approved")),
            "notes": decision.get("notes"),
        }
        telemetry["data"] = data
        journey["telemetry"] = telemetry

        dest = out / path.name
        tmp = dest.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(journey, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        tmp.replace(dest)
        applied.append(dest)

    skipped = sorted(set(decisions) - matched_ids)
    return ApplyResult(applied=applied, skipped=skipped)
