# SQLite-backed read index + product management — design

Status: approved, not yet implemented.

Two coupled components, both landing on one shared SQLite file:

1. `services/api` stops scanning the filesystem on every request and
   reads from a self-maintained SQLite index instead (fixes the "high
   read time / no response" symptom, adds product/project journey and
   metrics counts).
2. `services/collector`'s product/tenant roster moves off a hand-edited
   JSON file and onto the same SQLite file, with a new CLI and
   hash-only key storage.

They're one spec because component 2 changes where component 1's
`products` table lives and how it's written.

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

Separately: creating a product today means hand-editing/appending to a
`products.json` roster via `services/collector --init-products-file`/
`--add-product-file`, then restarting the collector process to pick up
the change — no revoke, no rotate, and the roster holds every tenant's
`api_key` in plaintext on disk indefinitely.

## Decisions

Made during brainstorming (`superpowers:brainstorming`), each with the
alternative considered:

| Decision | Chosen | Rejected alternative, and why |
|---|---|---|
| Who maintains the read index | `services/api` indexes itself, in-process | `services/collector` writes through to the index on ingest — rejected; the user wants collector's journey/metrics *storage* untouched: "collector data will stay in json file as we are doing... no changes." (Product identity is a separate exception — see below.) |
| Freshness mechanism | A background thread inside the FastAPI process, polling every `ODYSSEY_API_INDEX_INTERVAL_SECONDS` (default 5s) | An OS-level cron job calling a reindex CLI — a second moving part to deploy/monitor, for no real benefit over an in-process thread |
| Counts | Plain indexed `GROUP BY` SQL queries, computed on read | A materialized `counts` table incrementally bumped on every insert — extra bookkeeping that can drift from the fact tables; at dashboard scale (tens of thousands of rows) an indexed `COUNT(*)` is already sub-millisecond and always correct |
| First-run behavior | Block server startup on one full synchronous index pass | Serve immediately with an empty/partial index that fills in over the first few background cycles — rejected because it means early requests silently under-report; a one-time startup delay is an easy trade for an internal dashboard |
| Journey `project` field | Newly indexed from `JourneyHeader.journey_metadata["project"]` | Leaving it unindexed — rejected, since the user wants journey counts broken down by project too, and this field already exists on disk (added in the prior `2026-09-02-product-project-metrics-design.md` pass) but was never surfaced by `services/api` |
| Where product identity lives | Moved into the shared SQLite file — `products.json` and its loader are deleted entirely | Keep `products.json` as source of truth, add only an editing CLI on top — rejected; the user explicitly chose to move identity into the DB and drop the file-based path entirely |
| `api_key` storage, everywhere | Hash-only (`sha256`), at rest and in the auth check itself — plaintext exists only for the instant it's generated and printed once | Store plaintext (reversible/encrypted) so a key can be shown again later — rejected; standard secret-handling practice (GitHub/Stripe-style), and the user confirmed "lost key → reissue" is an acceptable trade |
| DB file scope | **One shared file** (`ODYSSEY_DB_URI`) for both services | Two separate files (collector's products DB, api's index cache) — the user chose the shared file after weighing the corruption-recovery consequence (see Edge cases) |
| Corruption recovery | Both services **refuse to start** on a corrupt/unreadable DB and log a clear error — no auto-delete | Auto-delete and rebuild on corruption — safe when the file was api-only cache, but now `products` holds real, unrecoverable tenant credentials in the same file; auto-wiping it is unacceptable |
| Config surface | One shared setting, `ODYSSEY_DB_URI`, read by both services | Separate `ODYSSEY_API_DB` (api-only) — superseded once product identity moved into the same file; a single setting now, since one file has two writers |
| Collector's auth-check cost | In-memory cache of the whole `products` table, refreshed every `ODYSSEY_COLLECTOR_AUTH_CACHE_TTL_SECONDS` (default 60s, env-configurable), with a DB-query fallback on cache miss | A DB query on every ingest request — rejected by the user as too much per-request overhead on collector's high-throughput ingest path; the cache trades up to one TTL cycle of revoke-propagation delay for near-zero per-request auth cost |

Explicitly **out of scope** for this pass: any change to how
`services/collector` stores journeys/metrics themselves (still local
JSONL, unchanged); `get_journey` (single journey detail) stays a direct
file read since it's already O(1) per request and always wants
fully-current content; datasets/models registries (small YAML files,
not a measured bottleneck); real-time key caching in collector (every
auth check is a fresh indexed DB lookup — see Component 2).

## Shared infrastructure

