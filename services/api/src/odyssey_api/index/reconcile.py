"""Drops index rows for files that have vanished from disk since they
were indexed -- services/collector's prune.py deletes old date
directories independently of services/api, so the index needs its own
pass to notice. Run on a slower cadence than the incremental indexing
passes (see index/worker.py) -- stat-ing every already-known path on
every pass would defeat the point of incremental indexing.
"""

from __future__ import annotations

import os
from sqlite3 import Connection


def reconcile(conn: Connection) -> int:
    paths = [row["path"] for row in conn.execute("SELECT path FROM indexed_files").fetchall()]
    removed = 0
    for path in paths:
        if os.path.exists(path):
            continue
        conn.execute("DELETE FROM indexed_files WHERE path = ?", (path,))
        conn.execute("DELETE FROM journeys WHERE source_path = ?", (path,))
        conn.execute("DELETE FROM metrics_snapshots WHERE source_path = ?", (path,))
        conn.execute("DELETE FROM exports WHERE path = ?", (path,))
        removed += 1
    conn.commit()
    return removed
