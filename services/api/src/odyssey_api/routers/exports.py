from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends
from odyssey_schemas import ExportArtifactOut

from odyssey_api import deps
from odyssey_api.domain import exports as domain
from odyssey_api.settings import Settings

router = APIRouter(prefix="/exports", tags=["exports"])


@router.get("", response_model=List[ExportArtifactOut])
def list_exports(
    settings: Settings = Depends(deps.get_settings_dep),
) -> List[ExportArtifactOut]:
    return [ExportArtifactOut(**e) for e in domain.list_exports(settings.exports_dir)]
