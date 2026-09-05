"""Journeys use-case (routers/journeys.py) — reads exactly what
`services/collector` writes, through the same `fold_shard` every exporter
(`odyssey sft`/`odyssey dpo`/Trajectory JSON) already uses, so a journey
looks identical here to how it looks everywhere else in the repo.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

from odyssey.export import fold_shard
from odyssey.fold import FoldResult

from odyssey_api.index.manager import IndexHandle
from odyssey_api.repositories import filesystem

__all__ = [
    "JourneyNotFoundError",
    "list_journeys",
    "list_journeys_with_status",
    "list_journeys_with_status_indexed",
    "get_journey",
]


class JourneyNotFoundError(LookupError):
    pass


def list_journeys(
    journeys_dir: Path, product_slug: Optional[str] = None
) -> List[Tuple[str, str]]:
    return filesystem.list_journeys(journeys_dir, product_slug)


def list_journeys_with_status(
    journeys_dir: Path, product_slug: Optional[str] = None
) -> List[Tuple[str, str, bool]]:
    """``[(journey_id, date, complete), ...]`` — folds every shard once
    (not the two-pass "list then re-find-then-fold" a naive router would
    do) to answer whether it's `trainable` per `fold.FoldResult.complete`.
    A shard that fails to fold (malformed on disk) is reported incomplete
    rather than aborting the whole listing. Reuses `filesystem.list_journeys`
    for partition/shard discovery so there is exactly one place that walks
    `journeys_dir` and decides what counts as a journey shard, not two.
    The shard's actual path is re-resolved via `find_journey_path` rather
    than rebuilt as `journeys_dir / journey_date / f"{journey_id}.jsonl"` —
    that flat-layout join is wrong for a product-scoped journey, which
    lives one level deeper at `<journeys_dir>/<product_slug>/<journey_date>/...`.
    """
    out: List[Tuple[str, str, bool]] = []
    for journey_id, journey_date in filesystem.list_journeys(journeys_dir, product_slug):
        shard = filesystem.find_journey_path(journeys_dir, journey_id)
        try:
            if shard is None:
                raise OSError(f"shard for {journey_id!r} vanished mid-listing")
            complete = fold_shard(shard).complete
        except (OSError, ValueError):
            complete = False
        out.append((journey_id, journey_date, complete))
    return out


def list_journeys_with_status_indexed(
    index: IndexHandle, product_slug: Optional[str], date: Optional[str]
) -> List[Tuple[str, str, bool]]:
    """Index-backed replacement for `list_journeys_with_status`: reads
    `journey_id`/`date`/`complete` straight out of the `journeys` table
    (populated at index time by `odyssey_api.index.journeys_indexer`)
    instead of re-walking `journeys_dir` and re-folding every shard on
    every request."""
    sql = "SELECT journey_id, date, complete FROM journeys WHERE 1=1"
    params: list = []
    if product_slug is not None:
        sql += " AND product_slug = ?"
        params.append(product_slug)
    if date is not None:
        sql += " AND date = ?"
        params.append(date)
    sql += " ORDER BY date, journey_id"
    rows = index.query(sql, tuple(params))
    return [(r["journey_id"], r["date"], bool(r["complete"])) for r in rows]


def get_journey(journeys_dir: Path, journey_id: str) -> FoldResult:
    path = filesystem.find_journey_path(journeys_dir, journey_id)
    if path is None:
        raise JourneyNotFoundError(journey_id)
    return fold_shard(path)
