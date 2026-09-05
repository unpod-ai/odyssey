"""Product create/list/revoke/rotate/migrate against the shared
products table -- the only writer of that table in the whole system
(see the design spec's per-table ownership rule). Replaces the old
`_init_products_file`/`_add_product` file-mutating functions entirely.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from odyssey_store.auth import hash_api_key
from odyssey_store.db import connect

from odyssey_collector.auth_cache import CreatedProduct


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_product(db_uri: str, slug: str, name: str) -> CreatedProduct:
    api_key = secrets.token_urlsafe(32)
    conn = connect(db_uri)
    try:
        existing = conn.execute(
            "SELECT 1 FROM products WHERE slug = ?", (slug,)
        ).fetchone()
        if existing is not None:
            raise ValueError(f"a product with slug {slug!r} already exists")
        try:
            conn.execute(
                "INSERT INTO products (slug, name, api_key_hash, revoked, created_at) VALUES (?, ?, ?, 0, ?)",
                (slug, name, hash_api_key(api_key), _now()),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"a product with slug {slug!r} already exists") from None
        conn.commit()
    finally:
        conn.close()
    return CreatedProduct(slug=slug, name=name, api_key=api_key)


def list_products(db_uri: str) -> List[Dict[str, Any]]:
    conn = connect(db_uri)
    try:
        rows = conn.execute(
            "SELECT slug, name, revoked, created_at FROM products ORDER BY slug"
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "slug": r["slug"],
            "name": r["name"],
            "revoked": bool(r["revoked"]),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def revoke_product(db_uri: str, slug: str) -> None:
    conn = connect(db_uri)
    try:
        cursor = conn.execute("UPDATE products SET revoked = 1 WHERE slug = ?", (slug,))
        conn.commit()
        if cursor.rowcount == 0:
            raise KeyError(f"no product with slug {slug!r}")
    finally:
        conn.close()


def rotate_product(db_uri: str, slug: str) -> CreatedProduct:
    conn = connect(db_uri)
    try:
        row = conn.execute(
            "SELECT name FROM products WHERE slug = ?", (slug,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no product with slug {slug!r}")
        new_key = secrets.token_urlsafe(32)
        conn.execute(
            "UPDATE products SET api_key_hash = ?, revoked = 0 WHERE slug = ?",
            (hash_api_key(new_key), slug),
        )
        conn.commit()
        name = row["name"]
    finally:
        conn.close()
    return CreatedProduct(slug=slug, name=name, api_key=new_key)


def migrate_products_from_json(db_uri: str, json_path: Path) -> int:
    """One-time cutover: hashes each already-existing plaintext api_key
    as-is, no rotation forced -- already-integrated tenants keep working
    with the same key they already have.

    This is the sole production cutover path off the old ``products.json``
    file for every existing deployment -- an operator running this once,
    mid-migration, deserves a clear error naming what's wrong rather than a
    bare ``KeyError``/``TypeError``/``sqlite3.IntegrityError`` traceback.
    Every entry is validated up front, before any row is inserted, so a
    malformed entry anywhere in the file leaves the database completely
    untouched (no partial migration).
    """
    raw = json.loads(Path(json_path).read_text(encoding="utf-8"))
    entries = raw.get("products") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise ValueError(f"{json_path}: expected a {{'products': [...]}} roster")

    for i, entry in enumerate(entries):
        if not isinstance(entry, dict) or not all(
            isinstance(entry.get(k), str) and entry.get(k)
            for k in ("slug", "name", "api_key")
        ):
            raise ValueError(
                f"{json_path}: products[{i}] must have non-empty string "
                "'slug', 'name', and 'api_key' fields"
            )

    keys = [entry["api_key"] for entry in entries]
    if len(set(keys)) != len(keys):
        raise ValueError(f"{json_path}: the same api_key is registered to two products")

    conn = connect(db_uri)
    try:
        count = 0
        try:
            for entry in entries:
                conn.execute(
                    """
                    INSERT INTO products (slug, name, api_key_hash, revoked, created_at)
                    VALUES (?, ?, ?, 0, ?)
                    ON CONFLICT(slug) DO UPDATE SET
                        name = excluded.name, api_key_hash = excluded.api_key_hash
                    """,
                    (
                        entry["slug"],
                        entry["name"],
                        hash_api_key(entry["api_key"]),
                        _now(),
                    ),
                )
                count += 1
        except sqlite3.IntegrityError:
            conn.rollback()
            raise ValueError(
                f"{json_path}: the same api_key is registered to two products"
            ) from None
        conn.commit()
    finally:
        conn.close()
    return count
