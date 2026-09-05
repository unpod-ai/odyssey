# services/api SQLite index — design

Status: approved, not yet implemented.

## Problem

`services/api` answers every read (`/journeys`, `/metrics`, `/exports`,
`/products`) by scanning the filesystem fresh, on every single request:

- `domain.journeys.list_journeys_with_status` calls
  `filesystem.find_journey_path` (a full re-walk of `journeys_dir`) and
  `odyssey.export.fold_shard` (a full read + parse + aggregate of the
  shard's every message/tool-call/reward) **once per journey, per
  request**. For M journeys that's M directory rescans plus M full-file
  folds, synchronously, in the request path — effectively O(M²)
  directory-scan work layered under O(M) fold work. This is the
  dominant suspect for the "high read time / no response" symptom as
  journey count grows.
- `domain.metrics.list_metrics` re-reads and re-parses every line of
  every metrics shard, every request — unbounded linear scan, no cache.
- `domain.exports.list_exports` (via `filesystem.list_exports`)
  re-sha256s every export shard's full bytes, every request, even
  though export files are write-once and never change after creation —
  pure repeated waste.
- There is no aggregate/counter layer at all: "how many journeys does
  product X have" means listing and counting everything live.

Additionally, there is currently no way to see journey/metric counts
broken down by product and by project in one place, or to see them at
all without walking every page of the paginated list endpoints
client-side (the dashboard's `collectAll` helper, added in the previous
session, is exactly this workaround).

## Decisions

Made during brainstorming (`superpowers:brainstorming`), each with the
alternative considered:

| Decision | Chosen | Rejected alternative, and why |
|---|---|---|
| Who maintains the index | `services/api` indexes itself, in-process | `services/collector` writes through to the DB on ingest — real-time, but requires collector and api to coordinate a shared DB and crosses collector's/api's today-strict write/read ownership boundary. Explicitly ruled out by the user: "collector data will stay in json file as we are doing... no changes." |
| Freshness mechanism | A background thread inside the FastAPI process, polling every `ODYSSEY_API_INDEX_INTERVAL_SECONDS` (default 5s) | An OS-level cron job calling a reindex CLI — a second moving part to deploy/monitor, for no real benefit over an in-process thread |
| Counts | Plain indexed `GROUP BY` SQL queries, computed on read | A materialized `counts` table incrementally bumped on every insert — extra bookkeeping that can drift from the fact tables; at dashboard scale (tens of thousands of rows) an indexed `COUNT(*)` is already sub-millisecond and always correct |
| `api_key` handling | Store `sha256(api_key)` only | Store the plaintext `api_key` — rejected; conflicts with the existing rule that `services/api` never persists or exposes a product's real key. A one-way hash is safe at rest (`secrets.token_urlsafe(32)`-strength keys resist brute force) while still letting a row be identity-linked to a specific key if ever needed |
| First-run behavior | Block server startup on one full synchronous index pass | Serve immediately with an empty/partial index that fills in over the first few background cycles — rejected because it means early requests silently under-report; a one-time startup delay is an easy trade for an internal dashboard |
| Journey `project` field | Newly indexed from `JourneyHeader.journey_metadata["project"]` | Leaving it unindexed — rejected, since the user wants journey counts broken down by project too, and this field already exists on disk (added in the prior `2026-09-02-product-project-metrics-design.md` pass) but was never surfaced by `services/api` |
| DB file config | New `Settings.index_db` / `ODYSSEY_API_DB` path, explicit | Derive automatically from `journeys_dir` — rejected in favor of an explicit setting, matching every other `Settings` field's env-var-first pattern |

Explicitly **out of scope** for this pass: any change to
`services/collector`'s storage or wire format; `get_journey` (single
journey detail) stays a direct file read since it's already O(1) per
request and always wants fully-current content; real auth verification
using `api_key_hash` (it's stored for future use, nothing reads it yet);
datasets/models registries (small YAML files, not a measured bottleneck).

## Architecture

A new `odyssey_api.index` subpackage:

- **`index/db.py`** — SQLite connection management. WAL mode (allows
  FastAPI's request-handling threads to read while the indexer thread
  writes, without blocking each other), schema creation/migration.
- **`index/indexer.py`** — the incremental scanner. For each of
  `journeys_dir` (journeys + `metrics/` subdirectories, flat or
  product-scoped layout — reuses the existing directory-walk logic from
  `repositories/filesystem.py`), `exports_dir`, and `products_file`,
  decides new / changed / unchanged / gone by comparing `(path, mtime,
  size)` against the `indexed_files` manifest table. Only new/changed
  files get parsed — this is what turns O(everything, every request)
  into O(what changed since the last pass).
- **`index/worker.py`** — a daemon thread started from FastAPI's
  lifespan hook (`odyssey_api.main.create_app`). Runs one full pass
  synchronously before `create_app` returns (blocking first-run
  behavior), then loops every `ODYSSEY_API_INDEX_INTERVAL_SECONDS`
  doing incremental passes, plus a full filesystem-reconciliation pass
  every `ODYSSEY_API_INDEX_RECONCILE_EVERY` cycles (default 20, ~100s)
  to catch deletions (see Edge cases). Stopped cleanly on shutdown.
- **Routers stop touching the filesystem** for list endpoints.
  `domain/journeys.py`, `domain/metrics.py`, `domain/exports.py`,
  `domain/products.py` swap their `repositories/filesystem.py` calls for
  SQL queries against the index DB. `repositories/filesystem.py`'s
  scanning functions become the indexer's private implementation detail
  (called only from `index/indexer.py`), not something a router calls
  per-request anymore.
- **The DB is a disposable cache, never a source of truth.** Deleting
  the file and restarting is always safe — the next boot just re-runs a
  full index from the JSONL/export/products files, which remain the
  only real data. This preserves `services/api`'s existing "never own
  real data" boundary; it now owns a rebuildable derived index of its
  own, not collector's data.
- A manual trigger — `odyssey api reindex` CLI command — forces an
  immediate full pass, useful right after a deploy or in tests, as a
  supplement to (not a replacement for) the background worker.

## Data model

```sql
-- Bookkeeping: what the indexer has already seen, and where it left off.
CREATE TABLE indexed_files (
  path        TEXT PRIMARY KEY,   -- absolute path
  kind        TEXT NOT NULL,      -- 'journey' | 'metrics' | 'export'
  mtime_ns    INTEGER NOT NULL,
  size_bytes  INTEGER NOT NULL,
  byte_offset INTEGER NOT NULL DEFAULT 0,  -- metrics files: bytes already parsed (append-only tailing)
  indexed_at  TEXT NOT NULL         -- ISO-8601 UTC
);

CREATE TABLE products (
  slug          TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  api_key_hash  TEXT,             -- sha256(api_key), never the plaintext
  indexed_at    TEXT NOT NULL
);

CREATE TABLE journeys (
  journey_id        TEXT PRIMARY KEY,
  product_slug      TEXT,                  -- NULL in single-tenant/flat layout
  project           TEXT,                  -- from JourneyHeader.journey_metadata["project"]
  date              TEXT NOT NULL,         -- partition date, 'YYYY-MM-DD'
  complete          INTEGER NOT NULL,      -- 0/1
  incomplete_reason TEXT,
  num_steps         INTEGER,
  aggregated_reward REAL,
  num_tool_calls    INTEGER,
  num_tool_failures INTEGER,
  tool_error_rate   REAL,
  source_path       TEXT NOT NULL,
  source_mtime_ns   INTEGER NOT NULL,
  indexed_at        TEXT NOT NULL
);
CREATE INDEX ix_journeys_product_date ON journeys(product_slug, date);
CREATE INDEX ix_journeys_product_project ON journeys(product_slug, project);

CREATE TABLE metrics_snapshots (
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
);
CREATE INDEX ix_metrics_product_project ON metrics_snapshots(product_slug, project);
CREATE INDEX ix_metrics_hostname_ts ON metrics_snapshots(hostname, ts);

CREATE TABLE exports (
  path        TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  rows        INTEGER NOT NULL,
  sha256      TEXT NOT NULL,
  mtime_ns    INTEGER NOT NULL,
  indexed_at  TEXT NOT NULL
);
```

Notes:

- No separate "counters" table — see Decisions above.
- Metrics files are append-only logs a probe keeps writing to all day;
  `indexed_files.byte_offset` lets the indexer `seek()` past bytes
  already parsed and only read new lines, instead of re-parsing the
  whole file on every change.
- Journey shards are check-and-refold on `(mtime, size)` change (covers
  a journey still receiving events); once `complete=1` and the file
  stops changing, it's indexed exactly once, ever.
- `indexed_at` is ISO-8601 UTC text — SQLite has no native datetime
  type, and every other timestamp in this codebase (`ts`, `date`)
  already uses ISO-8601 strings, so this stays consistent and remains
  lexicographically sortable.

## Read path & endpoint changes

- **`/journeys`, `/metrics`, `/exports`** — `domain/*.py` query SQL
  (`SELECT ... WHERE product_slug = ? AND date = ? ORDER BY ... LIMIT ?
  OFFSET ?`) instead of scanning files, reusing the existing
  cursor-pagination envelopes (`JourneyPageOut`, `MetricsPageOut`,
  `ExportPageOut`) — no API/SDK contract change, only a faster
  implementation underneath. `?product=`/`?date=` filters become SQL
  `WHERE` clauses instead of Python list comprehensions.
- **New: `GET /journeys/counts` and `GET /metrics/counts`** — each
  returns `{by_product: [{product_slug, count}], by_project:
  [{product_slug, project, count}], by_date: [{date, count}]}` (dates
  only meaningful for journeys) via one indexed `GROUP BY` query per
  breakdown. In scope for this pass, both the backend endpoints and
  switching the dashboard's Journeys page (its `collectAll`-based
  client-side date/project counting, added last session) to call
  `/journeys/counts` instead of walking every page itself.
