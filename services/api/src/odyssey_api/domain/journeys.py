"""Journeys use-case (routers/journeys.py) — reads exactly what
`services/collector` writes, through the same `fold_shard` every exporter
(`odyssey sft`/`odyssey dpo`/Trajectory JSON) already uses, so a journey
looks identical here to how it looks everywhere else in the repo.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

from odyssey.export import fold_shard
from odyssey.fold import FoldResult

from odyssey_api.repositories import filesystem

__all__ = [
    "JourneyNotFoundError",
    "list_journeys",
    "list_journeys_with_status",
    "get_journey",
]


class JourneyNotFoundError(LookupError):
    pass


def list_journeys(journeys_dir: Path) -> List[Tuple[str, str]]:
    return filesystem.list_journeys(journeys_dir)


def list_journeys_with_status(journeys_dir: Path) -> List[Tuple[str, str, bool]]:
    """``[(journey_id, date, complete), ...]`` — folds every shard once
    (not the two-pass "list then re-find-then-fold" a naive router would
    do) to answer whether it's `trainable` per `fold.FoldResult.complete`.
    A shard that fails to fold (malformed on disk) is reported incomplete
    rather than aborting the whole listing.
    """
    out: List[Tuple[str, str, bool]] = []
    if not journeys_dir.is_dir():
        return out
    for date_dir in sorted(p for p in journeys_dir.iterdir() if p.is_dir()):
        for shard in sorted(date_dir.glob("*.jsonl")):
            try:
                complete = fold_shard(shard).complete
            except (OSError, ValueError):
                complete = False
            out.append((shard.stem, date_dir.name, complete))
    return out


def get_journey(journeys_dir: Path, journey_id: str) -> FoldResult:
    path = filesystem.find_journey_path(journeys_dir, journey_id)
    if path is None:
        raise JourneyNotFoundError(journey_id)
    return fold_shard(path)
