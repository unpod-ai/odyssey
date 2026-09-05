# odyssey-collector

The ingest endpoint `odyssey.HttpSink` posts to. Not the read API
(`services/api`, which reads the exact files this service writes) — this
is a receiver, not a query surface.

## Why stdlib, not FastAPI

The job here is I/O: accept a JSONL POST, check a bearer token, persist bytes.
That's not a routing/validation/DTO problem FastAPI solves — it's what
`services/api` will actually need FastAPI for, once `odyssey-schemas` exists
and there are real routers, request models, and OpenAPI generation to do. A
framework commitment made here now would likely be thrown away or awkwardly
merged when that lands (`docs/WORKING.md` items 8.2/1.8 call these out as
"the same server"). The wire contract below is what has to stay stable in the
meantime — everything behind it can change freely.

## Run it

```bash
cd services/collector
uv sync --extra dev
uv run odyssey-collector --data-dir ./collector-data
# or: uv run python -m odyssey_collector.server
```

Point an SDK process at it:

```python
import odyssey
odyssey.init(sink=odyssey.HttpSink("http://127.0.0.1:8787"))
```

## Config

Env-first, explicit argument wins — same precedence as `odyssey.config.resolve()`.

| Env var | CLI flag | Default | |
|---|---|---|---|
| `ODYSSEY_COLLECTOR_HOST` | `--host` | `127.0.0.1` | |
| `ODYSSEY_COLLECTOR_PORT` | `--port` | `8787` | |
| `ODYSSEY_COLLECTOR_DATA_DIR` | `--data-dir` | `./collector-data` | where `<date>/<journey_id>.jsonl` files land |
| `ODYSSEY_DB_URI` | `--db-uri` | unset (required) | SQLite database file/URI for product/tenant roster and shared auth cache with `services/api`. Must be identical in both services (see `docs/runbooks/run-services.md`) |
| `ODYSSEY_COLLECTOR_AUTH_CACHE_TTL_SECONDS` | `--auth-cache-ttl-seconds` | `60` | Auth cache time-to-live in seconds (product/api_key lookups cached to reduce DB hits) |
| `ODYSSEY_COLLECTOR_TIMEZONE` | `--timezone` | `UTC` | IANA name (e.g. `Asia/Kolkata`); which day a batch's date-partition belongs to. Unrecognised names fall back to UTC |
| `ODYSSEY_COLLECTOR_DEBUG` | `--debug` | unset (off) | per-request access log (method, path, status) to stdout, via the `odyssey_collector.requests` logger. Quiet by default — same as before this existed |

## Product scoping

For more than one tenant, use the SQLite database (via `--db-uri`) with the product management CLI commands. Each product has its own `slug`, `name`, and API key (stored as a hash for security). Each product's data writes into its own `<data_dir>/<slug>/<date>/` partition — isolation is structural (one caller's key can never resolve into another product's directory), not just an access check layered on shared storage.

`GET /products` (any registered key) returns the roster as `{slug, name}` pairs, never keys — a debugging/operator aid, not a privacy boundary between products (storage partitioning already is that).

### Managing products

Create a new product with a fresh random API key:

```bash
odyssey-collector --db-uri ./odyssey.db --create-product \
  --product-slug acme --product-name "Acme Corp"
```

This prints the generated API key once — save it now, as it cannot be retrieved later (only hashes are stored in the database).

List all products currently in the database:

```bash
odyssey-collector --db-uri ./odyssey.db --list-products
```

Revoke a product's access (prevent it from authenticating):

```bash
odyssey-collector --db-uri ./odyssey.db --revoke-product acme
```

Rotate a product's API key (invalidate the old key, generate a new one):

```bash
odyssey-collector --db-uri ./odyssey.db --rotate-product acme
```

This prints the new API key once — the old key is no longer valid.

### Shared database with services/api

`ODYSSEY_DB_URI` must point to the same SQLite file in both `services/collector` and `services/api` so they share the same product roster and auth cache. The database is initialized automatically on first use if it doesn't exist; see `docs/runbooks/run-services.md` for deployment guidance.

## Migrating from products.json

If you have an existing `products.json` file from an earlier deployment, use the one-time migration command:

```bash
odyssey-collector --db-uri ./odyssey.db --migrate-products-from-json /path/to/old/products.json
```

