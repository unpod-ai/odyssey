from __future__ import annotations

import hashlib

from odyssey_store.db import connect

from odyssey_api.index.exports_indexer import index_exports


def test_index_exports_computes_hash_once(tmp_path):
    exports_dir = tmp_path / "exports"
    exports_dir.mkdir()
    shard = exports_dir / "sft.jsonl"
    shard.write_text('{"messages": []}\n{"messages": []}\n')
    conn = connect(f"sqlite:///{tmp_path}/db.sqlite3")

    first = index_exports(conn, exports_dir)
    second = index_exports(conn, exports_dir)

    assert first == 1
    assert second == 0  # unchanged, not rehashed
    row = conn.execute("SELECT * FROM exports WHERE name = 'sft.jsonl'").fetchone()
    assert row["rows"] == 2
    expected_hash = hashlib.sha256(shard.read_bytes()).hexdigest()
    assert row["sha256"] == expected_hash


def test_index_exports_rehashes_on_change(tmp_path):
    exports_dir = tmp_path / "exports"
    exports_dir.mkdir()
    shard = exports_dir / "sft.jsonl"
    shard.write_text('{"messages": []}\n')
    conn = connect(f"sqlite:///{tmp_path}/db.sqlite3")
    index_exports(conn, exports_dir)

    shard.write_text('{"messages": []}\n{"messages": []}\n')
    count = index_exports(conn, exports_dir)

    assert count == 1
    row = conn.execute("SELECT rows FROM exports WHERE name = 'sft.jsonl'").fetchone()
    assert row["rows"] == 2
