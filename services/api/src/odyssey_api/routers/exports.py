from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from odyssey_schemas import ExportArtifactOut, ExportPageOut

from odyssey_api.deps import get_index_dep
from odyssey_api.domain import exports as domain
from odyssey_api.index.manager import IndexHandle
from odyssey_api.pagination import paginate

router = APIRouter(prefix="/exports", tags=["exports"])


@router.get("", response_model=ExportPageOut)
def list_exports(
    cursor: Optional[str] = None,
    limit: Optional[int] = None,
    index: IndexHandle = Depends(get_index_dep),
) -> ExportPageOut:
    """``?cursor=``/``?limit=`` paginate the result — see
    `odyssey_api.pagination.paginate`."""
    all_exports = [ExportArtifactOut(**e) for e in domain.list_exports_indexed(index)]
    items, next_cursor, has_more, total = paginate(all_exports, cursor, limit)
    return ExportPageOut(
        items=items, next_cursor=next_cursor, has_more=has_more, total=total
    )