This imports all products from the old JSON file into the SQLite database, with API keys hashed. The old `products.json` file is no longer needed after migration and can be safely deleted.

`slug` is what names the directory and every CLI/`prune.py` invocation;
`name` exists for `GET /products` and log/operator legibility.

`prune.py` (below) is unaware of products — point `--data-dir` at
`<data_dir>/<slug>` once per product rather than at the root.

## Wire contract

```
POST /journeys/<url-encoded journey_id>/events
Content-Type: application/x-ndjson; charset=utf-8
Authorization: Bearer <api_key>          # only when the server requires one
<header line><event line>...             # exactly what odyssey.FileSink writes to disk

200 {"journey_id": ..., "events_received": N}
400 malformed batch — not valid odyssey JSONL
401 missing/incorrect Authorization
500 storage failure
```

`GET /health` → `200 {"status": "ok"}`.

`GET /products` (product-scoped mode only, any registered key) →
`200 {"products": [{"slug": ..., "name": ...}, ...]}`; `404` outside
product-scoped mode, `401` without a valid key.

### Cross-journey batching (item 1.7)

`HttpSink.send_batch()` posts several journeys in one request instead of
one `POST /journeys/<id>/events` per journey:

```
POST /batch/events
Content-Type: application/json; charset=utf-8
Content-Encoding: gzip                   # optional, covers the whole envelope
Authorization: Bearer <api_key>          # one key covers every journey in the batch

{"journeys": {"<journey_id>": "<header line>\n<event line>...", ...}}

200 {"results": {"<journey_id>": {"ok": true, "events_received": N}
                  | {"ok": false, "error": "..."}, ...}}
400 malformed envelope
401 missing/incorrect Authorization
```

Always `200` once the envelope itself parses — each journey inside is
validated and stored independently through the same path a single-journey
POST uses, so one journey's malformed blob never blocks the others in the
same request. `odyssey`'s own `drain(..., batch_size=N)` /
`odyssey.init(drain_batch_size=N)` opt into sending batches this size;
default `batch_size=1` never calls `send_batch` at all.

## Opt-in metrics

```
POST /metrics
Content-Type: application/json; charset=utf-8
Authorization: Bearer <api_key>          # only when the server requires one

{"ts": ..., "hostname": ..., "os": ..., "cpu_count": ..., ...}

200 {"ok": true}
400 malformed body
401 missing/incorrect Authorization
500 storage failure
```

Its own channel, independent of journey capture — same auth rules as
every other POST (any product API key from the database), but its own
storage subdirectory, never mixed into a journey shard file:
`<data_dir>/<product_slug>/metrics/<YYYY-MM-DD>.jsonl` in product-scoped
mode, `<data_dir>/metrics/<YYYY-MM-DD>.jsonl` in single-product mode.

`public_ip` is added here, server-side, from `self.client_address[0]` —
the real TCP peer address of the connection — never trusted as client
input; the SDK never sends it and never could authoritatively know it.

The SDK side is opt-in and off by default: `odyssey.init(collect_metrics=True,
metrics_interval=300)` (or `ODYSSEY_COLLECT_METRICS`/`ODYSSEY_METRICS_INTERVAL`)
starts a background thread that POSTs one OS/CPU/mem/disk snapshot per
interval — see `packages/odyssey-core/src/odyssey/metrics.py`. `prune.py`
is unaware of `metrics/` in this pass — same "not done here" treatment as
product scoping above.

## Storage

Today: a local directory, partitioned by the date a batch was received (UTC
by default, `ODYSSEY_COLLECTOR_TIMEZONE`/`--timezone` to change it) —
`<data_dir>/<YYYY-MM-DD>/<journey_id>.jsonl`. Each file is byte-identical in
shape to what `FileSink` produces (own header, own contiguous events), written
through the same `odyssey.jsonl` codec so there's one parser for the wire
format, not two. Date-bucketing keeps a directory — and a single long-lived
`journey_id`'s file — from growing without bound, and makes old dates trivial
to archive or delete wholesale. A journey whose events straddle midnight
splits across two date directories; rare in practice (a journey is normally
one call or session) and each half is still independently readable.

`docs/STRUCTURE.md` names "spool → object store" as this service's eventual
destination — that's a deliberately deferred upgrade: swap the storage call
inside `_Handler._store`, the endpoint contract doesn't move.

## Not done here

- Object-store backing (still local disk)
- Any read path — that's `services/api`
