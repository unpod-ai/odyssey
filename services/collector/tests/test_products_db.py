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
    row = conn.execute(
        "SELECT api_key_hash FROM products WHERE slug = 'acme'"
    ).fetchone()
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

    assert products == [
        {
            "slug": "acme",
            "name": "Acme Corp",
            "revoked": False,
            "created_at": products[0]["created_at"],
        }
    ]
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
    row = conn.execute(
        "SELECT api_key_hash FROM products WHERE slug = 'acme'"
    ).fetchone()
    conn.close()
    assert row["api_key_hash"] == hash_api_key(rotated.api_key)
    assert row["api_key_hash"] != hash_api_key(created.api_key)


def test_migrate_products_from_json_preserves_existing_keys(tmp_path):
    json_path = tmp_path / "products.json"
    json_path.write_text(
        json.dumps(
            {
                "products": [
                    {
                        "slug": "acme",
                        "name": "Acme Corp",
                        "api_key": "sk-acme-original",
                    },
                    {
                        "slug": "globex",
                        "name": "Globex Inc",
                        "api_key": "sk-globex-original",
                    },
                ]
            }
        )
    )
    db_uri = f"sqlite:///{tmp_path}/db.sqlite3"

    count = migrate_products_from_json(db_uri, json_path)

    assert count == 2
    conn = connect(db_uri)
    row = conn.execute(
        "SELECT api_key_hash FROM products WHERE slug = 'acme'"
    ).fetchone()
    conn.close()
    assert row["api_key_hash"] == hash_api_key("sk-acme-original")


def test_migrate_products_from_json_rejects_a_malformed_entry(tmp_path):
    json_path = tmp_path / "products.json"
    json_path.write_text(
        json.dumps(
            {
                "products": [
                    {
                        "slug": "acme",
                        "name": "Acme Corp",
                        "api_key": "sk-acme-original",
                    },
                    {"slug": "globex", "name": "Globex Inc"},  # missing api_key
                ]
            }
        )
    )
    db_uri = f"sqlite:///{tmp_path}/db.sqlite3"

    with pytest.raises(ValueError, match=r"products\[1\].*slug.*name.*api_key"):
        migrate_products_from_json(db_uri, json_path)

    assert list_products(db_uri) == []


def test_migrate_products_from_json_rejects_duplicate_api_key_within_file(tmp_path):
    json_path = tmp_path / "products.json"
    json_path.write_text(
        json.dumps(
            {
                "products": [
                    {"slug": "acme", "name": "Acme Corp", "api_key": "sk-shared"},
                    {"slug": "globex", "name": "Globex Inc", "api_key": "sk-shared"},
                ]
            }
        )
    )
    db_uri = f"sqlite:///{tmp_path}/db.sqlite3"

    with pytest.raises(
        ValueError, match="the same api_key is registered to two products"
    ):
        migrate_products_from_json(db_uri, json_path)

    assert list_products(db_uri) == []


def test_migrate_products_from_json_rejects_duplicate_api_key_against_existing_product(
    tmp_path,
):
    """A JSON entry whose api_key hashes to the same value already stored
    for a different, existing product trips the api_key_hash unique index
    (a cross-run collision, not a duplicate within the file itself) --
    confirms the sqlite3.IntegrityError -> ValueError translation and that
    nothing new is inserted."""
    db_uri = f"sqlite:///{tmp_path}/db.sqlite3"
    create_product(db_uri, "acme", "Acme Corp")
    conn = connect(db_uri)
    conn.execute(
        "UPDATE products SET api_key_hash = ? WHERE slug = 'acme'",
        (hash_api_key("sk-acme-key"),),
    )
    conn.commit()
    conn.close()

    json_path = tmp_path / "products.json"
    json_path.write_text(
        json.dumps(
            {
                "products": [
                    {"slug": "globex", "name": "Globex Inc", "api_key": "sk-acme-key"},
                ]
            }
        )
    )

    with pytest.raises(
        ValueError, match="the same api_key is registered to two products"
    ):
        migrate_products_from_json(db_uri, json_path)

    products = list_products(db_uri)
    assert [p["slug"] for p in products] == ["acme"]
