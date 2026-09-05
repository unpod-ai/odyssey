from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from odyssey_store.db import CorruptDatabaseError, connect, parse_sqlite_uri


def test_parse_sqlite_uri_relative(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert parse_sqlite_uri("sqlite:///odyssey.sqlite3") == Path("odyssey.sqlite3")


def test_parse_sqlite_uri_absolute(tmp_path):
    uri = f"sqlite:///{tmp_path}/odyssey.sqlite3".replace(
        "///" + str(tmp_path), "////" + str(tmp_path).lstrip("/")
    )
    assert (
        parse_sqlite_uri(f"sqlite:////{str(tmp_path).lstrip('/')}/odyssey.sqlite3")
        == tmp_path / "odyssey.sqlite3"
    )


def test_connect_applies_schema_and_wal(tmp_path):
    uri = f"sqlite:///{tmp_path}/odyssey.sqlite3"
    conn = connect(uri)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {
            "indexed_files",
            "products",
            "journeys",
            "metrics_snapshots",
            "exports",
        } <= tables
    finally:
        conn.close()


def test_connect_twice_is_idempotent(tmp_path):
    uri = f"sqlite:///{tmp_path}/odyssey.sqlite3"
    connect(uri).close()
    # Applying the schema a second time against the same file must not raise.
    conn = connect(uri)
    conn.close()


def test_connect_raises_a_clear_error_on_a_corrupt_file(tmp_path):
    bad = tmp_path / "odyssey.sqlite3"
    bad.write_bytes(b"this is not a sqlite file at all, just garbage bytes")

    with pytest.raises(CorruptDatabaseError, match=str(bad)):
        connect(f"sqlite:///{bad}")
