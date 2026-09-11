"""Incrementally indexes journey shards into the `journeys` table.

Walks the same flat (`<journeys_dir>/<date>/<id>.jsonl`) and
product-scoped (`<journeys_dir>/<slug>/<date>/<id>.jsonl`) layouts
`repositories/filesystem.py` already knows about, but -- unlike
`filesystem.list_journeys` -- tags each journey with the product slug
it came from, which the fact table needs and the pooled listing
function doesn't provide.

A shard is only re-read (re-folded) if its (mtime, size) changed since
last indexed -- this is what turns "fold every journey, every request"
into "fold only what changed since the last pass".
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from sqlite3 import Connection
from typing import Optional

from odyssey.export import ExportError
from odyssey.fold import fold
from odyssey.jsonl import MalformedHeaderError, SchemaVersionError, read_events

from odyssey_api.index.manifest import get_file_state, upsert_file_state
from odyssey_api.repositories.filesystem import is_date_dir

logger = logging.getLogger("odyssey_api.index")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iter_shards(journeys_dir: Path):
    """Yields ``(shard_path, date, product_slug)`` for every journey
    shard under ``journeys_dir``, in either layout. ``product_slug`` is
    ``None`` in the flat layout."""
    if not journeys_dir.is_dir():
        return
    for entry in sorted(p for p in journeys_dir.iterdir() if p.is_dir()):
        if is_date_dir(entry):
            for shard in sorted(entry.glob("*.jsonl")):
                yield shard, entry.name, None
            continue
        if entry.name == "metrics":
            continue
        # Product-scoped: entry is a product-slug directory.
        for date_dir in sorted(p for p in entry.iterdir() if is_date_dir(p)):
            for shard in sorted(date_dir.glob("*.jsonl")):
                yield shard, date_dir.name, entry.name


def _index_one_shard(
    conn: Connection, shard: Path, date: str, product_slug: Optional[str]
) -> bool:
    stat = shard.stat()
    state = get_file_state(conn, str(shard))
    if state is not None and state[0] == stat.st_mtime_ns and state[1] == stat.st_size:
        return False  # unchanged since last index

    try:
        result = read_events(shard)
        if not result.events:
            raise ExportError(f"{shard}: no events to fold")
        header = result.header
        project = (
            (header.journey_metadata or {}).get("project")
            if header.journey_metadata
            else None
        )
        fold_result = fold(
            result.events,
            data_source=header.data_source or "unknown",
            conversation_id=header.journey_id,
            trace_id=header.trace_id,
            start_time=header.started_at,
        )
    except (MalformedHeaderError, SchemaVersionError, ExportError, ValueError) as exc:
        logger.warning("skipping malformed journey shard %s: %s", shard, exc)
        return False

    metrics = fold_result.journey.metrics
    now = _now()
    conn.execute(
        """
        INSERT INTO journeys (
            journey_id, product_slug, project, date, complete, incomplete_reason,
            num_steps, aggregated_reward, num_tool_calls, num_tool_failures,
            tool_error_rate, source_path, source_mtime_ns, indexed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(journey_id) DO UPDATE SET
            product_slug = excluded.product_slug,
            project = excluded.project,
            date = excluded.date,
            complete = excluded.complete,
            incomplete_reason = excluded.incomplete_reason,
            num_steps = excluded.num_steps,
            aggregated_reward = excluded.aggregated_reward,
            num_tool_calls = excluded.num_tool_calls,
            num_tool_failures = excluded.num_tool_failures,
            tool_error_rate = excluded.tool_error_rate,
            source_path = excluded.source_path,
            source_mtime_ns = excluded.source_mtime_ns,
            indexed_at = excluded.indexed_at
        """,
        (
            fold_result.journey_id,
            product_slug,
            project,
            date,
            1 if fold_result.complete else 0,
            fold_result.incomplete_reason,
            metrics.steps if metrics else None,
            metrics.aggregated_reward if metrics else None,
            metrics.num_tool_calls if metrics else None,
            metrics.num_tool_failures if metrics else None,
            metrics.tool_error_rate if metrics else None,
            str(shard),
            stat.st_mtime_ns,
            now,
        ),
    )
    upsert_file_state(
        conn, str(shard), "journey", stat.st_mtime_ns, stat.st_size, 0, now
    )
    return True


def index_journeys(conn: Connection, journeys_dir: Path) -> int:
    count = 0
    for shard, date, product_slug in _iter_shards(journeys_dir):
        if _index_one_shard(conn, shard, date, product_slug):
            count += 1
    conn.commit()
    return count