One SQLite file, path/URI given by `ODYSSEY_DB_URI` (e.g.
`sqlite:///./odyssey.sqlite3`), opened in **WAL mode** with a
`busy_timeout` pragma set (e.g. 5000ms) so collector's infrequent
`products` writes and api's frequent cache writes never hard-fail on a
lock collision — WAL allows many concurrent readers alongside a single
writer, and the two services never write the same table.

The DDL for all tables lives in one place: a new small shared package,
**`packages/odyssey-store`** (same role `odyssey-schemas` already plays
for pydantic DTOs — a contract both services import, owned by neither's
business logic). Whichever service starts first runs `CREATE TABLE IF
NOT EXISTS` for the whole schema; idempotent, no conflict either order.

**Per-table write ownership** (single-writer per table, even though the
file is shared):

| Table | Written by | Read by |
|---|---|---|
| `products` | `services/collector` (product CLI) | both |
| `indexed_files`, `journeys`, `metrics_snapshots`, `exports` | `services/api`'s indexer | `services/api` only |

**Only `products` is real, unrecoverable state.** The other four tables
remain a disposable cache: rebuildable from the JSONL/export files on
disk at any time, safe to drop and reindex from scratch. `products` is
not — see Edge cases for what that changes about corruption handling,
and why the file now needs regular backups (e.g. `sqlite3 <file>
".backup ..."`), which it didn't when it was api-only cache.

## Component 1 — `services/api` read index

### Architecture

A new `odyssey_api.index` subpackage:

- **`index/db.py`** — connects to the shared `ODYSSEY_DB_URI` file
  (via `odyssey_store`'s schema helper), applies the schema.
- **`index/indexer.py`** — the incremental scanner. For each of
  `journeys_dir` (journeys + `metrics/` subdirectories, flat or
  product-scoped layout — reuses the existing directory-walk logic from
  `repositories/filesystem.py`) and `exports_dir`, decides new /
  changed / unchanged / gone by comparing `(path, mtime, size)` against
  the `indexed_files` manifest table. Only new/changed files get
  parsed — this is what turns O(everything, every request) into O(what
  changed since the last pass). Reads `products` (read-only) to resolve
  `product_slug` for join purposes; never writes it.
- **`index/worker.py`** — a daemon thread started from FastAPI's
  lifespan hook (`odyssey_api.main.create_app`). Runs one full pass
  synchronously before `create_app` returns (blocking first-run
  behavior), then loops every `ODYSSEY_API_INDEX_INTERVAL_SECONDS`
  doing incremental passes, plus a full filesystem-reconciliation pass
  every `ODYSSEY_API_INDEX_RECONCILE_EVERY` cycles (default 20, ~100s)
  to catch deletions. Stopped cleanly on shutdown.
- **Routers stop touching the filesystem** for list endpoints.
  `domain/journeys.py`, `domain/metrics.py`, `domain/exports.py`,
  `domain/products.py` swap their `repositories/filesystem.py` calls for
  SQL queries against the shared DB. `repositories/filesystem.py`'s
  scanning functions become the indexer's private implementation detail
  (called only from `index/indexer.py`), not something a router calls
  per-request anymore.
- A manual trigger — `odyssey api reindex` CLI command — forces an
  immediate full pass, useful right after a deploy or in tests, as a
  supplement to (not a replacement for) the background worker.

### Data model (api-owned tables)

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

### Read path & endpoint changes

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
  a JSON file. Response shape unchanged (`{slug, name}`, still never
  `api_key`/`api_key_hash`).

## Component 2 — `services/collector` product management moves onto the DB

### `products` table (collector-owned)

```sql
CREATE TABLE products (
  slug          TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  api_key_hash  TEXT NOT NULL,     -- sha256(api_key); plaintext never stored, anywhere
  revoked       INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL      -- ISO-8601 UTC
);
CREATE UNIQUE INDEX ix_products_api_key_hash ON products(api_key_hash);
```

### Auth check becomes hash-based, cached in-memory with a miss fallback

`Product.product_for_key(api_key)` becomes: hash the incoming key, look
it up in an **in-memory cache of the whole `products` table**, refreshed
on a background thread every `ODYSSEY_COLLECTOR_AUTH_CACHE_TTL_SECONDS`
(default `60`, override via env var) — the same "background thread
refreshes an in-process cache" pattern Component 1's index worker
already uses. A cache **miss** falls through to a direct, indexed DB
query (`SELECT slug, name FROM products WHERE api_key_hash = ? AND
revoked = 0`) before rejecting the request, so a product created
seconds ago via `products create` authenticates immediately rather than
waiting for the next refresh.

This reintroduces a small staleness window on the *other* direction:
`revoke`/`rotate` can take up to one TTL cycle (60s by default) to take
effect, since a still-cached, now-revoked hash keeps resolving until the
cache refreshes. This is a deliberate trade against per-request DB
latency on collector's high-throughput ingest path — the same class of
staleness already accepted for `services/api`'s index (default 5s
there; collector's default is longer since ingest volume, not
dashboard-count freshness, is the priority here).

