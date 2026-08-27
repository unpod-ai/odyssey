"""Corpus versioning — items 4.4/4.5: `version = sha(recipe_hash + curated_watermark)`.

`recipe_hash` (`recipes/__init__.py`) answers "processed which way";
`curated_watermark` answers "built from which data." Both are defined by
`openspec/changes/add-journey-schema/design.md` Decision 9 — this module is
that decision's implementation, not a new design.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from odyssey.hashing import content_hash

__all__ = ["compute_curated_watermark", "corpus_version"]


def compute_curated_watermark(curated_dir: Path | str, *, seq: int) -> Dict[str, Any]:
    """The `{seq, hash}` pair for one curation run over a directory of
    normalized `*.json` journeys (`normalization`'s own output shape).

    `hash` is `content_hash` over the sorted `(journey_id, journey_content_hash)`
    set — sorted so the result is order-independent, per-journey (not just
    per-id) so a re-annotated or corrected journey changes the watermark even
    when the curated *set* of ids is unchanged. `journey_id` is
    `task.conversation_id` or `task.id`, the same fallback
    `normalization._save_journeys` already uses to name the file.

    `seq` is the human-facing half and is not derived here — it is the
    caller's job to track "which curation run is this," the same
    "seed from disk, never reissue" discipline `context.SeqAllocator`
    already applies to per-journey `seq`, at a different scope.
    """
    pairs: List[List[str]] = []
    for path in sorted(Path(curated_dir).glob("*.json")):
        journey = json.loads(path.read_text(encoding="utf-8"))
        task = journey.get("task") or {}
        journey_id = task.get("conversation_id") or task.get("id")
        if not journey_id:
            raise ValueError(f"{path}: journey has no task.conversation_id or task.id")

        telemetry = journey.get("telemetry") or {}
        journey_content_hash = (telemetry.get("data") or {}).get("content_hash")
        if not journey_content_hash:
            # No stamped hash to trust (e.g. hand-authored fixture) — fall
            # back to hashing the journey as found, same primitive either way.
            journey_content_hash = content_hash(journey)

        pairs.append([str(journey_id), str(journey_content_hash)])

    pairs.sort()
    return {"seq": seq, "hash": content_hash(pairs)}


def corpus_version(recipe_hash: str, curated_watermark: Dict[str, Any]) -> str:
    """`sha(recipe_hash + curated_watermark)`, per `design.md` Decision 9 —
    `sha` there is this project's own `content_hash`, not concatenation."""
    return content_hash({"recipe": recipe_hash, "watermark": curated_watermark})
