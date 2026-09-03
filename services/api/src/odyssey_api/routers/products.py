from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends
from odyssey_schemas import ProductOut

from odyssey_api import deps
from odyssey_api.domain import products as domain
from odyssey_api.settings import Settings

router = APIRouter(prefix="/products", tags=["products"])


@router.get("", response_model=List[ProductOut])
def list_products(
    settings: Settings = Depends(deps.get_settings_dep),
) -> List[ProductOut]:
    return [ProductOut(**p) for p in domain.list_products(settings.products_file)]
