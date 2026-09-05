from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from odyssey_schemas import MetricsPageOut, MetricsSnapshotOut
from pydantic import ValidationError

from odyssey_api import deps
from odyssey_api.domain import metrics as domain
from odyssey_api.pagination import paginate
from odyssey_api.settings import Settings

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.get("", response_model=MetricsPageOut)
def list_metrics(
    product: Optional[str] = None,
    cursor: Optional[str] = None,
    limit: Optional[int] = None,
    settings: Settings = Depends(deps.get_settings_dep),
) -> MetricsPageOut:
    """A snapshot missing `MetricsSnapshotOut`'s required fields (older
    probe payloads predate ``ts``/``os`` being sent on every snapshot) is
    skipped rather than 500ing the whole listing — the same
    "malformed entries don't take down the rest" defensiveness
    `domain.list_metrics` already applies to unparseable JSON lines.
    ``?product=<slug>`` narrows to one product's ``metrics/`` directory,
    same filter `/journeys` supports. ``?cursor=``/``?limit=`` paginate
    the result — see `odyssey_api.pagination.paginate`."""
    all_snapshots = []
    for m in domain.list_metrics(settings.journeys_dir, product):
        try:
            all_snapshots.append(MetricsSnapshotOut(**m))
        except ValidationError:
            continue
    items, next_cursor, has_more, total = paginate(all_snapshots, cursor, limit)
    return MetricsPageOut(items=items, next_cursor=next_cursor, has_more=has_more, total=total)
