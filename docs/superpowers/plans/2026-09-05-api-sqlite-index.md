# services/api SQLite Read Index — Implementation Plan (Part A)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `services/api`'s per-request filesystem scans (the O(n²)-shaped `/journeys` read path, unbounded `/metrics` scans, repeated `/exports` re-hashing) with a self-maintained SQLite index, and add product/project/date journey and metrics counts.

**Architecture:** A new shared `packages/odyssey-store` package owns the SQLite schema (all 6 tables, including the `products` table Part B's collector CLI will later write). `services/api` gets a new `odyssey_api.index` subpackage: an incremental indexer that walks the same JSONL/export files it reads today but only reprocesses new/changed ones (tracked via an `indexed_files` manifest table keyed by `(path, mtime, size)`), a background thread that reindexes every few seconds, and routers that query SQLite instead of the filesystem.

**Tech Stack:** Python stdlib `sqlite3` (no new dependency — matches this repo's "stdlib where possible" discipline), FastAPI, pytest, real-temp-directory fixtures (no mocks, per this repo's existing test convention).

**Spec:** `docs/superpowers/specs/2026-09-05-api-sqlite-index-design.md`

## Global Constraints

- No new runtime dependency for SQLite access — `sqlite3` is stdlib.
- Every list-endpoint response shape (`JourneyPageOut`, `MetricsPageOut`, `ExportPageOut`) stays exactly as it is today — this plan changes the implementation underneath, never the wire contract, so the existing SDK/dashboard code from prior sessions keeps working unmodified except where a task explicitly says otherwise.
- `indexed_at`/all timestamps are ISO-8601 UTC text (`datetime.now(timezone.utc).isoformat()`).
- WAL mode + `busy_timeout` pragma on every connection this plan opens.
- `repositories/filesystem.py`'s existing functions are reused by the indexer, not duplicated — only add to that file when a helper it doesn't yet have is needed (Task 2).
- Malformed/unparseable data (a bad JSON line, an unfoldable shard) is skipped and logged, never raised — matches this repo's existing defensiveness (see `filesystem.list_metrics`'s malformed-line handling).

---

### Task 1: `packages/odyssey-store` — schema + connection helper

**Files:**
- Create: `packages/odyssey-store/pyproject.toml`
- Create: `packages/odyssey-store/src/odyssey_store/__init__.py`
- Create: `packages/odyssey-store/src/odyssey_store/schema.py`
- Create: `packages/odyssey-store/src/odyssey_store/db.py`
- Create: `packages/odyssey-store/README.md`
- Test: `packages/odyssey-store/tests/test_db.py`
- Modify: `pyproject.toml:9` (add `"packages/odyssey-store"` to `[tool.uv.workspace].members`)

**Interfaces:**
- Produces: `odyssey_store.db.connect(uri: str) -> sqlite3.Connection` (WAL mode, `busy_timeout=5000`, `row_factory=sqlite3.Row`, schema applied). `odyssey_store.db.parse_sqlite_uri(uri: str) -> Path` (parses `sqlite:///relative/path` and `sqlite:////absolute/path`). `odyssey_store.schema.SCHEMA_STATEMENTS: list[str]` (every `CREATE TABLE IF NOT EXISTS`/`CREATE INDEX IF NOT EXISTS` statement, applied in order).

- [ ] **Step 1: Write the failing test**

