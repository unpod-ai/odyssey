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
| `ODYSSEY_COLLECTOR_API_KEY` | `--api-key` | unset (open) | one shared bearer token, unscoped. Mutually exclusive with `--products-file` |
| `ODYSSEY_COLLECTOR_PRODUCTS_FILE` | `--products-file` | unset | JSON `{"products": [{"slug", "name", "api_key"}, ...]}` file (product scoping, below). Mutually exclusive with `--api-key` |
| `ODYSSEY_COLLECTOR_TIMEZONE` | `--timezone` | `UTC` | IANA name (e.g. `Asia/Kolkata`); which day a batch's date-partition belongs to. Unrecognised names fall back to UTC |
| `ODYSSEY_COLLECTOR_DEBUG` | `--debug` | unset (off) | per-request access log (method, path, status) to stdout, via the `odyssey_collector.requests` logger. Quiet by default — same as before this existed |

## Product scoping

For more than one tenant, use `--products-file` instead of `--api-key`:

```json
{
  "products": [
    {"slug": "acme", "name": "Acme Corp", "api_key": "sk-acme-..."},
    {"slug": "globex", "name": "Globex Inc", "api_key": "sk-globex-..."}
  ]
}
```

### Bootstrapping the file

`odyssey-collector` deliberately refuses to start on a missing, empty, or
malformed `--products-file` — a silently-created empty roster would be
indistinguishable from "no keys configured", and every request would
just 401 with no explanation. `--init-products-file` is the safe way to
create a real starting one instead: run once, by hand, before starting
the server (never as a side effect of `serve`, and never via an env var
— see `docs/runbooks/run-services.md` for why):

```bash
odyssey-collector --init-products-file ./collector-products.json \
  --product-slug acme --product-name "Acme Corp"
```

Writes one product with a fresh `secrets.token_urlsafe(32)` `api_key` —
a real secret, not a placeholder — and prints it once. Refuses to
overwrite an existing file.

### Adding a product to an already-running deployment

`--add-product-file` appends to an existing roster the same way
`--init-products-file` bootstraps a new one — a fresh random `api_key`,
printed once, refuses a `slug` already present in the roster:

```bash
odyssey-collector --add-product-file ./collector-products.json \
  --product-slug globex --product-name "Globex Inc"
```

Requires the file to already exist (bootstrap one with
`--init-products-file` first) and does not start the server — restart the
running collector afterwards to pick up the new product, same as any
other products-file edit (the roster loads once at process start, not
live).

Each product's key writes into its own `<data_dir>/<slug>/<date>/` partition
— isolation is structural (one caller's key can never resolve into another
product's directory), not just an access check layered on shared storage.
`slug` is what names the directory and every CLI/`prune.py` invocation;
`name` exists for `GET /products` and log/operator legibility.

`GET /products` (any registered key) returns the roster as `{slug, name}`
pairs, never keys — a debugging/operator aid, not a privacy boundary between
products (storage partitioning already is that).

This is a stopgap, not real multi-tenant infrastructure: the roster is a
flat file loaded once at startup — edit it and restart the process to
add/revoke a product. `services/api` (Step 8) is a read-only service and
does not manage collector keys/products either — real key/product
management is still unbuilt anywhere in this repo.

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
every other POST (`--api-key` or a `--products-file` key), but its own
storage subdirectory, never mixed into a journey shard file:
`<data_dir>/<product_slug>/metrics/<YYYY-MM-DD>.jsonl` in product-scoped
mode, `<data_dir>/metrics/<YYYY-MM-DD>.jsonl` in single-shared-key mode.

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

- Real multi-tenant key/product management (the `--products-file` roster above
  is a flat-file stopgap, not a database)
- Object-store backing (still local disk)
- Any read path — that's `services/api`
