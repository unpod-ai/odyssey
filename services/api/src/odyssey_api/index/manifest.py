"""The indexed_files manifest -- what the indexer has already seen, and
where it left off. See odyssey_store.schema for the table definition.
"""

from __future__ import annotations

import sqlite3
from typing import Optional, Tuple


def get_file_state(
    conn: sqlite3.Connection, path: str
) -> Optional[Tuple[int, int, int]]:
    """``(mtime_ns, size_bytes, byte_offset)`` for a previously-indexed
    path, or ``None`` if it has never been indexed."""
    row = conn.execute(
        "SELECT mtime_ns, size_bytes, byte_offset FROM indexed_files WHERE path = ?",
        (path,),
    ).fetchone()
    if row is None:
        return None
    return (row["mtime_ns"], row["size_bytes"], row["byte_offset"])


def upsert_file_state(
    conn: sqlite3.Connection,
    path: str,
    kind: str,
    mtime_ns: int,
    size_bytes: int,
    byte_offset: int,
    indexed_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO indexed_files (path, kind, mtime_ns, size_bytes, byte_offset, indexed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            mtime_ns = excluded.mtime_ns,
            size_bytes = excluded.size_bytes,
            byte_offset = excluded.byte_offset,
            indexed_at = excluded.indexed_at
        """,
        (path, kind, mtime_ns, size_bytes, byte_offset, indexed_at),
    )