```python
# packages/odyssey-store/tests/test_db.py
from __future__ import annotations

import sqlite3
from pathlib import Path

from odyssey_store.db import connect, parse_sqlite_uri


def test_parse_sqlite_uri_relative(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert parse_sqlite_uri("sqlite:///odyssey.sqlite3") == Path("odyssey.sqlite3")


def test_parse_sqlite_uri_absolute(tmp_path):
    uri = f"sqlite:///{tmp_path}/odyssey.sqlite3".replace("///" + str(tmp_path), "////" + str(tmp_path).lstrip("/"))
    assert parse_sqlite_uri(f"sqlite:////{str(tmp_path).lstrip('/')}/odyssey.sqlite3") == tmp_path / "odyssey.sqlite3"


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd packages/odyssey-store && uv run pytest tests/test_db.py -v` (package doesn't exist yet)
Expected: FAIL with `ModuleNotFoundError: No module named 'odyssey_store'`

- [ ] **Step 3: Write the package scaffolding**

```toml
# packages/odyssey-store/pyproject.toml
[project]
name = "odyssey-store"
version = "0.1.0"
description = "Shared SQLite schema/connection helper for services/api's read index and services/collector's product roster — stdlib sqlite3 only, no ORM."
readme = "README.md"
requires-python = ">=3.12,<3.13"
license = { text = "Apache-2.0" }
authors = [{ name = "Unpod", email = "parvinder@unpod.ai" }]

# stdlib sqlite3 only, matching odyssey-core's/odyssey-collector's own
# dependency discipline -- this package is a thin, dependency-free schema
# contract two independently-deployed services import, not a place to pull
# in an ORM.
dependencies = []

[project.optional-dependencies]
dev = [
  "pytest>=8",
  "black==26.3.1",
  "isort>=5.13",
  "flake8>=7.3",
  "pyrefly>=0.1",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/odyssey_store"]

[tool.black]
target-version = ["py312"]

[tool.isort]
profile = "black"

[tool.pytest.ini_options]
testpaths = ["tests"]
```

```markdown
<!-- packages/odyssey-store/README.md -->
# odyssey-store

Shared SQLite schema and connection helper for the one `ODYSSEY_DB_URI`
file `services/api` and `services/collector` both use — `services/api`'s
read index (`journeys`/`metrics_snapshots`/`exports`/`indexed_files`,
disposable/rebuildable) and `services/collector`'s `products` table
(real, unrecoverable tenant credentials, hash-only). Each table has
exactly one writer; see
`docs/superpowers/specs/2026-09-05-api-sqlite-index-design.md`.

This package owns only the DDL and the connection helper — no
business logic, no queries beyond schema application.
```

```python
# packages/odyssey-store/src/odyssey_store/__init__.py
from __future__ import annotations

from odyssey_store.db import connect, parse_sqlite_uri
from odyssey_store.schema import SCHEMA_STATEMENTS

__all__ = ["connect", "parse_sqlite_uri", "SCHEMA_STATEMENTS"]
```

```python
# packages/odyssey-store/src/odyssey_store/schema.py
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
```

```python
# packages/odyssey-store/src/odyssey_store/db.py
"""Connection helper for the one shared ODYSSEY_DB_URI file. Deliberately
opens a fresh connection per call rather than holding one shared
long-lived connection across threads -- sqlite3 connections are not
safe to share across threads without care, and opening a local-file
connection is cheap (tens of microseconds), so "one connection per
caller" sidesteps the whole class of cross-thread sharing bugs.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from odyssey_store.schema import SCHEMA_STATEMENTS

_PREFIX = "sqlite:///"


def parse_sqlite_uri(uri: str) -> Path:
    """``sqlite:///relative/path`` -> ``Path("relative/path")``;
    ``sqlite:////absolute/path`` -> ``Path("/absolute/path")`` -- the
    same three-slash-relative/four-slash-absolute convention SQLAlchemy's
    sqlite URIs use, so it reads familiarly to anyone who has seen a
    ``DATABASE_URL``.
    """
    if not uri.startswith(_PREFIX):
        raise ValueError(f"{uri!r}: expected a sqlite:/// URI")
    rest = uri[len(_PREFIX) :]
    if rest.startswith("/"):
        return Path("/" + rest.lstrip("/"))
    return Path(rest)


def connect(uri: str) -> sqlite3.Connection:
    """Opens (creating the file/parent dirs if needed), applies the
    shared schema (idempotent), and returns a ready-to-query connection
    in WAL mode with a 5s busy timeout."""
    path = parse_sqlite_uri(uri)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)
    conn.commit()
    return conn
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd packages/odyssey-store && uv run pytest tests/test_db.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Register the new workspace member**

Edit `pyproject.toml` line 9:

```toml
members = ["packages/odyssey-core", "packages/odyssey-schemas", "packages/odyssey-store", "services/collector", "services/api", "data_preparation", "cli", "training", "evaluation", "sdk/python"]
```

Run: `uv sync` (from repo root) — must complete without error.

- [ ] **Step 6: Commit**

```bash
git add packages/odyssey-store pyproject.toml uv.lock
git commit -m "feat(store): add odyssey-store package with shared SQLite schema"
```

---

### Task 2: Promote `filesystem._is_date_dir` to a public helper

The indexer (Task 4) needs to walk journey/metrics directories the same way `repositories/filesystem.py` already does, while also tracking *which product* each entry belongs to — something `filesystem.list_journeys` doesn't expose (it pools every product into one flat list). Rather than duplicate the date-partition-detection logic, promote the existing private helper to public.

**Files:**
- Modify: `services/api/src/odyssey_api/repositories/filesystem.py`
- Test: `services/api/tests/unit/test_filesystem.py` (create if it doesn't exist; check first)

**Interfaces:**
- Produces: `is_date_dir(path: Path) -> bool` (same behavior as today's `_is_date_dir`, just public and exported).

- [ ] **Step 1: Check for an existing filesystem unit test file**

Run: `ls services/api/tests/unit/`
If `test_filesystem.py` doesn't exist, this task creates it.

- [ ] **Step 2: Write the failing test**

```python
# services/api/tests/unit/test_filesystem.py
from __future__ import annotations

from odyssey_api.repositories.filesystem import is_date_dir


def test_is_date_dir_true_for_iso_date(tmp_path):
    d = tmp_path / "2026-08-28"
    d.mkdir()
    assert is_date_dir(d) is True


def test_is_date_dir_false_for_non_date_name(tmp_path):
    d = tmp_path / "metrics"
    d.mkdir()
    assert is_date_dir(d) is False


def test_is_date_dir_false_for_file(tmp_path):
    f = tmp_path / "2026-08-28"
    f.write_text("not a directory")
    assert is_date_dir(f) is False
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd services/api && uv run pytest tests/unit/test_filesystem.py -v`
Expected: FAIL with `ImportError: cannot import name 'is_date_dir'`

- [ ] **Step 4: Rename in the source file**

In `services/api/src/odyssey_api/repositories/filesystem.py`:
1. Add `"is_date_dir"` to `__all__`.
2. Rename the function `_is_date_dir` to `is_date_dir` (its definition, currently right after the imports).
3. Update its three call sites in the same file (`_list_journeys_flat`, `list_journeys`, `list_metrics`) from `_is_date_dir(...)` to `is_date_dir(...)`.

- [ ] **Step 5: Run test to verify it passes, and that nothing else broke**

Run: `cd services/api && uv run pytest -q`
Expected: PASS, same total count as before plus the 3 new tests (no regressions — this is a pure rename)

- [ ] **Step 6: Commit**

```bash
git add services/api/src/odyssey_api/repositories/filesystem.py services/api/tests/unit/test_filesystem.py
git commit -m "refactor(api): promote is_date_dir to a public helper for the indexer"
```

---

### Task 3: `ODYSSEY_DB_URI` setting + indexer config

**Files:**
- Modify: `services/api/src/odyssey_api/settings.py`
- Modify: `services/api/pyproject.toml` (add `odyssey-store` dependency)
- Test: `services/api/tests/unit/test_settings.py` (create if absent)

**Interfaces:**
- Produces: `Settings.db_uri: str` (default `"sqlite:///./odyssey.sqlite3"`, overridden by `ODYSSEY_DB_URI`). `Settings.index_interval_seconds: int` (default `5`, env `ODYSSEY_API_INDEX_INTERVAL_SECONDS`). `Settings.index_reconcile_every: int` (default `20`, env `ODYSSEY_API_INDEX_RECONCILE_EVERY`).

- [ ] **Step 1: Write the failing test**

```python
# services/api/tests/unit/test_settings.py
from __future__ import annotations

from odyssey_api.settings import Settings


def test_db_uri_default():
    assert Settings().db_uri == "sqlite:///./odyssey.sqlite3"


def test_db_uri_from_env(monkeypatch):
    monkeypatch.setenv("ODYSSEY_DB_URI", "sqlite:///tmp/other.sqlite3")
    assert Settings().db_uri == "sqlite:///tmp/other.sqlite3"


def test_index_interval_default():
    assert Settings().index_interval_seconds == 5


def test_index_reconcile_every_default():
    assert Settings().index_reconcile_every == 20
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/api && uv run pytest tests/unit/test_settings.py -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'db_uri'`

- [ ] **Step 3: Add the fields**

In `services/api/src/odyssey_api/settings.py`, add after the `api_key` field:

```python
    db_uri: str = field(
        default_factory=lambda: os.environ.get(
            "ODYSSEY_DB_URI", "sqlite:///./odyssey.sqlite3"
        )
    )
    index_interval_seconds: int = field(
        default_factory=lambda: int(
            os.environ.get("ODYSSEY_API_INDEX_INTERVAL_SECONDS", "5")
        )
    )
    index_reconcile_every: int = field(
        default_factory=lambda: int(
            os.environ.get("ODYSSEY_API_INDEX_RECONCILE_EVERY", "20")
        )
    )
```

Add `odyssey-store` to `services/api/pyproject.toml`'s `dependencies` list and to `[tool.uv.sources]` (`odyssey-store = { workspace = true }`), matching the existing `odyssey-schemas` entries.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv sync && cd services/api && uv run pytest tests/unit/test_settings.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add services/api/src/odyssey_api/settings.py services/api/pyproject.toml services/api/tests/unit/test_settings.py uv.lock
git commit -m "feat(api): add ODYSSEY_DB_URI and index interval settings"
```

---

### Task 4: Journey indexing

**Files:**
- Create: `services/api/src/odyssey_api/index/__init__.py`
- Create: `services/api/src/odyssey_api/index/manifest.py`
- Create: `services/api/src/odyssey_api/index/journeys_indexer.py`
- Test: `services/api/tests/unit/index/test_journeys_indexer.py`

**Interfaces:**
- Consumes: `odyssey_store.db.connect(uri) -> sqlite3.Connection` (Task 1). `odyssey_api.repositories.filesystem.is_date_dir(path) -> bool` (Task 2). `odyssey.jsonl.read_events(path)` (existing, returns an object with `.header` and `.events`). `odyssey.fold.fold(events, **kwargs) -> FoldResult` (existing).
- Produces: `manifest.get_file_state(conn, path: str) -> tuple[int, int, int] | None` (mtime_ns, size_bytes, byte_offset, or `None` if never indexed). `manifest.upsert_file_state(conn, path: str, kind: str, mtime_ns: int, size_bytes: int, byte_offset: int, indexed_at: str) -> None`. `journeys_indexer.index_journeys(conn, journeys_dir: Path) -> int` (returns count of journeys (re)indexed this pass).

- [ ] **Step 1: Write the failing test**

```python
# services/api/tests/unit/index/test_journeys_indexer.py
from __future__ import annotations

from odyssey.jsonl import write_events
from odyssey.primitives import JourneyEvent, JourneyHeader, Message, Terminal
from odyssey_store.db import connect

from odyssey_api.index.journeys_indexer import index_journeys

JID = "j_idx"


def _write_journey(journeys_dir, jid, date, project=None, complete=True):
    date_dir = journeys_dir / date
    date_dir.mkdir(parents=True, exist_ok=True)
    header = JourneyHeader(
        journey_id=jid,
        data_source="livekit",
        journey_metadata={"project": project} if project else None,
    )
    events = [
        JourneyEvent(
            journey_id=jid, seq=0, kind="message", event_id="e0",
            message=Message(role="user", content="hi"),
        )
    ]
    if complete:
        events.append(
            JourneyEvent(
                journey_id=jid, seq=1, kind="terminal", event_id="e1",
                terminal=Terminal(termination_reason="ENV_DONE"),
            )
        )
    write_events(date_dir / f"{jid}.jsonl", events, header=header)


def test_index_journeys_inserts_row(tmp_path):
    journeys_dir = tmp_path / "journeys"
    _write_journey(journeys_dir, JID, "2026-08-28", project="odyssey")
    conn = connect(f"sqlite:///{tmp_path}/db.sqlite3")

    count = index_journeys(conn, journeys_dir)

    assert count == 1
    row = conn.execute("SELECT * FROM journeys WHERE journey_id = ?", (JID,)).fetchone()
    assert row["date"] == "2026-08-28"
    assert row["complete"] == 1
    assert row["project"] == "odyssey"
    assert row["product_slug"] is None


def test_index_journeys_skips_unchanged_file_on_second_pass(tmp_path):
    journeys_dir = tmp_path / "journeys"
    _write_journey(journeys_dir, JID, "2026-08-28")
    conn = connect(f"sqlite:///{tmp_path}/db.sqlite3")

    first = index_journeys(conn, journeys_dir)
    second = index_journeys(conn, journeys_dir)

    assert first == 1
    assert second == 0  # nothing changed, nothing reprocessed


def test_index_journeys_tags_product_slug_in_scoped_layout(tmp_path):
    journeys_dir = tmp_path / "journeys"
    _write_journey(journeys_dir / "unpod", JID, "2026-08-28")
    conn = connect(f"sqlite:///{tmp_path}/db.sqlite3")

    index_journeys(conn, journeys_dir)

    row = conn.execute("SELECT product_slug FROM journeys WHERE journey_id = ?", (JID,)).fetchone()
    assert row["product_slug"] == "unpod"


def test_index_journeys_skips_malformed_shard(tmp_path, caplog):
    journeys_dir = tmp_path / "journeys"
    date_dir = journeys_dir / "2026-08-28"
    date_dir.mkdir(parents=True)
    (date_dir / "broken.jsonl").write_text("not valid jsonl\n")
    conn = connect(f"sqlite:///{tmp_path}/db.sqlite3")

    count = index_journeys(conn, journeys_dir)

    assert count == 0
    assert conn.execute("SELECT COUNT(*) FROM journeys").fetchone()[0] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/api && uv run pytest tests/unit/index/test_journeys_indexer.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'odyssey_api.index'`

- [ ] **Step 3: Implement the manifest module**

```python
# services/api/src/odyssey_api/index/manifest.py
"""The indexed_files manifest -- what the indexer has already seen, and
where it left off. See odyssey_store.schema for the table definition.
"""

from __future__ import annotations

import sqlite3
from typing import Optional, Tuple


def get_file_state(conn: sqlite3.Connection, path: str) -> Optional[Tuple[int, int, int]]:
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
```

- [ ] **Step 4: Implement the journeys indexer**

```python
# services/api/src/odyssey_api/index/journeys_indexer.py
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


def _index_one_shard(conn: Connection, shard: Path, date: str, product_slug: Optional[str]) -> bool:
    stat = shard.stat()
    state = get_file_state(conn, str(shard))
    if state is not None and state[0] == stat.st_mtime_ns and state[1] == stat.st_size:
        return False  # unchanged since last index

    try:
        result = read_events(shard)
        if not result.events:
            raise ExportError(f"{shard}: no events to fold")
        header = result.header
        project = (header.journey_metadata or {}).get("project") if header.journey_metadata else None
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
    upsert_file_state(conn, str(shard), "journey", stat.st_mtime_ns, stat.st_size, 0, now)
    return True


def index_journeys(conn: Connection, journeys_dir: Path) -> int:
    count = 0
    for shard, date, product_slug in _iter_shards(journeys_dir):
        if _index_one_shard(conn, shard, date, product_slug):
            count += 1
    conn.commit()
    return count
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd services/api && uv run pytest tests/unit/index/test_journeys_indexer.py -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Commit**

```bash
git add services/api/src/odyssey_api/index/__init__.py services/api/src/odyssey_api/index/manifest.py services/api/src/odyssey_api/index/journeys_indexer.py services/api/tests/unit/index/test_journeys_indexer.py
git commit -m "feat(api): incremental journey indexer into SQLite"
```

---

### Task 5: Metrics indexing (append-only tailing)

**Files:**
- Create: `services/api/src/odyssey_api/index/metrics_indexer.py`
- Test: `services/api/tests/unit/index/test_metrics_indexer.py`

**Interfaces:**
- Consumes: `manifest.get_file_state`/`upsert_file_state` (Task 4). `is_date_dir` (Task 2).
- Produces: `metrics_indexer.index_metrics(conn, journeys_dir: Path) -> int` (count of new snapshot rows inserted this pass).

- [ ] **Step 1: Write the failing test**

```python
# services/api/tests/unit/index/test_metrics_indexer.py
from __future__ import annotations

import json

from odyssey_store.db import connect

from odyssey_api.index.metrics_indexer import index_metrics


def _snapshot(ts, hostname, project=None):
    return {"ts": ts, "hostname": hostname, "os": "Linux", "project": project}


def test_index_metrics_inserts_rows(tmp_path):
    metrics_dir = tmp_path / "journeys" / "metrics"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "2026-09-05.jsonl").write_text(
        json.dumps(_snapshot("t1", "h1")) + "\n" + json.dumps(_snapshot("t2", "h2")) + "\n"
    )
    conn = connect(f"sqlite:///{tmp_path}/db.sqlite3")

    count = index_metrics(conn, tmp_path / "journeys")

    assert count == 2
    assert conn.execute("SELECT COUNT(*) FROM metrics_snapshots").fetchone()[0] == 2


