from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from odyssey_schemas import (
    CountsOut,
    MetricsPageOut,
    MetricsSnapshotOut,
    ProductCountOut,
    ProjectCountOut,
)
from pydantic import ValidationError

from odyssey_api.deps import get_index_dep
from odyssey_api.domain import metrics as domain
from odyssey_api.index.manager import IndexHandle
from odyssey_api.pagination import paginate

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.get("", response_model=MetricsPageOut)
def list_metrics(
    product: Optional[str] = None,
    cursor: Optional[str] = None,
    limit: Optional[int] = None,
    index: IndexHandle = Depends(get_index_dep),
) -> MetricsPageOut:
    """A snapshot missing `MetricsSnapshotOut`'s required fields (older
    probe payloads predate ``ts``/``os`` being sent on every snapshot) is
    skipped rather than 500ing the whole listing — the same
    "malformed entries don't take down the rest" defensiveness
    `domain.list_metrics` already applies to unparseable JSON lines.
    ``?product=<slug>`` narrows to one product against the SQLite index
    (see `odyssey_api.index`), same filter `/journeys` supports.
    ``?cursor=``/``?limit=`` paginate the result — see
    `odyssey_api.pagination.paginate`."""
    all_snapshots = []
    for m in domain.list_metrics_indexed(index, product):
        try:
            all_snapshots.append(MetricsSnapshotOut(**m))
        except ValidationError:
            continue
    items, next_cursor, has_more, total = paginate(all_snapshots, cursor, limit)
    return MetricsPageOut(items=items, next_cursor=next_cursor, has_more=has_more, total=total)


@router.get("/counts", response_model=CountsOut)
def metrics_counts(index: IndexHandle = Depends(get_index_dep)) -> CountsOut:
    by_product = index.query(
        "SELECT product_slug, COUNT(*) AS count FROM metrics_snapshots GROUP BY product_slug"
    )
    by_project = index.query(
        "SELECT product_slug, project, COUNT(*) AS count FROM metrics_snapshots GROUP BY product_slug, project"
    )
    return CountsOut(
        by_product=[ProductCountOut(**dict(r)) for r in by_product],
        by_project=[ProjectCountOut(**dict(r)) for r in by_project],
        by_date=[],
    )
