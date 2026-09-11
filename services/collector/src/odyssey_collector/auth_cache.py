"""In-memory auth cache for services/collector's product lookup --
avoids a DB round-trip on every ingest request. See the design spec's
"Collector's auth-check cost" decision: a whole-table cache refreshed on
a background thread, with a direct DB query on a cache miss so a
just-created product authenticates immediately rather than waiting for
the next refresh.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Dict, Optional

from odyssey_store.auth import hash_api_key
from odyssey_store.db import connect


@dataclass(frozen=True)
class Product:
    """A registered tenant, as read from the `products` table -- never
    carries the plaintext api_key (see `CreatedProduct` for the one-shot
    exception at creation/rotation time)."""

    slug: str
    name: str
    api_key_hash: str


@dataclass(frozen=True)
class CreatedProduct:
    """The one-shot result of creating or rotating a product's key --
    the only place a plaintext api_key exists after generation, and only
    for as long as it takes the CLI to print it."""

    slug: str
    name: str
    api_key: str


class AuthCache:
    def __init__(self, db_uri: str, ttl_seconds: float) -> None:
        self._db_uri = db_uri
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._by_hash: Dict[str, Product] = {}
        self._stop_event = threading.Event()
        self._refresh()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _refresh(self) -> None:
        conn = connect(self._db_uri)
        try:
            rows = conn.execute(
                "SELECT slug, name, api_key_hash FROM products WHERE revoked = 0"
            ).fetchall()
        finally:
            conn.close()
        by_hash = {
            row["api_key_hash"]: Product(row["slug"], row["name"], row["api_key_hash"])
            for row in rows
        }
        with self._lock:
            self._by_hash = by_hash

    def _loop(self) -> None:
        while not self._stop_event.wait(self._ttl_seconds):
            self._refresh()

    def lookup(self, api_key: str) -> Optional[Product]:
        key_hash = hash_api_key(api_key)
        with self._lock:
            product = self._by_hash.get(key_hash)
        if product is not None:
            return product

        # Cache miss: query the DB directly rather than rejecting outright,
        # so a product created seconds ago authenticates immediately.
        conn = connect(self._db_uri)
        try:
            row = conn.execute(
                "SELECT slug, name, api_key_hash FROM products WHERE api_key_hash = ? AND revoked = 0",
                (key_hash,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return Product(row["slug"], row["name"], row["api_key_hash"])

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=5)
