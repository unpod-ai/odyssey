"""Products use-case — reads `services/collector`'s `--products-file`
roster (tenant `slug`/`name` pairs). `api_key` is stripped at the
repository layer (`filesystem.read_products`), not here, since a
read-only listing endpoint must never be in a position to echo the
tenant secret back.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from odyssey_api.repositories import filesystem

__all__ = ["list_products"]


def list_products(products_file: Optional[Path]) -> List[Dict[str, Any]]:
    return filesystem.read_products(products_file)
