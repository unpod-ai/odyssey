"""Products use-case. `list_products` reads `services/collector`'s
`--products-file` roster (tenant `slug`/`name` pairs) and is retained for
callers still on the filesystem path; `list_products_indexed` reads the
SQLite index's `products` table instead, which is what `GET /products`
now uses. In both cases `api_key`/`api_key_hash` is intentionally never
returned, since a read-only listing endpoint must never be in a position
to echo the tenant secret (or its hash) back.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from odyssey_api.index.manager import IndexHandle
from odyssey_api.repositories import filesystem

__all__ = ["list_products", "list_products_indexed"]


def list_products(products_file: Optional[Path]) -> List[Dict[str, Any]]:
    return filesystem.read_products(products_file)


def list_products_indexed(index: IndexHandle) -> List[Dict[str, Any]]:
    rows = index.query("SELECT slug, name FROM products WHERE revoked = 0 ORDER BY slug")
    return [dict(row) for row in rows]
