from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends
from odyssey_schemas import MetricsSnapshotOut

from odyssey_api import deps
from odyssey_api.domain import metrics as domain
from odyssey_api.settings import Settings

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.get("", response_model=List[MetricsSnapshotOut])
def list_metrics(
    settings: Settings = Depends(deps.get_settings_dep),
) -> List[MetricsSnapshotOut]:
    return [MetricsSnapshotOut(**m) for m in domain.list_metrics(settings.journeys_dir)]
