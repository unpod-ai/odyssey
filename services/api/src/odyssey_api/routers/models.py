from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from odyssey_schemas import ModelOut, ModelVersionOut

from odyssey_api import deps
from odyssey_api.domain import registries as domain
from odyssey_api.settings import Settings

router = APIRouter(prefix="/models", tags=["models"])


def _version_out(v: dict) -> ModelVersionOut:
    return ModelVersionOut(
        version=v["version"],
        sha256=v["sha256"],
        uri=v["uri"],
        base_model=v.get("base_model"),
        corpus_version=v.get("corpus_version"),
    )


@router.get("", response_model=List[ModelOut])
def list_models(settings: Settings = Depends(deps.get_settings_dep)) -> List[ModelOut]:
    models = domain.list_models(settings.models_registry)
    return [
        ModelOut(name=name, versions=[_version_out(v) for v in versions])
        for name, versions in models.items()
    ]


@router.get("/{name}", response_model=ModelOut)
def get_model(
    name: str, settings: Settings = Depends(deps.get_settings_dep)
) -> ModelOut:
    models = domain.list_models(settings.models_registry)
    if name not in models:
        raise HTTPException(status_code=404, detail=f"model {name!r} not found")
    return ModelOut(name=name, versions=[_version_out(v) for v in models[name]])
