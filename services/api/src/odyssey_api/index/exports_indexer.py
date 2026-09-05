"""Indexes export shards, hashing each one only when its (mtime, size)
changes -- export files are write-once, so in practice this hashes each
shard exactly once, ever, instead of on every /exports request.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from sqlite3 import Connection

from odyssey_api.index.manifest import get_file_state, upsert_file_state


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def index_exports(conn: Connection, exports_dir: Path) -> int:
    if not exports_dir.is_dir():
        return 0
    count = 0
    for shard in sorted(exports_dir.glob("*.jsonl")):
        stat = shard.stat()
        state = get_file_state(conn, str(shard))
        if state is not None and state[0] == stat.st_mtime_ns and state[1] == stat.st_size:
            continue

        h = hashlib.sha256()
        rows = 0
        with open(shard, "rb") as f:
            for line in f:
                if line.strip():
                    rows += 1
                h.update(line)

        now = _now()
        conn.execute(
            """
            INSERT INTO exports (path, name, rows, sha256, mtime_ns, indexed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                name = excluded.name, rows = excluded.rows, sha256 = excluded.sha256,
                mtime_ns = excluded.mtime_ns, indexed_at = excluded.indexed_at
            """,
            (str(shard), shard.name, rows, h.hexdigest(), stat.st_mtime_ns, now),
        )
        upsert_file_state(conn, str(shard), "export", stat.st_mtime_ns, stat.st_size, 0, now)
        count += 1
    conn.commit()
    return count
