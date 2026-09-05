# services/collector/tests/test_cli.py
from __future__ import annotations

import json

import pytest
from odyssey_store.auth import hash_api_key
from odyssey_store.db import connect

from odyssey_collector.server import main


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    """See test_server.py's fixture of the same name -- a developer's real
    ``$ODYSSEY_DB_URI`` (or the other collector env vars) must never leak
    into a CLI test that constructs its own isolated tmp_path database."""
    monkeypatch.delenv("ODYSSEY_DB_URI", raising=False)
    monkeypatch.delenv("ODYSSEY_COLLECTOR_API_KEY", raising=False)
    monkeypatch.delenv("ODYSSEY_COLLECTOR_AUTH_CACHE_TTL_SECONDS", raising=False)


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


def test_api_key_and_db_uri_together_is_a_clean_error_not_a_traceback(tmp_path, capsys):
    db_uri = f"sqlite:///{tmp_path}/db.sqlite3"

    code = main(["--api-key", "sk-x", "--db-uri", db_uri])

    assert code == 1
    err = capsys.readouterr().err
    assert "api_key" in err and "db_uri" in err
    assert "Traceback" not in err
