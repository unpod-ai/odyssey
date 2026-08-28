from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from odyssey_schemas import DatasetOut, DatasetVersionOut

from odyssey_api import deps
from odyssey_api.domain import registries as domain
from odyssey_api.settings import Settings

router = APIRouter(prefix="/datasets", tags=["datasets"])


@router.get("", response_model=List[DatasetOut])
def list_datasets(
    settings: Settings = Depends(deps.get_settings_dep),
) -> List[DatasetOut]:
    corpora = domain.list_datasets(settings.datasets_registry)
    return [
        DatasetOut(
            name=name,
            versions=[
                DatasetVersionOut(
                    version=v["version"],
                    manifest_sha256=v["manifest_sha256"],
                    uri=v["uri"],
                )
                for v in versions
            ],
        )
        for name, versions in corpora.items()
    ]


@router.get("/{name}", response_model=DatasetOut)
def get_dataset(
    name: str, settings: Settings = Depends(deps.get_settings_dep)
) -> DatasetOut:
    corpora = domain.list_datasets(settings.datasets_registry)
    if name not in corpora:
        raise HTTPException(status_code=404, detail=f"dataset {name!r} not found")
    return DatasetOut(
        name=name,
        versions=[
            DatasetVersionOut(
                version=v["version"], manifest_sha256=v["manifest_sha256"], uri=v["uri"]
            )
            for v in corpora[name]
        ],
    )