def test_index_metrics_tails_appended_lines_only(tmp_path):
    metrics_dir = tmp_path / "journeys" / "metrics"
    metrics_dir.mkdir(parents=True)
    shard = metrics_dir / "2026-09-05.jsonl"
    shard.write_text(json.dumps(_snapshot("t1", "h1")) + "\n")
    conn = connect(f"sqlite:///{tmp_path}/db.sqlite3")
    index_metrics(conn, tmp_path / "journeys")

    with open(shard, "a") as f:
        f.write(json.dumps(_snapshot("t2", "h2")) + "\n")
    second_count = index_metrics(conn, tmp_path / "journeys")

    assert second_count == 1
    assert conn.execute("SELECT COUNT(*) FROM metrics_snapshots").fetchone()[0] == 2


def test_index_metrics_skips_malformed_line(tmp_path):
    metrics_dir = tmp_path / "journeys" / "metrics"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "2026-09-05.jsonl").write_text(
        "not json\n" + json.dumps(_snapshot("t1", "h1")) + "\n"
    )
    conn = connect(f"sqlite:///{tmp_path}/db.sqlite3")

    count = index_metrics(conn, tmp_path / "journeys")

    assert count == 1


def test_index_metrics_tags_product_slug(tmp_path):
    metrics_dir = tmp_path / "journeys" / "unpod" / "metrics"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "2026-09-05.jsonl").write_text(json.dumps(_snapshot("t1", "h1")) + "\n")
    conn = connect(f"sqlite:///{tmp_path}/db.sqlite3")

    index_metrics(conn, tmp_path / "journeys")

    row = conn.execute("SELECT product_slug FROM metrics_snapshots").fetchone()
    assert row["product_slug"] == "unpod"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/api && uv run pytest tests/unit/index/test_metrics_indexer.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
