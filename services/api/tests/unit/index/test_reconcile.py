from __future__ import annotations

from odyssey_store.db import connect

from odyssey_api.index.reconcile import reconcile


def _seed(conn, path, exists_row=True):
    conn.execute(
        "INSERT INTO indexed_files (path, kind, mtime_ns, size_bytes, byte_offset, indexed_at) "
        "VALUES (?, 'journey', 0, 0, 0, 'x')",
        (path,),
    )
    if exists_row:
        conn.execute(
            "INSERT INTO journeys (journey_id, date, complete, source_path, source_mtime_ns, indexed_at) "
            "VALUES ('j1', '2026-08-28', 1, ?, 0, 'x')",
            (path,),
        )
    conn.commit()


def test_reconcile_drops_rows_for_deleted_files(tmp_path):
    conn = connect(f"sqlite:///{tmp_path}/db.sqlite3")
    gone_path = str(tmp_path / "gone.jsonl")  # never actually created on disk
    _seed(conn, gone_path)

    removed = reconcile(conn)

    assert removed == 1
    assert conn.execute("SELECT COUNT(*) FROM indexed_files").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM journeys").fetchone()[0] == 0


def test_reconcile_keeps_rows_for_existing_files(tmp_path):
    conn = connect(f"sqlite:///{tmp_path}/db.sqlite3")
    still_here = tmp_path / "here.jsonl"
    still_here.write_text("x")
    _seed(conn, str(still_here))

    removed = reconcile(conn)

    assert removed == 0
    assert conn.execute("SELECT COUNT(*) FROM indexed_files").fetchone()[0] == 1
