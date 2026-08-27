# odyssey-collector

The ingest endpoint `odyssey.HttpSink` posts to. Not the read API
(`services/api`, not built yet) — this is a receiver, not a query surface.

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
| `ODYSSEY_COLLECTOR_DATA_DIR` | `--data-dir` | `./collector-data` | where `<journey_id>.jsonl` files land |
| `ODYSSEY_COLLECTOR_API_KEY` | `--api-key` | unset (open) | if set, requires `Authorization: Bearer <key>` |

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

## Storage

Today: a local directory, partitioned by the UTC date a batch was received —
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

- Project scoping (multi-tenant auth beyond a single shared bearer token)
- Object-store backing (still local disk)
- Any read path — that's `services/api`
