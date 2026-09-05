from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from odyssey_schemas import EvalRunOut, EvalRunPageOut

from odyssey_api import deps
from odyssey_api.domain import eval_runs as domain
from odyssey_api.pagination import paginate
from odyssey_api.settings import Settings

router = APIRouter(prefix="/runs", tags=["runs"])


@router.get("", response_model=EvalRunPageOut)
def list_runs(
    cursor: Optional[str] = None,
    limit: Optional[int] = None,
    settings: Settings = Depends(deps.get_settings_dep),
) -> EvalRunPageOut:
    """``?cursor=``/``?limit=`` paginate the result — see
    `odyssey_api.pagination.paginate`."""
    all_runs = [EvalRunOut(**run) for run in domain.list_eval_runs(settings.eval_reports_dir)]
    items, next_cursor, has_more, total = paginate(all_runs, cursor, limit)
    return EvalRunPageOut(items=items, next_cursor=next_cursor, has_more=has_more, total=total)