### New `odyssey-collector products` subcommands (entirely replace the file-based ones)

- `products create --slug --name` — generates a real key
  (`secrets.token_urlsafe(32)`, unchanged from today), stores only its
  hash, **prints the plaintext once** to stdout. No restart needed —
  the running collector process picks it up on the very next request
  since auth is a live DB lookup, not a startup-loaded cache.
- `products list` — slug / name / revoked / created_at. Never a key or
  hash.
- `products revoke --slug` — sets `revoked = 1`.
- `products rotate --slug` — revokes the old key, issues and prints a
  new one under the same slug, in one step.
- `products migrate-from-json --json-path <old products.json>` —
  **one-time cutover command.** Reads the existing roster, hashes each
  already-existing plaintext `api_key` as-is (no forced rotation, no
  disruption to already-integrated tenants), inserts rows into
  `products`. Run once per deployment during migration, then the JSON
  file is retired.

**Deleted entirely, no dual-mode/deprecation shim:**
`--init-products-file`, `--add-product-file`,
`ODYSSEY_COLLECTOR_PRODUCTS_FILE`, `_load_products_file`,
`_init_products_file`, `_add_product`, and the `Product.api_key`
plaintext field (replaced by a hash comparison at the auth boundary,
never a stored/held plaintext value beyond the moment `create`/`rotate`
generates and prints it).

`--api-key` (the separate single-shared-key, unscoped mode) is
**unaffected** — it doesn't involve the products table at all.

## Edge cases & failure handling

- **Deletions (api index).** `services/collector`'s `prune.py` deletes
  old date directories independently of the API. The periodic full
  reconciliation pass (every `ODYSSEY_API_INDEX_RECONCILE_EVERY`
  cycles, not every incremental one) stats every already-known
  `indexed_files.path` and deletes the corresponding
  `indexed_files`/`journeys`/`metrics_snapshots`/`exports` rows for
  anything gone from disk. Never touches `products`.
- **Malformed/unfoldable shard or metrics line** — skipped and logged,
  same defensiveness `filesystem.py` already has today; never crashes
  the indexer thread or aborts the whole pass.
- **Corrupt or schema-mismatched shared DB file** — detected on
  startup. **Neither service auto-deletes it.** Both fail to start with
  a clear error naming the file, requiring an operator to restore from
  backup or investigate. This is a deliberate change from a pure-cache
  design (delete-and-rebuild would be safe) because the same file now
  holds `products` — real, unrecoverable tenant credentials once this
  ships. Operators must back up `ODYSSEY_DB_URI`'s file going forward.
- **Product resolution ordering (api indexer)** — `products` is
  collector-owned and may be updated independently of any index pass;
  the indexer treats `product_slug` as a soft reference (no FK
  enforcement) — a journey/metric row is never blocked on its product
  existing yet.
- **Concurrent read/write across processes** — SQLite WAL mode plus a
  `busy_timeout` pragma; readers never block on a writer, and the rare
  simultaneous-write case (collector's CLI writing `products` at the
  same instant api's indexer writes a cache table) retries within the
  timeout instead of erroring.

## Testing

- Indexer unit tests against real temp-directory fixtures (this repo's
  established no-mocks convention): incremental re-index picks up new
  files; metrics tailing picks up appended lines without re-reading
  earlier ones; reconciliation drops rows for deleted files; a
  malformed shard/line is skipped, not fatal.
- Router/integration tests: same shapes as today's
  `services/api/tests/integration/test_api.py`, now asserting against
  indexed data instead of live filesystem scans.
- Counts endpoint tests: multi-product, multi-project, multi-date
  fixture, asserting `GROUP BY` correctness.
- A regression test proving `/journeys` no longer calls
  `fold_shard`/`find_journey_path` per request (e.g. by asserting on
  indexer call counts, or a perf-shaped test with a large fixture that
  would time out under the old O(M²) behavior).
- Collector product-CLI tests: `create` prints a key once and only a
  hash is ever persisted; a freshly created product authenticates
  immediately (cache-miss fallback path); `revoke`/`rotate` take effect
  within one cache TTL cycle (test with a short injected TTL, not the
  60s default); `migrate-from-json` preserves existing tenants' keys
  (same hash as hashing the original plaintext) without requiring
  rotation.
- Auth cache unit test: cache hit avoids a DB query entirely (assert on
  query count); cache miss falls through to exactly one DB query.
- A shared-schema test: both services independently applying `CREATE
  TABLE IF NOT EXISTS` against a fresh file (in either start order)
  converges on the same schema with no error.