# services/api/src/odyssey_api/index/metrics_indexer.py
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
            except json.JSONDecodeError:
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

    upsert_file_state(conn, str(shard), "metrics", stat.st_mtime_ns, stat.st_size, consumed_offset, _now())
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/api && uv run pytest tests/unit/index/test_metrics_indexer.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add services/api/src/odyssey_api/index/metrics_indexer.py services/api/tests/unit/index/test_metrics_indexer.py
git commit -m "feat(api): tailing metrics indexer into SQLite"
```

---

### Task 6: Exports indexing (hash-once cache)

**Files:**
- Create: `services/api/src/odyssey_api/index/exports_indexer.py`
- Test: `services/api/tests/unit/index/test_exports_indexer.py`

**Interfaces:**
- Produces: `exports_indexer.index_exports(conn, exports_dir: Path) -> int` (count of shards (re)hashed this pass).

- [ ] **Step 1: Write the failing test**

```python
# services/api/tests/unit/index/test_exports_indexer.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/api && uv run pytest tests/unit/index/test_exports_indexer.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
# services/api/src/odyssey_api/index/exports_indexer.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/api && uv run pytest tests/unit/index/test_exports_indexer.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add services/api/src/odyssey_api/index/exports_indexer.py services/api/tests/unit/index/test_exports_indexer.py
git commit -m "feat(api): hash-once exports indexer into SQLite"
```

---

### Task 7: Reconciliation (deletions)

**Files:**
- Create: `services/api/src/odyssey_api/index/reconcile.py`
- Test: `services/api/tests/unit/index/test_reconcile.py`

**Interfaces:**
- Produces: `reconcile.reconcile(conn) -> int` (count of rows removed for files no longer on disk).

- [ ] **Step 1: Write the failing test**

```python
# services/api/tests/unit/index/test_reconcile.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/api && uv run pytest tests/unit/index/test_reconcile.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
# services/api/src/odyssey_api/index/reconcile.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/api && uv run pytest tests/unit/index/test_reconcile.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add services/api/src/odyssey_api/index/reconcile.py services/api/tests/unit/index/test_reconcile.py
git commit -m "feat(api): reconciliation pass drops index rows for deleted files"
```

---

### Task 8: Index manager (lazy per-settings singleton, blocking first pass)

**Files:**
- Create: `services/api/src/odyssey_api/index/manager.py`
- Test: `services/api/tests/unit/index/test_manager.py`

**Interfaces:**
- Consumes: `journeys_indexer.index_journeys`, `metrics_indexer.index_metrics`, `exports_indexer.index_exports`, `reconcile.reconcile` (Tasks 4-7). `odyssey_store.db.connect` (Task 1). `Settings` (Task 3).
- Produces: `manager.get_index(settings: Settings) -> IndexHandle`. `IndexHandle.query(sql: str, params: tuple = ()) -> list[sqlite3.Row]`. `IndexHandle.stop() -> None` (stops the background thread). `manager.reset_for_tests() -> None` (clears the singleton registry — test-only helper).

Note on deviating from the spec's literal "block server startup" wording: `services/api`'s `create_app()` is called once at import time and per-test with settings applied *after* creation via `dependency_overrides` (see `services/api/tests/integration/test_api.py`'s `_client()` helper) — there is no single point where "the real settings" are known at app-creation time. This manager instead blocks on **first access per distinct `db_uri`**, via a FastAPI dependency (Task 9) — functionally identical from a caller's perspective (the first request against a given settings/DB blocks until indexed; every later request is fast), and compatible with the existing test pattern without requiring changes to it.

- [ ] **Step 1: Write the failing test**

```python
# services/api/tests/unit/index/test_manager.py
from __future__ import annotations

import time

from odyssey.jsonl import write_events
from odyssey.primitives import JourneyEvent, JourneyHeader, Message, Terminal

from odyssey_api.index import manager
from odyssey_api.settings import Settings


def _write_journey(journeys_dir, jid):
    date_dir = journeys_dir / "2026-08-28"
    date_dir.mkdir(parents=True)
    write_events(
        date_dir / f"{jid}.jsonl",
        [
            JourneyEvent(journey_id=jid, seq=0, kind="message", event_id="e0", message=Message(role="user", content="hi")),
            JourneyEvent(journey_id=jid, seq=1, kind="terminal", event_id="e1", terminal=Terminal(termination_reason="ENV_DONE")),
        ],
        header=JourneyHeader(journey_id=jid, data_source="livekit"),
    )


def test_get_index_runs_full_pass_before_returning(tmp_path):
    manager.reset_for_tests()
    journeys_dir = tmp_path / "journeys"
    _write_journey(journeys_dir, "j1")
    settings = Settings(journeys_dir=journeys_dir, db_uri=f"sqlite:///{tmp_path}/db.sqlite3", index_interval_seconds=3600)

    handle = manager.get_index(settings)

    rows = handle.query("SELECT journey_id FROM journeys")
    assert [r["journey_id"] for r in rows] == ["j1"]
    handle.stop()


def test_get_index_returns_same_handle_for_same_settings(tmp_path):
    manager.reset_for_tests()
    settings = Settings(journeys_dir=tmp_path / "journeys", db_uri=f"sqlite:///{tmp_path}/db.sqlite3", index_interval_seconds=3600)

    first = manager.get_index(settings)
    second = manager.get_index(settings)

    assert first is second
    first.stop()


def test_background_worker_picks_up_new_journey(tmp_path):
    manager.reset_for_tests()
    journeys_dir = tmp_path / "journeys"
    journeys_dir.mkdir()
    settings = Settings(journeys_dir=journeys_dir, db_uri=f"sqlite:///{tmp_path}/db.sqlite3", index_interval_seconds=1)

    handle = manager.get_index(settings)
    assert handle.query("SELECT COUNT(*) AS n FROM journeys")[0]["n"] == 0

    _write_journey(journeys_dir, "j2")
    time.sleep(2.5)  # give the background thread at least one cycle

    rows = handle.query("SELECT journey_id FROM journeys")
    assert [r["journey_id"] for r in rows] == ["j2"]
    handle.stop()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/api && uv run pytest tests/unit/index/test_manager.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
