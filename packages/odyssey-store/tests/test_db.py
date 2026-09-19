from __future__ import annotations

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


def test_a_database_that_predates_a_column_gains_it(tmp_path):
    """The index is rebuildable, but `products` in the same file is not, so an
    older database is migrated rather than replaced."""
    import sqlite3

    from odyssey_store.schema import ADDED_COLUMNS

    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    # The table as it shipped, before schema 2.1 had anything to index.
    old.execute(
        "CREATE TABLE journeys ("
        "journey_id TEXT PRIMARY KEY, product_slug TEXT, project TEXT, "
        "date TEXT NOT NULL, complete INTEGER NOT NULL, incomplete_reason TEXT, "
        "num_steps INTEGER, aggregated_reward REAL, num_tool_calls INTEGER, "
        "num_tool_failures INTEGER, tool_error_rate REAL, "
        "source_path TEXT NOT NULL, source_mtime_ns INTEGER NOT NULL, "
        "indexed_at TEXT NOT NULL)"
    )
    old.execute(
        "INSERT INTO journeys (journey_id, date, complete, source_path, "
        "source_mtime_ns, indexed_at) "
        "VALUES ('j1', '2026-09-12', 1, '/tmp/j1.jsonl', 1, 'now')"
    )
    old.commit()
    old.close()

    conn = connect(f"sqlite:///{path}")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(journeys)")}
    assert {c for _t, c, _d in ADDED_COLUMNS} <= columns
    assert conn.execute("SELECT journey_id FROM journeys").fetchone()[0] == "j1"

    # Applying it twice is the normal case: every connection runs it.
    connect(f"sqlite:///{path}").close()
    conn.close()