- **`get_journey`** (single-journey detail) is unchanged — direct file
  read + fold, already O(1) per request.
- **`GET /products`** reads the `products` table instead of re-parsing
  `products_file` every request. Response shape unchanged (`{slug,
  name}`, still never `api_key`/`api_key_hash`).

## Edge cases & failure handling

- **Deletions.** `services/collector`'s `prune.py` deletes old date
  directories independently of the API. The periodic full-reconciliation
  pass (every `ODYSSEY_API_INDEX_RECONCILE_EVERY` cycles, not every
  incremental one) stats every already-known `indexed_files.path` and
  deletes the corresponding `indexed_files`/`journeys`/`metrics_snapshots`/
  `exports` rows for anything gone from disk.
- **Malformed/unfoldable shard or metrics line** — skipped and logged,
  same defensiveness `filesystem.py` already has today; never crashes
  the indexer thread or aborts the whole pass.
- **Corrupt or schema-mismatched DB file** — detected on startup;
  delete and rebuild from scratch rather than attempting repair, since
  it's a pure cache.
- **Product resolution ordering** — each pass indexes `products_file`
  before journeys/metrics, since journey/metric rows carry a soft
  `product_slug` reference (no FK enforcement — a product can be added
  after journeys already exist under its slug, and a journey/metric row
  is never blocked on a product existing).
- **Concurrent read during a write** — SQLite WAL mode; readers never
  block on the indexer's writes.

## Testing

- Indexer unit tests against real temp-directory fixtures (this repo's
  established no-mocks convention): incremental re-index picks up new
  files; metrics tailing picks up appended lines without re-reading
  earlier ones; reconciliation drops rows for deleted files; a
  malformed shard/line is skipped, not fatal; product ordering resolves
  slugs correctly.
- Router/integration tests: same shapes as today's
  `services/api/tests/integration/test_api.py`, now asserting against
  indexed data instead of live filesystem scans.
- Counts endpoint tests: multi-product, multi-project, multi-date
  fixture, asserting `GROUP BY` correctness.
- A regression test proving `/journeys` no longer calls
  `fold_shard`/`find_journey_path` per request (e.g. by asserting on
  indexer call counts, or a perf-shaped test with a large fixture that
  would time out under the old O(M²) behavior).
