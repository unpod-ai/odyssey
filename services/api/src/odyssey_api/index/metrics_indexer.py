"""Tails metrics shards (append-only NDJSON a probe keeps writing to all
day) into `metrics_snapshots`, reading only bytes appended since the
last pass -- see `indexed_files.byte_offset`.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from sqlite3 import Connection

from odyssey_api.index.manifest import get_file_state, upsert_file_state
from odyssey_api.repositories.filesystem import is_date_dir

logger = logging.getLogger("odyssey_api.index")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iter_metrics_dirs(journeys_dir: Path):
    """Yields ``(metrics_dir, product_slug)`` -- the flat layout's own
    ``metrics/`` plus one per product-slug directory."""
    if not journeys_dir.is_dir():
        return
    yield journeys_dir / "metrics", None
    for entry in journeys_dir.iterdir():
        if entry.is_dir() and not is_date_dir(entry) and entry.name != "metrics":
            yield entry / "metrics", entry.name


def _tail_shard(conn: Connection, shard: Path, product_slug) -> int:
    stat = shard.stat()
    state = get_file_state(conn, str(shard))
    start_offset = state[2] if state is not None else 0
    if state is not None and stat.st_size == state[1] and stat.st_mtime_ns == state[0]:
        return 0  # unchanged

    inserted = 0
    with open(shard, "rb") as f:
        f.seek(start_offset)
        consumed_offset = start_offset
        for raw_line in f:
            if not raw_line.endswith(b"\n"):
                break  # partial line at EOF -- leave it for next pass
            consumed_offset += len(raw_line)
            line = raw_line.strip()
            if not line:
                continue
            try:
                snapshot = json.loads(line)
                if not isinstance(snapshot, dict):
                    raise AttributeError("metrics line did not decode to an object")
            except (json.JSONDecodeError, AttributeError):
                logger.warning("skipping malformed metrics line in %s", shard)
                continue
            now = _now()
            conn.execute(
                """
                INSERT INTO metrics_snapshots (
                    product_slug, ts, hostname, os, cpu_count, memory_total_bytes,
                    memory_available_bytes, disk_total_bytes, disk_free_bytes,
                    project, public_ip, source_path, indexed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    product_slug,
                    snapshot.get("ts", ""),
                    snapshot.get("hostname", ""),
                    snapshot.get("os"),
                    snapshot.get("cpu_count"),
                    snapshot.get("memory_total_bytes"),
                    snapshot.get("memory_available_bytes"),
                    snapshot.get("disk_total_bytes"),
                    snapshot.get("disk_free_bytes"),
                    snapshot.get("project"),
                    snapshot.get("public_ip"),
                    str(shard),
                    now,
                ),
            )
            inserted += 1

    upsert_file_state(
        conn,
        str(shard),
        "metrics",
        stat.st_mtime_ns,
        stat.st_size,
        consumed_offset,
        _now(),
    )
    return inserted


def index_metrics(conn: Connection, journeys_dir: Path) -> int:
    count = 0
    for metrics_dir, product_slug in _iter_metrics_dirs(journeys_dir):
        if not metrics_dir.is_dir():
            continue
        for shard in sorted(metrics_dir.glob("*.jsonl")):
            count += _tail_shard(conn, shard, product_slug)
    conn.commit()
    return count
