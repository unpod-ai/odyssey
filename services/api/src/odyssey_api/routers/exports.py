from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from odyssey_schemas import ExportArtifactOut, ExportPageOut

from odyssey_api import deps
from odyssey_api.domain import exports as domain
from odyssey_api.pagination import paginate
from odyssey_api.settings import Settings

router = APIRouter(prefix="/exports", tags=["exports"])


@router.get("", response_model=ExportPageOut)
def list_exports(
    cursor: Optional[str] = None,
    limit: Optional[int] = None,
    settings: Settings = Depends(deps.get_settings_dep),
) -> ExportPageOut:
    """``?cursor=``/``?limit=`` paginate the result — see
    `odyssey_api.pagination.paginate`."""
    all_exports = [ExportArtifactOut(**e) for e in domain.list_exports(settings.exports_dir)]
    items, next_cursor, has_more, total = paginate(all_exports, cursor, limit)
    return ExportPageOut(items=items, next_cursor=next_cursor, has_more=has_more, total=total)
