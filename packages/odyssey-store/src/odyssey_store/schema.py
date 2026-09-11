"""The one shared schema definition -- see this package's README for why
it lives here rather than in either service. Every statement is
IF NOT EXISTS/idempotent: whichever service starts first applies the
whole schema, the other's later apply is a no-op.
"""

from __future__ import annotations

SCHEMA_STATEMENTS: list[str] = [
    # Bookkeeping: what services/api's indexer has already seen, and
    # where it left off (metrics files are tailed, not fully reparsed).
    """
    CREATE TABLE IF NOT EXISTS indexed_files (
        path        TEXT PRIMARY KEY,
        kind        TEXT NOT NULL,
        mtime_ns    INTEGER NOT NULL,
        size_bytes  INTEGER NOT NULL,
        byte_offset INTEGER NOT NULL DEFAULT 0,
        indexed_at  TEXT NOT NULL
    )
    """,
    # Owned/written by services/collector only (Part B). services/api
    # only ever reads this table.
    """
    CREATE TABLE IF NOT EXISTS products (
        slug          TEXT PRIMARY KEY,
        name          TEXT NOT NULL,
        api_key_hash  TEXT NOT NULL,
        revoked       INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_products_api_key_hash ON products(api_key_hash)",
    """
    CREATE TABLE IF NOT EXISTS journeys (
        journey_id        TEXT PRIMARY KEY,
        product_slug      TEXT,
        project           TEXT,
        date              TEXT NOT NULL,
        complete          INTEGER NOT NULL,
        incomplete_reason TEXT,
        num_steps         INTEGER,
        aggregated_reward REAL,
        num_tool_calls    INTEGER,
        num_tool_failures INTEGER,
        tool_error_rate   REAL,
        source_path       TEXT NOT NULL,
        source_mtime_ns   INTEGER NOT NULL,
        indexed_at        TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_journeys_product_date ON journeys(product_slug, date)",
    "CREATE INDEX IF NOT EXISTS ix_journeys_product_project ON journeys(product_slug, project)",
    """
    CREATE TABLE IF NOT EXISTS metrics_snapshots (
        id                      INTEGER PRIMARY KEY,
        product_slug            TEXT,
        ts                      TEXT NOT NULL,
        hostname                TEXT NOT NULL,
        os                      TEXT,
        cpu_count               INTEGER,
        memory_total_bytes      INTEGER,
        memory_available_bytes  INTEGER,
        disk_total_bytes        INTEGER,
        disk_free_bytes         INTEGER,
        project                 TEXT,
        public_ip               TEXT,
        source_path             TEXT NOT NULL,
        indexed_at              TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_metrics_product_project ON metrics_snapshots(product_slug, project)",
    "CREATE INDEX IF NOT EXISTS ix_metrics_hostname_ts ON metrics_snapshots(hostname, ts)",
    """
    CREATE TABLE IF NOT EXISTS exports (
        path        TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        rows        INTEGER NOT NULL,
        sha256      TEXT NOT NULL,
        mtime_ns    INTEGER NOT NULL,
        indexed_at  TEXT NOT NULL
    )
    """,
]