# services/api/src/odyssey_api/index/manager.py
"""One IndexHandle per distinct ODYSSEY_DB_URI, lazily created on first
access and cached for the process's lifetime. See this module's note in
the implementation plan for why this replaces a literal
"block at server startup" hook -- services/api's create_app()/Settings
override pattern has no single point where the real settings are known
before the first request.
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Dict

from odyssey_store.db import connect

from odyssey_api.index.exports_indexer import index_exports
from odyssey_api.index.journeys_indexer import index_journeys
from odyssey_api.index.metrics_indexer import index_metrics
from odyssey_api.index.reconcile import reconcile
from odyssey_api.settings import Settings


class IndexHandle:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._run_pass(full_reconcile=False)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _run_pass(self, full_reconcile: bool) -> None:
        conn = connect(self._settings.db_uri)
        try:
            with self._lock:
                index_journeys(conn, self._settings.journeys_dir)
                index_metrics(conn, self._settings.journeys_dir)
                index_exports(conn, self._settings.exports_dir)
                if full_reconcile:
                    reconcile(conn)
        finally:
            conn.close()

    def _loop(self) -> None:
        cycles = 0
        while not self._stop_event.wait(self._settings.index_interval_seconds):
            cycles += 1
            full_reconcile = cycles % self._settings.index_reconcile_every == 0
            self._run_pass(full_reconcile=full_reconcile)

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        conn = connect(self._settings.db_uri)
        try:
            with self._lock:
                return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=5)


_registry: Dict[str, IndexHandle] = {}
_registry_lock = threading.Lock()


def get_index(settings: Settings) -> IndexHandle:
    with _registry_lock:
        handle = _registry.get(settings.db_uri)
        if handle is None:
            handle = IndexHandle(settings)
            _registry[settings.db_uri] = handle
        return handle


def reset_for_tests() -> None:
    """Test-only: stops and forgets every cached handle so each test gets
    a fresh index scoped to its own tmp_path settings."""
    with _registry_lock:
        for handle in _registry.values():
            handle.stop()
        _registry.clear()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/api && uv run pytest tests/unit/index/test_manager.py -v`
Expected: PASS (3 tests; the third takes ~2.5s, that's expected)

- [ ] **Step 5: Commit**

```bash
git add services/api/src/odyssey_api/index/manager.py services/api/tests/unit/index/test_manager.py
git commit -m "feat(api): lazy per-settings index manager with background worker"
```

---

### Task 9: FastAPI dependency + `/journeys` reads from the index

**Files:**
- Modify: `services/api/src/odyssey_api/deps.py`
- Modify: `services/api/src/odyssey_api/domain/journeys.py`
- Modify: `services/api/src/odyssey_api/routers/journeys.py`
- Modify: `services/api/tests/integration/test_api.py` (existing journeys tests — add `db_uri` to each test's `Settings(...)` construction so each test gets an isolated tmp_path DB; add `from odyssey_api.index import manager` + `manager.reset_for_tests()` in a fixture)

**Interfaces:**
- Consumes: `manager.get_index` (Task 8).
- Produces: `deps.get_index_dep(settings: Settings = Depends(get_settings_dep)) -> IndexHandle`. `domain.journeys.list_journeys_with_status_indexed(index: IndexHandle, product: str | None, date: str | None) -> list[tuple[str, str, bool]]` (same 3-tuple shape the router already builds `JourneySummaryOut` from, now backed by SQL instead of folding every shard).

- [ ] **Step 1: Write the failing test**

Add to `services/api/tests/integration/test_api.py`, adjusting the existing `_client` helper and one test:

```python
# At the top of test_api.py, add:
import uuid
from odyssey_api.index import manager

# Replace the existing `_client` helper with one that gives every test
# its own isolated index DB, and add a fixture to reset the singleton
# registry between tests so tmp_path reuse across the test session can't
# leak a stale handle:

@pytest.fixture(autouse=True)
def _reset_index_manager():
    manager.reset_for_tests()
    yield
    manager.reset_for_tests()


def _client(settings: Settings) -> TestClient:
    if not settings.db_uri or settings.db_uri == Settings().db_uri:
        settings = replace(settings, db_uri=f"sqlite:///{uuid.uuid4().hex}.sqlite3")
    app = create_app()
    app.dependency_overrides[deps.get_settings_dep] = lambda: settings
    return TestClient(app)
```

(This needs `import pytest` and `from dataclasses import replace` added to the file's imports if not already present — check first.)

Add a new perf-shaped regression test:

```python
def test_journeys_list_does_not_fold_every_shard_per_request(tmp_path, monkeypatch):
    """Regression test for the O(n^2) read path: with the index in place,
    fold() must run once per journey (at index time), not once per
    journey per request."""
    import odyssey.fold as fold_module

    journeys_dir = tmp_path / "journeys"
    for i in range(5):
        jid = f"perf_{i}"
        date_dir = journeys_dir / "2026-08-28"
        date_dir.mkdir(parents=True, exist_ok=True)
        write_events(
            date_dir / f"{jid}.jsonl",
            [
                JourneyEvent(journey_id=jid, seq=0, kind="message", event_id="e0", message=Message(role="user", content="hi")),
                JourneyEvent(journey_id=jid, seq=1, kind="terminal", event_id="e1", terminal=Terminal(termination_reason="ENV_DONE")),
            ],
            header=JourneyHeader(journey_id=jid, data_source="livekit"),
        )

    settings = Settings(journeys_dir=journeys_dir, db_uri=f"sqlite:///{tmp_path}/db.sqlite3", index_interval_seconds=3600)
    client = _client(settings)

    client.get("/journeys")  # triggers the index's first (blocking) pass

    call_count = 0
    real_fold = fold_module.fold

    def counting_fold(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return real_fold(*args, **kwargs)

    monkeypatch.setattr(fold_module, "fold", counting_fold)

    resp = client.get("/journeys")

    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 5
    assert call_count == 0  # no refolding on a request against an already-indexed, unchanged set
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/api && uv run pytest tests/integration/test_api.py -k journeys -v`
Expected: FAIL (settings has no `db_uri` field usage plumbed through yet / journeys router still calls the old filesystem path)

- [ ] **Step 3: Add the dependency**

In `services/api/src/odyssey_api/deps.py`, add:

```python
from odyssey_api.index.manager import IndexHandle, get_index


def get_index_dep(settings: Settings = Depends(get_settings_dep)) -> IndexHandle:
    return get_index(settings)
```

(Check the existing imports in `deps.py` first — `Settings`/`Depends`/`get_settings_dep` are likely already imported; add only what's missing.)

- [ ] **Step 4: Rewrite `domain/journeys.py`'s listing function**

Replace `list_journeys_with_status` (or add alongside it, per whichever the router will call — see Step 5) with an index-backed version:

```python
# services/api/src/odyssey_api/domain/journeys.py -- add:
from typing import List, Optional, Tuple

from odyssey_api.index.manager import IndexHandle


def list_journeys_with_status_indexed(
    index: IndexHandle, product_slug: Optional[str], date: Optional[str]
) -> List[Tuple[str, str, bool]]:
    sql = "SELECT journey_id, date, complete FROM journeys WHERE 1=1"
    params: list = []
    if product_slug is not None:
        sql += " AND product_slug = ?"
        params.append(product_slug)
    if date is not None:
        sql += " AND date = ?"
        params.append(date)
    sql += " ORDER BY date, journey_id"
    rows = index.query(sql, tuple(params))
    return [(r["journey_id"], r["date"], bool(r["complete"])) for r in rows]
```

- [ ] **Step 5: Rewrite the router**

In `services/api/src/odyssey_api/routers/journeys.py`, replace the `list_journeys` function body:

```python
from odyssey_api.deps import get_index_dep
from odyssey_api.index.manager import IndexHandle


@router.get("", response_model=JourneyPageOut)
def list_journeys(
    product: Optional[str] = None,
    date: Optional[str] = None,
    cursor: Optional[str] = None,
    limit: Optional[int] = None,
    index: IndexHandle = Depends(get_index_dep),
) -> JourneyPageOut:
    """``?product=<slug>``/``?date=<YYYY-MM-DD>`` filter server-side against
    the SQLite index (see `odyssey_api.index`) rather than the filesystem.
    ``?cursor=``/``?limit=`` paginate the (already-filtered) result."""
    all_journeys = [
        JourneySummaryOut(journey_id=journey_id, date=journey_date, complete=complete)
        for journey_id, journey_date, complete in domain.list_journeys_with_status_indexed(
            index, product, date
        )
    ]
    items, next_cursor, has_more, total = paginate(all_journeys, cursor, limit)
    return JourneyPageOut(items=items, next_cursor=next_cursor, has_more=has_more, total=total)
```

Remove the now-unused `settings: Settings = Depends(deps.get_settings_dep)` parameter from this function if nothing else in it needs `settings` directly (the index handle already carries the settings it was built from).

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd services/api && uv run pytest tests/integration/test_api.py -v`
Expected: PASS — all existing journeys tests plus the new regression test. If any existing test relies on `get_journey` (single-journey detail), it's unaffected (Task stays on the direct-file-read path per the spec).

- [ ] **Step 7: Commit**

```bash
git add services/api/src/odyssey_api/deps.py services/api/src/odyssey_api/domain/journeys.py services/api/src/odyssey_api/routers/journeys.py services/api/tests/integration/test_api.py
git commit -m "feat(api): /journeys reads from the SQLite index, not the filesystem"
```

---

### Task 10: `/metrics` and `/exports` read from the index

**Files:**
- Modify: `services/api/src/odyssey_api/domain/metrics.py`
- Modify: `services/api/src/odyssey_api/routers/metrics.py`
- Modify: `services/api/src/odyssey_api/domain/exports.py`
- Modify: `services/api/src/odyssey_api/routers/exports.py`
- Modify: `services/api/tests/integration/test_api.py` (metrics/exports tests — same `db_uri`/index-trigger adjustment as Task 9, already covered by the shared `_client` fixture change)

**Interfaces:**
- Produces: `domain.metrics.list_metrics_indexed(index, product) -> list[dict]` (same field names `MetricsSnapshotOut` expects). `domain.exports.list_exports_indexed(index) -> list[dict]` (same field names `ExportArtifactOut` expects).

- [ ] **Step 1: Write the failing tests**

The existing `test_metrics_list`, `test_metrics_list_skips_malformed_lines`, `test_metrics_list_skips_snapshots_missing_required_fields`, and `test_runs_and_exports`'s exports half in `test_api.py` already exercise this path end-to-end (they were updated for pagination in a prior session). No new test file is needed — running them against the not-yet-rewired routers is the "write failing test" step here, since the router rewrite is what Steps 3-4 do.

Run: `cd services/api && uv run pytest tests/integration/test_api.py -k "metrics or exports" -v`
Expected: currently PASS against the old filesystem path — confirm this baseline, then proceed; the assertions themselves don't change, only what serves them.

- [ ] **Step 2: Add indexed query functions**

```python
# services/api/src/odyssey_api/domain/metrics.py -- add:
from typing import Any, Dict, List, Optional

from odyssey_api.index.manager import IndexHandle

_COLUMNS = [
    "ts", "hostname", "os", "cpu_count", "memory_total_bytes",
    "memory_available_bytes", "disk_total_bytes", "disk_free_bytes",
    "project", "public_ip",
]


def list_metrics_indexed(index: IndexHandle, product_slug: Optional[str]) -> List[Dict[str, Any]]:
    sql = f"SELECT {', '.join(_COLUMNS)} FROM metrics_snapshots WHERE 1=1"
    params: list = []
    if product_slug is not None:
        sql += " AND product_slug = ?"
        params.append(product_slug)
    sql += " ORDER BY ts DESC"
    rows = index.query(sql, tuple(params))
    return [dict(row) for row in rows]
```

```python
# services/api/src/odyssey_api/domain/exports.py -- add:
from typing import Any, Dict, List

from odyssey_api.index.manager import IndexHandle


def list_exports_indexed(index: IndexHandle) -> List[Dict[str, Any]]:
    rows = index.query("SELECT name, path, rows, sha256 FROM exports ORDER BY name")
    return [dict(row) for row in rows]
```

- [ ] **Step 3: Rewire the routers**

```python
# services/api/src/odyssey_api/routers/metrics.py
@router.get("", response_model=MetricsPageOut)
def list_metrics(
    product: Optional[str] = None,
    cursor: Optional[str] = None,
    limit: Optional[int] = None,
    index: IndexHandle = Depends(get_index_dep),
) -> MetricsPageOut:
    all_snapshots = []
    for m in domain.list_metrics_indexed(index, product):
        try:
            all_snapshots.append(MetricsSnapshotOut(**m))
        except ValidationError:
            continue
    items, next_cursor, has_more, total = paginate(all_snapshots, cursor, limit)
    return MetricsPageOut(items=items, next_cursor=next_cursor, has_more=has_more, total=total)
```

```python
# services/api/src/odyssey_api/routers/exports.py
@router.get("", response_model=ExportPageOut)
def list_exports(
    cursor: Optional[str] = None,
    limit: Optional[int] = None,
    index: IndexHandle = Depends(get_index_dep),
) -> ExportPageOut:
    all_exports = [ExportArtifactOut(**e) for e in domain.list_exports_indexed(index)]
    items, next_cursor, has_more, total = paginate(all_exports, cursor, limit)
    return ExportPageOut(items=items, next_cursor=next_cursor, has_more=has_more, total=total)
```

Update each file's imports accordingly (`get_index_dep`, `IndexHandle`), and remove the now-unused `settings`/filesystem-based parameters if nothing else in the function needs them.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd services/api && uv run pytest tests/integration/test_api.py -v`
Expected: PASS, full suite (same assertions as before, now served from SQLite)

- [ ] **Step 5: Commit**

```bash
git add services/api/src/odyssey_api/domain/metrics.py services/api/src/odyssey_api/routers/metrics.py services/api/src/odyssey_api/domain/exports.py services/api/src/odyssey_api/routers/exports.py
git commit -m "feat(api): /metrics and /exports read from the SQLite index"
```

---

### Task 11: `/products` reads from the index

**Files:**
- Modify: `services/api/src/odyssey_api/domain/products.py`
- Modify: `services/api/src/odyssey_api/routers/products.py`
- Modify: `services/api/tests/integration/test_api.py` (`test_products_list_drops_api_key`, `test_products_list_empty_when_unset` — these currently seed a `products_file`; update them to seed the `products` table directly via a test-only insert helper, since the JSON-file path stops being read here)

**Interfaces:**
- Produces: `domain.products.list_products_indexed(index) -> list[dict]` (`{"slug": ..., "name": ...}` only — matches today's `ProductOut`, never a key or hash).

- [ ] **Step 1: Update the existing tests to seed via the index DB**

```python
# services/api/tests/integration/test_api.py
def _seed_product(settings: Settings, slug: str, name: str) -> None:
    from odyssey_api.index.manager import get_index
    index = get_index(settings)
    index.query(
        "INSERT INTO products (slug, name, api_key_hash, revoked, created_at) "
        "VALUES (?, ?, 'test-hash', 0, '2026-01-01T00:00:00+00:00')",
        (slug, name),
    )
```

Note: `IndexHandle.query` as written in Task 8 only does `SELECT`s via `fetchall()` — extend it in this task to also support writes, or add a small `execute` method:

```python
# services/api/src/odyssey_api/index/manager.py -- add to IndexHandle:
    def execute(self, sql: str, params: tuple = ()) -> None:
        conn = connect(self._settings.db_uri)
        try:
            with self._lock:
                conn.execute(sql, params)
                conn.commit()
        finally:
            conn.close()
```

Update `_seed_product` above to call `index.execute(...)` instead of `index.query(...)`.

Replace `test_products_list_drops_api_key`'s body to seed via `_seed_product(settings, "unpod", "Unpod")` instead of writing a `products_file`, and assert the response is still `[{"slug": "unpod", "name": "Unpod"}]` with `"secret-key"`/`"test-hash"` not in `resp.text`.

`test_products_list_empty_when_unset` needs no change — an unseeded index's `products` table is empty by construction.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/api && uv run pytest tests/integration/test_api.py -k products -v`
Expected: FAIL (router still reads `products_file`)

- [ ] **Step 3: Add the indexed query + rewire the router**

```python
# services/api/src/odyssey_api/domain/products.py -- add:
from typing import Any, Dict, List

from odyssey_api.index.manager import IndexHandle


def list_products_indexed(index: IndexHandle) -> List[Dict[str, Any]]:
    rows = index.query("SELECT slug, name FROM products WHERE revoked = 0 ORDER BY slug")
    return [dict(row) for row in rows]
```

```python
# services/api/src/odyssey_api/routers/products.py
from odyssey_api.deps import get_index_dep
from odyssey_api.index.manager import IndexHandle


@router.get("", response_model=List[ProductOut])
def list_products(index: IndexHandle = Depends(get_index_dep)) -> List[ProductOut]:
    return [ProductOut(**p) for p in domain.list_products_indexed(index)]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/api && uv run pytest tests/integration/test_api.py -v`
Expected: PASS, full suite

- [ ] **Step 5: Commit**

```bash
git add services/api/src/odyssey_api/domain/products.py services/api/src/odyssey_api/routers/products.py services/api/src/odyssey_api/index/manager.py services/api/tests/integration/test_api.py
git commit -m "feat(api): /products reads from the SQLite index"
```

---

### Task 12: `/journeys/counts` and `/metrics/counts`

**Files:**
- Modify: `services/api/src/odyssey_api/routers/journeys.py`
- Modify: `services/api/src/odyssey_api/routers/metrics.py`
- Modify: `packages/odyssey-schemas/src/odyssey_schemas/__init__.py`
- Test: `services/api/tests/integration/test_api.py` (add new test functions)

**Interfaces:**
- Produces: `CountsOut` (new pydantic model: `by_product: list[ProductCountOut]`, `by_project: list[ProjectCountOut]`, `by_date: list[DateCountOut]`, the last only populated for journeys). `GET /journeys/counts`, `GET /metrics/counts`.

- [ ] **Step 1: Write the failing test**

```python
# services/api/tests/integration/test_api.py -- add:
def test_journeys_counts(tmp_path):
    journeys_dir = tmp_path / "journeys"
    for slug, jid, date in [("unpod", "j1", "2026-08-28"), ("unpod", "j2", "2026-08-28"), ("acme", "j3", "2026-08-29")]:
        date_dir = journeys_dir / slug / date
        date_dir.mkdir(parents=True)
        write_events(
            date_dir / f"{jid}.jsonl",
            [
                JourneyEvent(journey_id=jid, seq=0, kind="message", event_id="e0", message=Message(role="user", content="hi")),
                JourneyEvent(journey_id=jid, seq=1, kind="terminal", event_id="e1", terminal=Terminal(termination_reason="ENV_DONE")),
            ],
            header=JourneyHeader(journey_id=jid, data_source="livekit"),
        )
    settings = Settings(journeys_dir=journeys_dir, db_uri=f"sqlite:///{tmp_path}/db.sqlite3", index_interval_seconds=3600)
    client = _client(settings)

    resp = client.get("/journeys/counts")

    assert resp.status_code == 200
    body = resp.json()
    by_product = {row["product_slug"]: row["count"] for row in body["by_product"]}
    assert by_product == {"unpod": 2, "acme": 1}
    by_date = {row["date"]: row["count"] for row in body["by_date"]}
    assert by_date == {"2026-08-28": 2, "2026-08-29": 1}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/api && uv run pytest tests/integration/test_api.py -k counts -v`
Expected: FAIL with 404 (route doesn't exist)

- [ ] **Step 3: Add the schema**

```python
# packages/odyssey-schemas/src/odyssey_schemas/__init__.py -- add to __all__ and the file:
class ProductCountOut(BaseModel):
    product_slug: Optional[str] = None
    count: int


class ProjectCountOut(BaseModel):
    product_slug: Optional[str] = None
    project: Optional[str] = None
    count: int


class DateCountOut(BaseModel):
    date: str
    count: int


class CountsOut(BaseModel):
    by_product: List[ProductCountOut]
    by_project: List[ProjectCountOut]
    by_date: List[DateCountOut] = []
```

Add `"ProductCountOut", "ProjectCountOut", "DateCountOut", "CountsOut"` to `__all__`.

- [ ] **Step 4: Add the routes**

```python
# services/api/src/odyssey_api/routers/journeys.py -- add, note: route order
# matters in FastAPI -- this must be registered before `@router.get("/{journey_id}")`
# so "/journeys/counts" doesn't get captured as journey_id="counts".
@router.get("/counts", response_model=CountsOut)
def journey_counts(index: IndexHandle = Depends(get_index_dep)) -> CountsOut:
    by_product = index.query(
        "SELECT product_slug, COUNT(*) AS count FROM journeys GROUP BY product_slug"
    )
    by_project = index.query(
        "SELECT product_slug, project, COUNT(*) AS count FROM journeys GROUP BY product_slug, project"
    )
    by_date = index.query(
        "SELECT date, COUNT(*) AS count FROM journeys GROUP BY date ORDER BY date"
    )
    return CountsOut(
        by_product=[ProductCountOut(**dict(r)) for r in by_product],
        by_project=[ProjectCountOut(**dict(r)) for r in by_project],
        by_date=[DateCountOut(**dict(r)) for r in by_date],
    )
```

```python
# services/api/src/odyssey_api/routers/metrics.py -- add:
@router.get("/counts", response_model=CountsOut)
def metrics_counts(index: IndexHandle = Depends(get_index_dep)) -> CountsOut:
    by_product = index.query(
        "SELECT product_slug, COUNT(*) AS count FROM metrics_snapshots GROUP BY product_slug"
    )
    by_project = index.query(
        "SELECT product_slug, project, COUNT(*) AS count FROM metrics_snapshots GROUP BY product_slug, project"
    )
    return CountsOut(
        by_product=[ProductCountOut(**dict(r)) for r in by_product],
        by_project=[ProjectCountOut(**dict(r)) for r in by_project],
        by_date=[],
    )
```

Ensure `/counts` is defined **before** `@router.get("/{journey_id}")` in `journeys.py` (move it above that function if the file currently has `get_journey` first).

- [ ] **Step 5: Run test to verify it passes**

Run: `cd services/api && uv run pytest tests/integration/test_api.py -v`
Expected: PASS, full suite

- [ ] **Step 6: Regenerate the OpenAPI schema and both SDKs**

Run: `bash scripts/codegen.sh`
Expected: `services/api/openapi.json`, `sdk/python/src/odyssey_sdk/resources/{journeys,metrics}.py`, and `sdk/javascript/src/{types.generated.ts,resources/{journeys,metrics}.ts}` are regenerated with `counts()` methods and the new `CountsOut`/`ProductCountOut`/`ProjectCountOut`/`DateCountOut` types. Run `uv run odyssey sdk check-drift` afterward — expect "no drift".

- [ ] **Step 7: Commit**

```bash
git add packages/odyssey-schemas services/api/src/odyssey_api/routers services/api/tests/integration/test_api.py services/api/openapi.json sdk/python/src/odyssey_sdk/resources sdk/javascript/src
git commit -m "feat(api): add /journeys/counts and /metrics/counts, regenerate SDKs"
```

---

### Task 13: `odyssey api reindex` CLI command

**Files:**
- Modify: `services/api/src/odyssey_api/cli.py`
- Test: `services/api/tests/unit/test_cli_reindex.py`

**Interfaces:**
- Produces: `odyssey api reindex` — runs one full pass (journeys + metrics + exports + reconcile) against the settings resolved from env, prints a summary, exits 0.

- [ ] **Step 1: Write the failing test**

```python
# services/api/tests/unit/test_cli_reindex.py
from __future__ import annotations

from typer.testing import CliRunner

from odyssey_api.cli import register


def _make_app():
    import typer

    app = typer.Typer()
    register(app)
    return app


def test_reindex_command_runs_and_prints_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("ODYSSEY_API_JOURNEYS_DIR", str(tmp_path / "journeys"))
    monkeypatch.setenv("ODYSSEY_DB_URI", f"sqlite:///{tmp_path}/db.sqlite3")
    runner = CliRunner()

    result = runner.invoke(_make_app(), ["reindex"])

    assert result.exit_code == 0
    assert "journeys" in result.stdout.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/api && uv run pytest tests/unit/test_cli_reindex.py -v`
Expected: FAIL — no such command

- [ ] **Step 3: Implement**

Add to `services/api/src/odyssey_api/cli.py`'s `register()` function, alongside `serve`/`openapi`/`routes`:

```python
    def reindex() -> None:
        """Force one full index pass (journeys + metrics + exports +
        reconciliation) right now, outside the background worker's
        interval -- useful right after a deploy or in scripts/tests."""
        from odyssey_api.index.exports_indexer import index_exports
        from odyssey_api.index.journeys_indexer import index_journeys
        from odyssey_api.index.metrics_indexer import index_metrics
        from odyssey_api.index.reconcile import reconcile
        from odyssey_api.settings import get_settings
        from odyssey_store.db import connect

        settings = get_settings()
        conn = connect(settings.db_uri)
        try:
            j = index_journeys(conn, settings.journeys_dir)
            m = index_metrics(conn, settings.journeys_dir)
            e = index_exports(conn, settings.exports_dir)
            removed = reconcile(conn)
        finally:
            conn.close()
        print(f"journeys indexed: {j}, metrics indexed: {m}, exports indexed: {e}, reconciled away: {removed}")

    app.command("reindex")(reindex)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/api && uv run pytest tests/unit/test_cli_reindex.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/api/src/odyssey_api/cli.py services/api/tests/unit/test_cli_reindex.py
git commit -m "feat(api): add 'odyssey api reindex' CLI command"
```

---

### Task 14: Dashboard — Journeys page calls `/journeys/counts`

**Files:**
- Modify: `apps/web/src/app/(dashboard)/journeys/page.tsx`

**Interfaces:**
- Consumes: `client.journeys.counts() -> CountsOut` (generated by Task 12's codegen run).

- [ ] **Step 1: Replace the `collectAll`-based date-count computation**

In `apps/web/src/app/(dashboard)/journeys/page.tsx`, replace the `collectAll<JourneySummaryOut>(...)` call (used today to walk every page and compute `dateCounts` client-side) with:

```tsx
const counts = await client.journeys.counts();
const dateCounts = counts.by_date
  .map(({ date, count }) => ({ date, count }))
  .sort((a, b) => b.date.localeCompare(a.date));
```

Remove the now-unused `collectAll` import and the `dateCountMap`/`allForCounts` local computation this replaces. Keep everything else on the page (the paginated table, `TableFilters`, `Pagination`) unchanged — only the counts source moves server-side.

- [ ] **Step 2: Manually verify in a dev server**

Run: `cd apps/web && ODYSSEY_API_BASE_URL=http://127.0.0.1:8000 pnpm dev -p 3011`, with `services/api` running against a fixture with multiple journeys/dates (same manual-verification approach used in the prior pagination session). Confirm the "by date" chip row still renders correct counts and clicking a chip still filters `?date=`.

- [ ] **Step 3: Run the existing web test suite and typecheck**

Run: `cd apps/web && pnpm exec tsc --noEmit && pnpm test`
Expected: PASS, no type errors

- [ ] **Step 4: Commit**

```bash
git add apps/web/src/app/\(dashboard\)/journeys/page.tsx
git commit -m "feat(web): journeys date counts come from /journeys/counts, not client-side collectAll"
```

---

## Self-Review Notes

- **Spec coverage:** every item in Component 1 of the spec (`indexed_files`/`journeys`/`metrics_snapshots`/`exports` schema, incremental indexing, byte-offset tailing, background worker, reconciliation, `/journeys`+`/metrics`+`/exports`+`/products` reading from the index, `/journeys/counts`+`/metrics/counts`, `odyssey api reindex`) has a task. `get_journey` is explicitly left unchanged (Task 9's note) per the spec's own scope cut. The "block on first run" spec requirement is satisfied by Task 8's blocking-on-first-access design, with an explicit note explaining the adaptation from literal server-startup blocking.
- **Not covered here (belongs to Part B):** the `products` table's writer (services/collector's CLI), hash-based auth, and the shared-DB corruption-handling policy — see `2026-09-05-collector-product-management.md`.
- **Out of scope, confirmed against the spec:** datasets/models registries, single-journey detail (`get_journey`).
