from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends
from odyssey_schemas import ProductOut

from odyssey_api.deps import get_index_dep
from odyssey_api.domain import products as domain
from odyssey_api.index.manager import IndexHandle

router = APIRouter(prefix="/products", tags=["products"])


@router.get("", response_model=List[ProductOut])
def list_products(index: IndexHandle = Depends(get_index_dep)) -> List[ProductOut]:
    return [ProductOut(**p) for p in domain.list_products_indexed(index)]
