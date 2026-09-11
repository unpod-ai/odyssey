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
    cache = AuthCache(
        db_uri, ttl_seconds=3600
    )  # long TTL: cache won't refresh on its own
    _seed(
        db_uri, "newco", "New Co", "sk-newco"
    )  # created after the cache's initial load

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
