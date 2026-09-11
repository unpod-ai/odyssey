"""Metrics use-case — lists `services/collector`'s `POST /metrics`-written
host telemetry snapshots. No metrics registry exists anywhere in this repo
(same shape as exports), so this is a directory listing, not a registry
read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from odyssey_api.index.manager import IndexHandle
from odyssey_api.repositories import filesystem

__all__ = ["list_metrics", "list_metrics_indexed"]

_COLUMNS = [
    "ts",
    "hostname",
    "os",
    "cpu_count",
    "memory_total_bytes",
    "memory_available_bytes",
    "disk_total_bytes",
    "disk_free_bytes",
    "project",
    "public_ip",
]


def list_metrics(
    journeys_dir: Path, product_slug: Optional[str] = None
) -> List[Dict[str, Any]]:
    return filesystem.list_metrics(journeys_dir, product_slug)


def list_metrics_indexed(
    index: IndexHandle, product_slug: Optional[str]
) -> List[Dict[str, Any]]:
    """Index-backed replacement for `list_metrics`: reads snapshot fields
    straight out of the `metrics_snapshots` table (populated at index time)
    instead of re-walking `journeys_dir`/`metrics/*.jsonl` on every request."""
    sql = f"SELECT {', '.join(_COLUMNS)} FROM metrics_snapshots WHERE 1=1"
    params: list = []
    if product_slug is not None:
        sql += " AND product_slug = ?"
        params.append(product_slug)
    sql += " ORDER BY ts DESC"
    rows = index.query(sql, tuple(params))
    out = []
    for row in rows:
        snapshot = dict(row)
        # `ts`/`hostname` are NOT NULL columns; the indexer (metrics_indexer.py)
        # inserts "" for a snapshot that arrived without one rather than
        # violating that constraint. Drop the key here so the field is
        # genuinely *missing* to `MetricsSnapshotOut`, matching the
        # filesystem-backed behavior where an absent key -- not an empty
        # string -- is what makes it a required-field ValidationError.
        if snapshot.get("ts") == "":
            del snapshot["ts"]
        if snapshot.get("hostname") == "":
            del snapshot["hostname"]
        out.append(snapshot)
    return out
