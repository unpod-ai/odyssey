# services/collector Product Management on SQLite — Implementation Plan (Part B)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move `services/collector`'s product/tenant roster off a hand-edited `products.json` file onto the shared `ODYSSEY_DB_URI` SQLite file, with hash-only `api_key` storage (never plaintext, anywhere, at rest) and a new CLI replacing the file-editing flags entirely.

**Architecture:** `products` is a table in the same shared SQLite file `services/api`'s index uses (see Part A) — collector is its only writer. Auth checks go through an in-memory `AuthCache` (whole-table cache, refreshed on a background thread every `ODYSSEY_COLLECTOR_AUTH_CACHE_TTL_SECONDS`, with a DB-query fallback on cache miss) instead of a static in-memory tuple loaded once from JSON at startup.

**Tech Stack:** Python stdlib `sqlite3` (via `packages/odyssey-store`, built in Part A), stdlib `hashlib`/`secrets`, `argparse` (this service's existing CLI style — no typer here, matching its "stdlib only" discipline).

**Spec:** `docs/superpowers/specs/2026-09-05-api-sqlite-index-design.md` (Component 2)

**Depends on:** Part A's Task 1 (`packages/odyssey-store` must exist with the `products` table in its schema) must be complete before starting this plan.

## Global Constraints

- No plaintext `api_key` is ever written to disk, logged, or held in memory longer than the single `create`/`rotate` call that generates and returns it to the CLI for one-time printing.
- `ODYSSEY_COLLECTOR_AUTH_CACHE_TTL_SECONDS` default `60`, env-overridable.
- A corrupt/unreadable shared DB file must cause the process to fail to start with a clear error — never silently deleted or rebuilt (Part A's `products` table holds real, unrecoverable tenant credentials once this ships).
- `--api-key` (the separate single-shared-key, unscoped mode) is untouched by this plan.
- Every new/changed behavior in `services/collector/src/odyssey_collector/server.py` keeps this repo's existing "no mocks, real server on an ephemeral port" test convention (`services/collector/tests/test_server.py`'s established pattern).

---

### Task 1: `hash_api_key` in `odyssey_store` + collector's `db_uri` config

**Files:**
- Create: `packages/odyssey-store/src/odyssey_store/auth.py`
- Modify: `packages/odyssey-store/src/odyssey_store/__init__.py`
- Test: `packages/odyssey-store/tests/test_auth.py`
- Modify: `services/collector/pyproject.toml` (add `odyssey-store` dependency)

**Interfaces:**
- Produces: `odyssey_store.auth.hash_api_key(api_key: str) -> str` (sha256 hex digest).

- [ ] **Step 1: Write the failing test**

```python
# packages/odyssey-store/tests/test_auth.py
from __future__ import annotations

import hashlib

from odyssey_store.auth import hash_api_key


def test_hash_api_key_is_sha256_hex():
    assert hash_api_key("sk-test") == hashlib.sha256(b"sk-test").hexdigest()


def test_hash_api_key_is_deterministic():
    assert hash_api_key("sk-test") == hash_api_key("sk-test")


def test_hash_api_key_differs_for_different_keys():
    assert hash_api_key("sk-a") != hash_api_key("sk-b")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd packages/odyssey-store && uv run pytest tests/test_auth.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'odyssey_store.auth'`

- [ ] **Step 3: Implement**

```python
# packages/odyssey-store/src/odyssey_store/auth.py
"""One hash function, shared by services/collector (who stores it, who
authenticates against it) and any future migration/audit tooling (who
needs to reproduce it from an already-issued key) -- a single
implementation so "how is a key hashed" is never a question with two
different answers.
"""

from __future__ import annotations

import hashlib


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()
```

Add `from odyssey_store.auth import hash_api_key` and `"hash_api_key"` to `packages/odyssey-store/src/odyssey_store/__init__.py`'s import and `__all__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd packages/odyssey-store && uv run pytest tests/test_auth.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Add the collector dependency and config field**

Add `"odyssey-store"` to `services/collector/pyproject.toml`'s `dependencies` list and `[tool.uv.sources]` (`odyssey-store = { workspace = true }`).

- [ ] **Step 6: Run `uv sync` and commit**

```bash
uv sync
git add packages/odyssey-store services/collector/pyproject.toml uv.lock
git commit -m "feat(store): add hash_api_key, wire odyssey-store into collector"
```

---

### Task 2: `Product`/`CreatedProduct` + `AuthCache`

**Files:**
- Create: `services/collector/src/odyssey_collector/auth_cache.py`
- Test: `services/collector/tests/test_auth_cache.py`

**Interfaces:**
- Produces: `Product` (dataclass: `slug: str`, `name: str`, `api_key_hash: str` — no plaintext field, replacing the old `Product(slug, name, api_key)`). `CreatedProduct` (dataclass: `slug: str`, `name: str`, `api_key: str` — the one-shot plaintext-carrying return of a create/rotate operation, never persisted). `AuthCache(db_uri: str, ttl_seconds: float)` with `.lookup(api_key: str) -> Product | None` and `.stop() -> None`.

- [ ] **Step 1: Write the failing test**

```python
# services/collector/tests/test_auth_cache.py
from __future__ import annotations

import time

from odyssey_store.auth import hash_api_key
from odyssey_store.db import connect

from odyssey_collector.auth_cache import AuthCache


def _seed(db_uri, slug, name, api_key):
    conn = connect(db_uri)
    try:
        conn.execute(
            "INSERT INTO products (slug, name, api_key_hash, revoked, created_at) "
            "VALUES (?, ?, ?, 0, '2026-01-01T00:00:00+00:00')",
            (slug, name, hash_api_key(api_key)),
        )
        conn.commit()
    finally:
        conn.close()


def test_lookup_hits_the_initial_cache(tmp_path):
    db_uri = f"sqlite:///{tmp_path}/db.sqlite3"
    _seed(db_uri, "acme", "Acme Corp", "sk-acme")
    cache = AuthCache(db_uri, ttl_seconds=3600)

    product = cache.lookup("sk-acme")

    assert product is not None
    assert product.slug == "acme"
    cache.stop()


def test_lookup_returns_none_for_unknown_key(tmp_path):
    db_uri = f"sqlite:///{tmp_path}/db.sqlite3"
    cache = AuthCache(db_uri, ttl_seconds=3600)

    assert cache.lookup("sk-nope") is None
    cache.stop()


def test_lookup_falls_through_to_db_on_cache_miss_for_newly_created_product(tmp_path):
    db_uri = f"sqlite:///{tmp_path}/db.sqlite3"
    cache = AuthCache(db_uri, ttl_seconds=3600)  # long TTL: cache won't refresh on its own
    _seed(db_uri, "newco", "New Co", "sk-newco")  # created after the cache's initial load

    product = cache.lookup("sk-newco")

    assert product is not None
    assert product.slug == "newco"
    cache.stop()


def test_revoked_product_is_excluded(tmp_path):
    db_uri = f"sqlite:///{tmp_path}/db.sqlite3"
    _seed(db_uri, "acme", "Acme Corp", "sk-acme")
    conn = connect(db_uri)
    conn.execute("UPDATE products SET revoked = 1 WHERE slug = 'acme'")
    conn.commit()
    conn.close()

    cache = AuthCache(db_uri, ttl_seconds=3600)
    assert cache.lookup("sk-acme") is None
    cache.stop()


def test_background_refresh_picks_up_revocation(tmp_path):
    db_uri = f"sqlite:///{tmp_path}/db.sqlite3"
    _seed(db_uri, "acme", "Acme Corp", "sk-acme")
    cache = AuthCache(db_uri, ttl_seconds=1)
    assert cache.lookup("sk-acme") is not None

    conn = connect(db_uri)
    conn.execute("UPDATE products SET revoked = 1 WHERE slug = 'acme'")
    conn.commit()
    conn.close()
    time.sleep(1.5)  # let the background thread refresh at least once

    assert cache.lookup("sk-acme") is None
    cache.stop()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/collector && uv run pytest tests/test_auth_cache.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
# services/collector/src/odyssey_collector/auth_cache.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/collector && uv run pytest tests/test_auth_cache.py -v`
Expected: PASS (5 tests; the last takes ~1.5s)

- [ ] **Step 5: Commit**

```bash
git add services/collector/src/odyssey_collector/auth_cache.py services/collector/tests/test_auth_cache.py
git commit -m "feat(collector): in-memory AuthCache backed by the shared products table"
```

---

### Task 3: `products_db.py` — create/list/revoke/rotate/migrate-from-json

**Files:**
- Create: `services/collector/src/odyssey_collector/products_db.py`
- Test: `services/collector/tests/test_products_db.py`

**Interfaces:**
- Consumes: `Product`, `CreatedProduct` (Task 2). `hash_api_key`, `connect` (Task 1, `odyssey_store`).
- Produces: `create_product(db_uri, slug, name) -> CreatedProduct`. `list_products(db_uri) -> list[dict]` (`{"slug", "name", "revoked", "created_at"}`). `revoke_product(db_uri, slug) -> None` (raises `KeyError` if slug unknown). `rotate_product(db_uri, slug) -> CreatedProduct` (raises `KeyError` if slug unknown). `migrate_products_from_json(db_uri, json_path) -> int` (count migrated).

- [ ] **Step 1: Write the failing test**

```python
# services/collector/tests/test_products_db.py
from __future__ import annotations

import json

import pytest
from odyssey_store.auth import hash_api_key
from odyssey_store.db import connect

from odyssey_collector.products_db import (
    create_product,
    list_products,
    migrate_products_from_json,
    revoke_product,
    rotate_product,
)


def test_create_product_stores_only_a_hash(tmp_path):
    db_uri = f"sqlite:///{tmp_path}/db.sqlite3"

    created = create_product(db_uri, "acme", "Acme Corp")

    assert created.slug == "acme"
    assert created.api_key  # a real plaintext key was generated
    conn = connect(db_uri)
    row = conn.execute("SELECT api_key_hash FROM products WHERE slug = 'acme'").fetchone()
    conn.close()
    assert row["api_key_hash"] == hash_api_key(created.api_key)
    assert created.api_key != row["api_key_hash"]


def test_create_product_refuses_a_duplicate_slug(tmp_path):
    db_uri = f"sqlite:///{tmp_path}/db.sqlite3"
    create_product(db_uri, "acme", "Acme Corp")

    with pytest.raises(ValueError, match="already exists"):
        create_product(db_uri, "acme", "Acme Again")


def test_list_products_never_includes_a_key_or_hash(tmp_path):
    db_uri = f"sqlite:///{tmp_path}/db.sqlite3"
    create_product(db_uri, "acme", "Acme Corp")

    products = list_products(db_uri)

    assert products == [{"slug": "acme", "name": "Acme Corp", "revoked": False, "created_at": products[0]["created_at"]}]
    assert "api_key" not in json.dumps(products)
    assert "hash" not in json.dumps(products)


def test_revoke_product_marks_revoked(tmp_path):
    db_uri = f"sqlite:///{tmp_path}/db.sqlite3"
    create_product(db_uri, "acme", "Acme Corp")

    revoke_product(db_uri, "acme")

    products = list_products(db_uri)
    assert products[0]["revoked"] is True


def test_revoke_unknown_slug_raises(tmp_path):
    db_uri = f"sqlite:///{tmp_path}/db.sqlite3"
    with pytest.raises(KeyError):
        revoke_product(db_uri, "nope")


def test_rotate_product_issues_a_new_key_and_invalidates_the_old_hash(tmp_path):
    db_uri = f"sqlite:///{tmp_path}/db.sqlite3"
    created = create_product(db_uri, "acme", "Acme Corp")

    rotated = rotate_product(db_uri, "acme")

    assert rotated.api_key != created.api_key
    conn = connect(db_uri)
    row = conn.execute("SELECT api_key_hash FROM products WHERE slug = 'acme'").fetchone()
    conn.close()
    assert row["api_key_hash"] == hash_api_key(rotated.api_key)
    assert row["api_key_hash"] != hash_api_key(created.api_key)


def test_migrate_products_from_json_preserves_existing_keys(tmp_path):
    json_path = tmp_path / "products.json"
    json_path.write_text(
        json.dumps(
            {
                "products": [
                    {"slug": "acme", "name": "Acme Corp", "api_key": "sk-acme-original"},
                    {"slug": "globex", "name": "Globex Inc", "api_key": "sk-globex-original"},
                ]
            }
        )
    )
    db_uri = f"sqlite:///{tmp_path}/db.sqlite3"

    count = migrate_products_from_json(db_uri, json_path)

    assert count == 2
    conn = connect(db_uri)
    row = conn.execute("SELECT api_key_hash FROM products WHERE slug = 'acme'").fetchone()
    conn.close()
    assert row["api_key_hash"] == hash_api_key("sk-acme-original")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/collector && uv run pytest tests/test_products_db.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
# services/collector/src/odyssey_collector/products_db.py
"""Product create/list/revoke/rotate/migrate against the shared
products table -- the only writer of that table in the whole system
(see the design spec's per-table ownership rule). Replaces the old
`_init_products_file`/`_add_product` file-mutating functions entirely.
"""

from __future__ import annotations

import json
import secrets
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
        existing = conn.execute("SELECT 1 FROM products WHERE slug = ?", (slug,)).fetchone()
        if existing is not None:
            raise ValueError(f"a product with slug {slug!r} already exists")
        conn.execute(
            "INSERT INTO products (slug, name, api_key_hash, revoked, created_at) VALUES (?, ?, ?, 0, ?)",
            (slug, name, hash_api_key(api_key), _now()),
        )
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
        {"slug": r["slug"], "name": r["name"], "revoked": bool(r["revoked"]), "created_at": r["created_at"]}
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
        row = conn.execute("SELECT name FROM products WHERE slug = ?", (slug,)).fetchone()
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
    with the same key they already have."""
    raw = json.loads(Path(json_path).read_text(encoding="utf-8"))
    entries = raw.get("products") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise ValueError(f"{json_path}: expected a {{'products': [...]}} roster")

    conn = connect(db_uri)
    try:
        count = 0
        for entry in entries:
            conn.execute(
                """
                INSERT INTO products (slug, name, api_key_hash, revoked, created_at)
                VALUES (?, ?, ?, 0, ?)
                ON CONFLICT(slug) DO UPDATE SET
                    name = excluded.name, api_key_hash = excluded.api_key_hash
                """,
                (entry["slug"], entry["name"], hash_api_key(entry["api_key"]), _now()),
            )
            count += 1
        conn.commit()
    finally:
        conn.close()
    return count
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/collector && uv run pytest tests/test_products_db.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add services/collector/src/odyssey_collector/products_db.py services/collector/tests/test_products_db.py
git commit -m "feat(collector): products_db create/list/revoke/rotate/migrate-from-json"
```

---

### Task 4: Wire `CollectorConfig` to the DB-backed `AuthCache`, remove file-based products entirely

This is the task that deletes `_load_products_file`, `_init_products_file`, `_add_product`, `ENV_PRODUCTS_FILE`, `ENV_KEYS_FILE_OLD`, and the old `Product(slug, name, api_key)` import from `server.py` — no dual-mode, per the spec.

**Files:**
- Modify: `services/collector/src/odyssey_collector/server.py`
- Modify: `services/collector/tests/test_server.py`

**Interfaces:**
- Produces: `CollectorConfig.db_uri: Optional[str]` (replaces `CollectorConfig.products: Optional[Tuple[Product, ...]]`). `CollectorConfig.product_for_key(api_key: str) -> Optional[Product]` (unchanged signature, now delegates to an internal `AuthCache`). `CollectorConfig.list_products() -> List[Product]` (fresh DB read, for the low-traffic `/products` debug endpoint — not cached, since an operator checking the roster wants it exactly current). `resolve_config(..., db_uri: Optional[str] = None, auth_cache_ttl_seconds: Optional[float] = None, ...)`.

- [ ] **Step 1: Update `CollectorConfig` and `resolve_config`**

In `services/collector/src/odyssey_collector/server.py`:

1. Remove the `Product` dataclass definition (now imported from `odyssey_collector.auth_cache` instead).
2. Remove `_load_products_file`, `_init_products_file`, `_add_product`, `_init_products_file`'s helpers, `ENV_PRODUCTS_FILE`, `ENV_KEYS_FILE_OLD`.
3. Add imports:

```python
from odyssey_collector.auth_cache import AuthCache, Product

ENV_DB_URI = "ODYSSEY_DB_URI"
ENV_AUTH_CACHE_TTL = "ODYSSEY_COLLECTOR_AUTH_CACHE_TTL_SECONDS"
DEFAULT_DB_URI = "sqlite:///./odyssey.sqlite3"
DEFAULT_AUTH_CACHE_TTL_SECONDS = 60.0
```

4. Replace the `CollectorConfig` dataclass's `products` field and `__post_init__`/`product_for_key`:

```python
@dataclass(frozen=True)
class CollectorConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    data_dir: Path = Path(DEFAULT_DATA_DIR)
    api_key: Optional[str] = None
    # Product-scoped mode: the shared SQLite file where `products` lives
    # (see packages/odyssey-store). Mutually exclusive with api_key.
    db_uri: Optional[str] = None
    auth_cache_ttl_seconds: float = DEFAULT_AUTH_CACHE_TTL_SECONDS
    timezone: Optional[str] = None
    debug: bool = False
    date_fn: Callable[[], str] = field(default_factory=lambda: _make_date_fn(None))

    def __post_init__(self) -> None:
        if self.timezone is not None:
            object.__setattr__(self, "date_fn", _make_date_fn(self.timezone))
        if self.api_key is not None and self.db_uri is not None:
            raise ValueError(
                "CollectorConfig: pass either api_key (single shared key, "
                "unscoped) or db_uri (multi-tenant, product-scoped "
                "storage), not both — picking an auth mode is explicit here, "
                "not a silent precedence rule"
            )
        auth_cache = AuthCache(self.db_uri, self.auth_cache_ttl_seconds) if self.db_uri else None
        object.__setattr__(self, "_auth_cache", auth_cache)

    def product_for_key(self, api_key: str) -> Optional[Product]:
        if self._auth_cache is None or not api_key:
            return None
        return self._auth_cache.lookup(api_key)

    def list_products(self) -> List[Product]:
        if self.db_uri is None:
            return []
        conn = connect(self.db_uri)
        try:
            rows = conn.execute(
                "SELECT slug, name, api_key_hash FROM products WHERE revoked = 0 ORDER BY slug"
            ).fetchall()
        finally:
            conn.close()
        return [Product(r["slug"], r["name"], r["api_key_hash"]) for r in rows]
```

(Add `from odyssey_store.db import connect` to the imports; `_auth_cache` is a non-init field set only via `object.__setattr__`, matching this file's existing `date_fn` pattern — it does not need a dataclass `field()` declaration since it's never passed by a caller.)

5. Update `resolve_config`:

```python
def resolve_config(
    *,
    host: Optional[str] = None,
    port: Optional[int] = None,
    data_dir: Optional[Path | str] = None,
    api_key: Optional[str] = None,
    db_uri: Optional[str] = None,
    auth_cache_ttl_seconds: Optional[float] = None,
    timezone: Optional[str] = None,
    debug: Optional[bool] = None,
) -> CollectorConfig:
    resolved_db_uri = db_uri if db_uri is not None else os.environ.get(ENV_DB_URI)
    return CollectorConfig(
        host=host if host is not None else os.environ.get(ENV_HOST, DEFAULT_HOST),
        port=int(port if port is not None else os.environ.get(ENV_PORT, DEFAULT_PORT)),
        data_dir=Path(data_dir if data_dir is not None else os.environ.get(ENV_DATA_DIR, DEFAULT_DATA_DIR)),
        api_key=api_key if api_key is not None else os.environ.get(ENV_API_KEY),
        db_uri=resolved_db_uri,
        auth_cache_ttl_seconds=(
            auth_cache_ttl_seconds
            if auth_cache_ttl_seconds is not None
            else float(os.environ.get(ENV_AUTH_CACHE_TTL, DEFAULT_AUTH_CACHE_TTL_SECONDS))
        ),
        timezone=timezone,
        debug=debug if debug is not None else _truthy(os.environ.get(ENV_DEBUG)),
    )
```

Remove the old `ENV_KEYS_FILE_OLD` fail-fast check block at the top of `resolve_config` (the renamed-env-var guard) — that guard was specific to the JSON-file era and no longer applies.

- [ ] **Step 2: Update `_Handler._get_products`**

```python
    def _get_products(self) -> None:
        config = self.server.config
        if config.db_uri is None:
            self._respond(404, {"error": "not found"})
            return
        authorized, _ = self._authenticate()
        if not authorized:
            self._respond(401, {"error": "missing or invalid Authorization"})
            return
        self._respond(
            200,
            {"products": [{"slug": p.slug, "name": p.name} for p in config.list_products()]},
        )
```

- [ ] **Step 3: Update `_authenticate`**

```python
    def _authenticate(self) -> Tuple[bool, Optional[str]]:
        config = self.server.config
        presented = self.headers.get("Authorization", "")
        if config.db_uri is not None:
            token = presented[len("Bearer "):] if presented.startswith("Bearer ") else ""
            product = config.product_for_key(token)
            return (product is not None, product.slug if product else None)
        if not config.api_key:
            return (True, None)
        return (presented == f"Bearer {config.api_key}", None)
```

- [ ] **Step 4: Rewrite the test fixtures in `test_server.py`**

Replace the `ACME`/`GLOBEX` module-level constants and the `scoped` fixture:

```python
# services/collector/tests/test_server.py
from odyssey_store.auth import hash_api_key
from odyssey_store.db import connect

from odyssey_collector.auth_cache import Product
from odyssey_collector.products_db import create_product


def _seed_product(db_uri, slug, name, api_key):
    conn = connect(db_uri)
    conn.execute(
        "INSERT INTO products (slug, name, api_key_hash, revoked, created_at) "
        "VALUES (?, ?, ?, 0, '2026-01-01T00:00:00+00:00')",
        (slug, name, hash_api_key(api_key)),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def scoped(tmp_path):
    db_uri = f"sqlite:///{tmp_path}/db.sqlite3"
    _seed_product(db_uri, "proj_acme", "Acme Corp", "sk-acme")
    _seed_product(db_uri, "proj_globex", "Globex Inc", "sk-globex")
    config = CollectorConfig(
        host="127.0.0.1",
        port=0,
        data_dir=tmp_path / "data",
        db_uri=db_uri,
        auth_cache_ttl_seconds=3600,
        date_fn=lambda: FIXED_DATE,
    )
    server = serve(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join()
```

Update the module's `from odyssey_collector.server import (...)` import line to drop `Product, _add_product, _init_products_file` and add nothing (those symbols no longer live in `server.py`).

- [ ] **Step 5: Delete the obsolete file-based tests**

Delete these test functions from `test_server.py` entirely (the file-based mechanism they test no longer exists — their DB-backed replacements are Task 3's `test_products_db.py`):
`test_a_malformed_products_file_fails_fast_at_startup`, `test_a_products_file_missing_the_products_key_is_rejected`, `test_a_product_entry_missing_a_field_is_rejected`, `test_a_duplicate_slug_is_rejected` (the products.json-shape one, distinct from any DB-based duplicate-slug test), `test_a_valid_products_file_round_trips_through_resolve_config`, `test_init_products_file_writes_a_loadable_roster`, `test_init_products_file_refuses_to_overwrite_an_existing_file`, `test_init_products_file_generates_a_different_key_each_time`, `test_add_product_appends_to_an_existing_roster`, `test_add_product_refuses_a_duplicate_slug`, `test_add_product_requires_an_existing_roster`.

- [ ] **Step 6: Update `test_api_key_and_products_are_mutually_exclusive`**

```python
def test_api_key_and_db_uri_are_mutually_exclusive(tmp_path):
    with pytest.raises(ValueError, match="not both"):
        CollectorConfig(api_key="sk-shared", db_uri=f"sqlite:///{tmp_path}/db.sqlite3")
```

- [ ] **Step 7: Update `test_a_slug_cannot_traverse_out_of_data_dir`**

```python
def test_a_slug_cannot_traverse_out_of_data_dir(tmp_path):
    db_uri = f"sqlite:///{tmp_path}/db.sqlite3"
    _seed_product(db_uri, "../../etc", "Evil", "sk-evil")
    config = CollectorConfig(
        host="127.0.0.1",
        port=0,
        data_dir=tmp_path / "data",
        db_uri=db_uri,
        auth_cache_ttl_seconds=3600,
        date_fn=lambda: FIXED_DATE,
    )
    server = serve(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        HttpSink(endpoint(server), api_key="sk-evil").send(JID, evs())
        escaped = (config.data_dir / ".." / "etc").resolve()
        assert not escaped.exists()
        written = list(config.data_dir.glob("**/*.jsonl"))
        assert len(written) == 1
        assert written[0].resolve().is_relative_to(config.data_dir.resolve())
    finally:
        server.shutdown()
        thread.join()
```

- [ ] **Step 8: Run the full collector test suite**

Run: `cd services/collector && uv run pytest -v`
Expected: PASS — every test using `scoped`/`running` fixtures unchanged (`test_a_registered_key_lands_under_its_own_product`, `test_two_products_writing_the_same_journey_id_never_collide`, `test_an_unregistered_key_is_rejected_and_nothing_is_written`, `test_a_missing_key_is_rejected_in_product_mode_too`, `test_get_products_lists_the_roster_by_slug_and_name`, `test_get_products_never_includes_api_keys`, `test_get_products_requires_a_registered_key`, `test_get_products_is_404_outside_product_scoped_mode`, `test_a_batch_is_product_scoped_like_single_sends`, `test_a_metrics_snapshot_lands_under_its_product_when_scoped`), the two renamed mutual-exclusivity/traversal tests passing, and the 11 deleted tests gone (replaced by Task 3's `test_products_db.py`).

- [ ] **Step 9: Commit**

```bash
git add services/collector/src/odyssey_collector/server.py services/collector/tests/test_server.py
git commit -m "feat(collector): auth reads from the shared products table, remove file-based roster entirely"
```

---

### Task 5: New CLI flags — create/list/revoke/rotate/migrate-from-json

**Files:**
- Modify: `services/collector/src/odyssey_collector/server.py` (the `main()` function's argparse setup)
- Test: `services/collector/tests/test_cli.py` (create — check first whether collector has a CLI-specific test file separate from `test_server.py`; if not, create one)

**Interfaces:**
- Produces: new CLI flags `--db-uri`, `--create-product`, `--list-products`, `--revoke-product SLUG`, `--rotate-product SLUG`, `--migrate-products-from-json PATH`, each a one-shot action that prints its result and exits without starting the server (same pattern the old `--init-products-file`/`--add-product-file` used).

- [ ] **Step 1: Check for an existing collector CLI test file**

Run: `ls services/collector/tests/`
If no `test_cli.py` exists, this task creates one; `main()` is already tested indirectly via `test_server.py` today, but these one-shot flags deserve their own focused file (matching how `test_server.py`'s own docstring scopes it to "a real server on an ephemeral port" — these flags never start a server).

- [ ] **Step 2: Write the failing test**

```python
# services/collector/tests/test_cli.py
from __future__ import annotations

import json

from odyssey_store.auth import hash_api_key
from odyssey_store.db import connect

from odyssey_collector.server import main


def test_create_product_prints_key_once(tmp_path, capsys):
    db_uri = f"sqlite:///{tmp_path}/db.sqlite3"

    code = main(["--db-uri", db_uri, "--create-product", "--product-slug", "acme", "--product-name", "Acme Corp"])

    assert code == 0
    out = capsys.readouterr().out
    assert "slug='acme'" in out
    conn = connect(db_uri)
    row = conn.execute("SELECT api_key_hash FROM products WHERE slug = 'acme'").fetchone()
    conn.close()
    assert row is not None
    # The printed api_key line must hash to what's stored -- proves a real
    # key was generated and only its hash persisted.
    printed_key = [line for line in out.splitlines() if line.startswith("api_key=")][0].split("=", 1)[1]
    assert hash_api_key(printed_key) == row["api_key_hash"]


def test_list_products_prints_roster_without_keys(tmp_path, capsys):
    db_uri = f"sqlite:///{tmp_path}/db.sqlite3"
    main(["--db-uri", db_uri, "--create-product", "--product-slug", "acme", "--product-name", "Acme Corp"])
    capsys.readouterr()  # discard the create output

    code = main(["--db-uri", db_uri, "--list-products"])

    assert code == 0
    out = capsys.readouterr().out
    assert "acme" in out
    assert "api_key" not in out


def test_revoke_product_then_it_no_longer_lists_as_active(tmp_path, capsys):
    db_uri = f"sqlite:///{tmp_path}/db.sqlite3"
    main(["--db-uri", db_uri, "--create-product", "--product-slug", "acme", "--product-name", "Acme Corp"])
    capsys.readouterr()

    code = main(["--db-uri", db_uri, "--revoke-product", "acme"])

    assert code == 0
    conn = connect(db_uri)
    row = conn.execute("SELECT revoked FROM products WHERE slug = 'acme'").fetchone()
    conn.close()
    assert row["revoked"] == 1


def test_rotate_product_prints_a_new_key(tmp_path, capsys):
    db_uri = f"sqlite:///{tmp_path}/db.sqlite3"
    main(["--db-uri", db_uri, "--create-product", "--product-slug", "acme", "--product-name", "Acme Corp"])
    first_out = capsys.readouterr().out
    first_key = [l for l in first_out.splitlines() if l.startswith("api_key=")][0].split("=", 1)[1]

    code = main(["--db-uri", db_uri, "--rotate-product", "acme"])

    assert code == 0
    second_out = capsys.readouterr().out
    second_key = [l for l in second_out.splitlines() if l.startswith("api_key=")][0].split("=", 1)[1]
    assert second_key != first_key


def test_migrate_products_from_json(tmp_path, capsys):
    json_path = tmp_path / "products.json"
    json_path.write_text(
        json.dumps({"products": [{"slug": "acme", "name": "Acme Corp", "api_key": "sk-original"}]})
    )
    db_uri = f"sqlite:///{tmp_path}/db.sqlite3"

    code = main(["--db-uri", db_uri, "--migrate-products-from-json", str(json_path)])

    assert code == 0
    conn = connect(db_uri)
    row = conn.execute("SELECT api_key_hash FROM products WHERE slug = 'acme'").fetchone()
    conn.close()
    assert row["api_key_hash"] == hash_api_key("sk-original")


def test_create_product_requires_db_uri(capsys):
    code = main(["--create-product", "--product-slug", "acme", "--product-name", "Acme Corp"])

    assert code == 1
    assert "db-uri" in capsys.readouterr().err.lower()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd services/collector && uv run pytest tests/test_cli.py -v`
Expected: FAIL — unrecognized arguments (`--db-uri`, `--create-product`, etc. don't exist yet)

- [ ] **Step 4: Add the flags to `main()`**

In `services/collector/src/odyssey_collector/server.py`'s `main()`, replace the removed `--products-file`/`--init-products-file`/`--add-product-file` `add_argument` calls with:

```python
    parser.add_argument(
        "--db-uri",
        default=None,
        help="shared SQLite file (see packages/odyssey-store) for product-scoped "
        "auth and management flags below; default: $ODYSSEY_DB_URI. "
        "Mutually exclusive with --api-key",
    )
    parser.add_argument(
        "--auth-cache-ttl-seconds",
        type=float,
        default=None,
        help="how long an auth-check result is cached in memory before "
        "re-reading --db-uri; default: 60 ($ODYSSEY_COLLECTOR_AUTH_CACHE_TTL_SECONDS)",
    )
    parser.add_argument(
        "--create-product",
        action="store_true",
        help="create a new product (--product-slug/--product-name) in --db-uri, "
        "print its api_key once, and exit -- does not start the server",
    )
    parser.add_argument(
        "--list-products",
        action="store_true",
        help="list every product in --db-uri (slug/name/revoked/created_at, "
        "never a key) and exit -- does not start the server",
    )
    parser.add_argument(
        "--revoke-product",
        default=None,
        metavar="SLUG",
        help="revoke a product's key in --db-uri and exit -- does not start the server",
    )
    parser.add_argument(
        "--rotate-product",
        default=None,
        metavar="SLUG",
        help="revoke a product's current key and issue a new one in --db-uri, "
        "print it once, and exit -- does not start the server",
    )
    parser.add_argument(
        "--migrate-products-from-json",
        default=None,
        metavar="PATH",
        help="one-time cutover: read an old --products-file-style JSON roster "
        "at PATH, hash each existing api_key as-is, and insert into --db-uri; "
        "exit -- does not start the server",
    )
```

Keep `--product-slug`/`--product-name` (already exist, defaults `"default"`/`"Default"`) — reused by `--create-product`.

Add the handling block, right after argument parsing and before the old `if args.init_products_file is not None:` block (which is being deleted along with `_init_products_file`/`_add_product`):

```python
    admin_actions = [
        args.create_product,
        args.list_products,
        args.revoke_product is not None,
        args.rotate_product is not None,
        args.migrate_products_from_json is not None,
    ]
    if any(admin_actions):
        if not args.db_uri and not os.environ.get(ENV_DB_URI):
            print("--db-uri (or $ODYSSEY_DB_URI) is required for product management flags", file=sys.stderr)
            return 1
        db_uri = args.db_uri or os.environ[ENV_DB_URI]

        from odyssey_collector.products_db import (
            create_product,
            list_products,
            migrate_products_from_json,
            revoke_product,
            rotate_product,
        )

        if args.create_product:
            created = create_product(db_uri, args.product_slug, args.product_name)
            print(f"product: slug={created.slug!r} name={created.name!r}")
            print(f"api_key={created.api_key}")
            print("save this key now -- it will not be printed again", file=sys.stderr)
            return 0

        if args.list_products:
            for p in list_products(db_uri):
                print(f"slug={p['slug']!r} name={p['name']!r} revoked={p['revoked']} created_at={p['created_at']}")
            return 0

        if args.revoke_product is not None:
            try:
                revoke_product(db_uri, args.revoke_product)
            except KeyError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(f"revoked {args.revoke_product!r}")
            return 0

        if args.rotate_product is not None:
            try:
                rotated = rotate_product(db_uri, args.rotate_product)
            except KeyError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(f"product: slug={rotated.slug!r} name={rotated.name!r}")
            print(f"api_key={rotated.api_key}")
            print("save this key now -- it will not be printed again", file=sys.stderr)
            return 0

        if args.migrate_products_from_json is not None:
            count = migrate_products_from_json(db_uri, Path(args.migrate_products_from_json))
            print(f"migrated {count} product(s) into {db_uri}")
            return 0
```

Update the call to `resolve_config(...)` further down to pass `db_uri=args.db_uri, auth_cache_ttl_seconds=args.auth_cache_ttl_seconds` instead of the removed `products_file=args.products_file`.

- [ ] **Step 5: Run test to verify it passes**

Run: `cd services/collector && uv run pytest tests/test_cli.py -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Run the full collector suite once more**

Run: `cd services/collector && uv run pytest -v`
Expected: PASS, full suite (Task 4's tests plus this task's)

- [ ] **Step 7: Commit**

```bash
git add services/collector/src/odyssey_collector/server.py services/collector/tests/test_cli.py
git commit -m "feat(collector): CLI for product create/list/revoke/rotate/migrate-from-json"
```

---

### Task 6: Corruption fail-fast — clear error, never auto-delete

**Files:**
- Modify: `packages/odyssey-store/src/odyssey_store/db.py`
- Test: `packages/odyssey-store/tests/test_db.py` (add to the file created in Part A, Task 1)

**Interfaces:**
- Produces: `odyssey_store.db.CorruptDatabaseError(RuntimeError)`, raised by `connect()` with a message naming the file path when the file exists but isn't a valid/openable SQLite database.

- [ ] **Step 1: Write the failing test**

```python
# packages/odyssey-store/tests/test_db.py -- add:
import pytest

from odyssey_store.db import CorruptDatabaseError, connect


def test_connect_raises_a_clear_error_on_a_corrupt_file(tmp_path):
    bad = tmp_path / "odyssey.sqlite3"
    bad.write_bytes(b"this is not a sqlite file at all, just garbage bytes")

    with pytest.raises(CorruptDatabaseError, match=str(bad)):
        connect(f"sqlite:///{bad}")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd packages/odyssey-store && uv run pytest tests/test_db.py -k corrupt -v`
Expected: FAIL — either no `CorruptDatabaseError` exists, or `connect()` raises a raw `sqlite3.DatabaseError` without the path in a clean message

- [ ] **Step 3: Implement**

```python
# packages/odyssey-store/src/odyssey_store/db.py -- modify connect():
import sqlite3
from pathlib import Path

from odyssey_store.schema import SCHEMA_STATEMENTS

_PREFIX = "sqlite:///"


class CorruptDatabaseError(RuntimeError):
    """Raised instead of letting a raw sqlite3.DatabaseError propagate --
    this file may hold real, unrecoverable tenant credentials (see the
    design spec's corruption-recovery decision), so both services must
    fail loudly and specifically here, never auto-delete-and-rebuild."""


def parse_sqlite_uri(uri: str) -> Path:
    if not uri.startswith(_PREFIX):
        raise ValueError(f"{uri!r}: expected a sqlite:/// URI")
    rest = uri[len(_PREFIX):]
    if rest.startswith("/"):
        return Path("/" + rest.lstrip("/"))
    return Path(rest)


def connect(uri: str) -> sqlite3.Connection:
    path = parse_sqlite_uri(uri)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)
        conn.commit()
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise CorruptDatabaseError(
            f"{path} is not a valid SQLite database (or is corrupt): {exc}. "
            f"This file may hold real tenant credentials — restore from "
            f"backup rather than deleting it."
        ) from exc
    return conn
```

Add `"CorruptDatabaseError"` to `packages/odyssey-store/src/odyssey_store/__init__.py`'s exports.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd packages/odyssey-store && uv run pytest tests/test_db.py -v`
Expected: PASS, full file (the original 4 tests plus this new one)

- [ ] **Step 5: Run both dependent services' full suites to confirm nothing regressed**

Run: `cd services/api && uv run pytest -q && cd ../collector && uv run pytest -q`
Expected: PASS, both suites — `connect()`'s behavior on a *valid* file is unchanged; only the corrupt-file path changed.

- [ ] **Step 6: Commit**

```bash
git add packages/odyssey-store/src/odyssey_store/db.py packages/odyssey-store/src/odyssey_store/__init__.py packages/odyssey-store/tests/test_db.py
git commit -m "feat(store): raise a clear CorruptDatabaseError instead of ever auto-deleting the shared DB"
```

---

### Task 7: Documentation

**Files:**
- Modify: `services/collector/README.md`
- Modify: `docs/runbooks/run-services.md` (check exact filename/path first — referenced in prior session's memory as an existing runbook)

**Interfaces:** None — docs only.

- [ ] **Step 1: Update `services/collector/README.md`**

Replace any `--products-file`/`--init-products-file`/`--add-product-file` documentation with the new `--db-uri`/`--create-product`/`--list-products`/`--revoke-product`/`--rotate-product`/`--migrate-products-from-json` flags, and add a short "Migrating from products.json" section pointing at `--migrate-products-from-json` and explaining the file is retired afterward.

- [ ] **Step 2: Update the run-services runbook**

Search for any mention of `--products-file`/`ODYSSEY_COLLECTOR_PRODUCTS_FILE`/`ODYSSEY_API_PRODUCTS_FILE` in `docs/runbooks/run-services.md` (`grep -rn "products-file\|PRODUCTS_FILE" docs/`) and update each to the new `ODYSSEY_DB_URI` config and CLI flags, including a note that `ODYSSEY_DB_URI` must resolve to the same file for both `services/collector` and `services/api` in a deployment.

- [ ] **Step 3: Commit**

```bash
git add services/collector/README.md docs/runbooks/run-services.md
git commit -m "docs: update collector product-management docs for the SQLite CLI"
```

---

## Self-Review Notes

- **Spec coverage:** Component 2's `products` table (already in Part A's schema), hash-based auth (Task 2), `AuthCache` with TTL + miss-fallback (Task 2), CLI create/list/revoke/rotate/migrate-from-json (Tasks 3, 5), deletion of the file-based mechanism with no dual-mode (Task 4), corruption fail-fast (Task 6) are each covered by a task.
- **Dependency on Part A confirmed:** Task 1 explicitly requires Part A's Task 1 (`packages/odyssey-store` with the `products` table) to already exist.
- **Type consistency check:** `Product` (slug, name, api_key_hash) is defined once in `auth_cache.py` (Task 2) and imported everywhere else that needs it (Task 4's `server.py`, Task 3's `products_db.py` via `CreatedProduct`) — no second, drifting definition.
- **Out of scope, confirmed against the spec:** real-time cross-process cache invalidation on revoke (accepted staleness window is the TTL); any admin HTTP API (CLI only, per the user's explicit choice during brainstorming).
