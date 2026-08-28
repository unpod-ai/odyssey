from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends
from odyssey_schemas import EvalRunOut

from odyssey_api import deps
from odyssey_api.domain import eval_runs as domain
from odyssey_api.settings import Settings

router = APIRouter(prefix="/runs", tags=["runs"])


@router.get("", response_model=List[EvalRunOut])
def list_runs(settings: Settings = Depends(deps.get_settings_dep)) -> List[EvalRunOut]:
    return [
        EvalRunOut(**run) for run in domain.list_eval_runs(settings.eval_reports_dir)
    ]
