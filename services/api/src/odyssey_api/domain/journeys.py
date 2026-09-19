"""Journeys use-case (routers/journeys.py) — reads exactly what
`services/collector` writes, through the same `fold_shard` every exporter
(`odyssey sft`/`odyssey dpo`/Trajectory JSON) already uses, so a journey
looks identical here to how it looks everywhere else in the repo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from odyssey.export import fold_shard
from odyssey.fold import FoldResult
from odyssey.jsonl import read_events

from odyssey_api.domain.provenance import Provenance, provenance, split_providers
from odyssey_api.index.manager import IndexHandle
from odyssey_api.repositories import filesystem

__all__ = [
    "IndexedJourney",
    "JourneyNotFoundError",
    "list_journeys",
    "list_journeys_with_status",
    "list_journeys_with_status_indexed",
    "get_journey",
]


class JourneyNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class IndexedJourney:
    """One row of the `journeys` table, as the listing endpoint returns it."""

    journey_id: str
    date: str
    complete: bool
    provenance: Provenance = field(default_factory=Provenance)


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
    for journey_id, journey_date in filesystem.list_journeys(
        journeys_dir, product_slug
    ):
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
) -> List[IndexedJourney]:
    """Index-backed replacement for `list_journeys_with_status`: reads each
    journey's row straight out of the `journeys` table (populated at index
    time by `odyssey_api.index.journeys_indexer`) instead of re-walking
    `journeys_dir` and re-folding every shard on every request.

    Schema 2.1's provenance and timing come from the same row, so a listing
    carries them without a second read of anything."""
    sql = (
        "SELECT journey_id, date, complete, framework, parent_journey_id, "
        "providers, avg_latency_ms, avg_ttft_ms FROM journeys WHERE 1=1"
    )
    params: list = []
    if product_slug is not None:
        sql += " AND product_slug = ?"
        params.append(product_slug)
    if date is not None:
        sql += " AND date = ?"
        params.append(date)
    # Newest date first by default -- matches the dashboard's own
    # date-count chips, which are already sorted descending.
    sql += " ORDER BY date DESC, journey_id DESC"
    return [
        IndexedJourney(
            journey_id=r["journey_id"],
            date=r["date"],
            complete=bool(r["complete"]),
            provenance=Provenance(
                framework=r["framework"],
                parent_journey_id=r["parent_journey_id"],
                providers=split_providers(r["providers"]),
                avg_latency_ms=r["avg_latency_ms"],
                avg_ttft_ms=r["avg_ttft_ms"],
            ),
        )
        for r in index.query(sql, tuple(params))
    ]


def get_journey(journeys_dir: Path, journey_id: str) -> Tuple[FoldResult, Provenance]:
    """One journey, folded, with the provenance its header carries.

    The header is read here rather than in the router because `fold_shard`
    keeps none of it, and `framework`/`parent_journey_id` live nowhere else.
    """
    path = filesystem.find_journey_path(journeys_dir, journey_id)
    if path is None:
        raise JourneyNotFoundError(journey_id)
    read = read_events(path)
    if not read.events:
        raise JourneyNotFoundError(journey_id)
    result = fold_shard(path)
    return result, provenance(result, read.header)
