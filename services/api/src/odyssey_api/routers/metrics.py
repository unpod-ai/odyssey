from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends
from odyssey_schemas import MetricsSnapshotOut
from pydantic import ValidationError

from odyssey_api import deps
from odyssey_api.domain import metrics as domain
from odyssey_api.settings import Settings

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.get("", response_model=List[MetricsSnapshotOut])
def list_metrics(
    product: Optional[str] = None,
    settings: Settings = Depends(deps.get_settings_dep),
) -> List[MetricsSnapshotOut]:
    """A snapshot missing `MetricsSnapshotOut`'s required fields (older
    probe payloads predate ``ts``/``os`` being sent on every snapshot) is
    skipped rather than 500ing the whole listing — the same
    "malformed entries don't take down the rest" defensiveness
    `domain.list_metrics` already applies to unparseable JSON lines.
    ``?product=<slug>`` narrows to one product's ``metrics/`` directory,
    same filter `/journeys` supports."""
    out: List[MetricsSnapshotOut] = []
    for m in domain.list_metrics(settings.journeys_dir, product):
        try:
            out.append(MetricsSnapshotOut(**m))
        except ValidationError:
            continue
    return out
