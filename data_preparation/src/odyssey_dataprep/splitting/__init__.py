"""Splitting — item 3.7: by session/group key, never by row.

Two journeys that share a `trace_id` (linked calls in one session) are one
unit for leakage purposes — put them in different splits and the model
sees half the session during training and is evaluated on the other half,
which is not evaluation, it is memorisation with extra steps. Assignment is
therefore keyed on the group, not the individual journey, and is
deterministic (a hash of the key, not `random`) so the same corpus splits
the same way on every run without persisting a decision anywhere.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from odyssey.hashing import content_hash

__all__ = ["group_key", "assign_split", "split_dir"]

DEFAULT_RATIOS: Dict[str, float] = {"train": 0.8, "val": 0.1, "test": 0.1}


def group_key(journey: Dict[str, Any]) -> str:
    """`trace_id` when present -- the session a journey belongs to -- else
    the journey's own id, so an ungrouped journey still splits on its own
    identity rather than colliding with every other ungrouped one."""
    task = journey.get("task") or {}
    return (
        journey.get("trace_id")
        or task.get("conversation_id")
        or task.get("id")
        or "unknown"
    )


def assign_split(key: str, ratios: Dict[str, float] = DEFAULT_RATIOS) -> str:
    """Deterministic bucket for ``key`` — a hash of the key, not `random`,
    landed against ``ratios``' cumulative range in a fixed (sorted) split
    order, so the same key always lands in the same split regardless of
    dict ordering."""
    total = sum(ratios.values())
    h = content_hash({"group_key": key})
    frac = int(h[:8], 16) / 0xFFFFFFFF

    cumulative = 0.0
    names = sorted(ratios)
    for name in names:
        cumulative += ratios[name] / total
        if frac < cumulative:
            return name
    return names[-1]


def split_dir(
    journeys_dir: Path | str,
    out_root: Path | str,
    *,
    ratios: Dict[str, float] = DEFAULT_RATIOS,
) -> Dict[str, List[Path]]:
    """Copy every journey in ``journeys_dir`` into ``{out_root}/{split}/``,
    grouped so no ``group_key`` ever appears in more than one split."""
    src = Path(journeys_dir)
    groups: Dict[str, List[Path]] = {}
    for path in sorted(src.glob("*.json")):
        journey = json.loads(path.read_text(encoding="utf-8"))
        groups.setdefault(group_key(journey), []).append(path)

    written: Dict[str, List[Path]] = {name: [] for name in ratios}
    for key, paths in sorted(groups.items()):
        split_name = assign_split(key, ratios)
        dest_dir = Path(out_root) / split_name
        dest_dir.mkdir(parents=True, exist_ok=True)
        for path in paths:
            dest = dest_dir / path.name
            dest.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            written.setdefault(split_name, []).append(dest)
    return written
