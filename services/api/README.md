# odyssey-api

The read API for journeys/datasets/models/eval-runs (items 8.1-8.3):
`GET /journeys`, `/journeys/{id}`, `/datasets`, `/datasets/{name}`,
`/models`, `/models/{name}`, `/runs`, `/exports`, `/health`.

## Not the ingest endpoint

`services/collector` (item 1.8) stays the write side —
`odyssey.HttpSink` posts there, not here. `docs/WORKING.md` flags "8.2 and
1.8 are the same server" as something to plan for eventually, but merging
them now would mean rewriting the collector's ingest path (idempotency,
project-scoping, gzip/`Retry-After` backoff) into FastAPI for no functional
gain today. This service reads the exact files the collector already
writes (`<data_dir>/<date>/<journey_id>.jsonl`) — read-only, through
`odyssey.export.fold_shard`, the same folding path every exporter uses —
and nothing here writes back into that directory.

## Architecture

```
routers/     parse/validate/render only — zero business logic
domain/      use-cases, zero fastapi imports (docs/STRUCTURE.md's rule)
repositories/ storage adapters — filesystem.py is the only one built (see below)
```

`domain/` never imports `fastapi`; it depends only on `repositories/` and
the sibling members whose files it reads (`odyssey`, `odyssey-dataprep`,
`odyssey-training`, `odyssey-eval`). `odyssey_schemas` supplies every
request/response DTO — `routers/` translates a domain result into one of
those models, nothing more.

## Not done here (deliberate scope cuts)

- **`repositories/mongo.py` / `postgres.py` / `objectstore.py`** —
  `docs/STRUCTURE.md` names these; only `filesystem.py` is built. Every
  registry/journey store this service reads is a real file on disk today
  (same "no object-store integration yet" state `odyssey_dataprep.datasets`
  is already in) — a DB-backed repository has no concrete deployment target
  yet. Same explicit-deferral treatment `judges.py` (item 7) got.
- **`workers/drain_consumer.py`** (Kafka -> spool drain) — no Kafka
  broker/topic exists anywhere in this repo. See `src/odyssey_api/workers/README.md`.
- **`migrations/` (alembic)** — no relational schema exists (filesystem
  only). See `migrations/README.md`.
- **`odyssey sdk`/`odyssey db` CLI groups** — depend on `sdk/python` (item
  8.4) and alembic migrations respectively, neither built yet. Only
  `odyssey api serve|openapi|routes` is mounted here.

## Run it

```bash
cd services/api
uv sync --extra dev
uv run uvicorn odyssey_api.main:app --host 127.0.0.1 --port 8000
```

Or, via the `odyssey` CLI (root-level, once `uv sync` has picked up this
member's `odyssey.commands` entry point):

```bash
uv run odyssey api serve --host 127.0.0.1 --port 8000
uv run odyssey api openapi --out services/api/openapi.json
uv run odyssey api routes
```

Point it at real data:

| Env var | Default |
|---|---|
| `ODYSSEY_API_HOST` / `ODYSSEY_API_PORT` | `127.0.0.1` / `8000` |
| `ODYSSEY_API_JOURNEYS_DIR` | `./collector-data` (a `services/collector --data-dir`) |
| `ODYSSEY_API_DATASETS_REGISTRY` | `data_preparation/datasets/registry.yaml` |
| `ODYSSEY_API_MODELS_REGISTRY` | `training/models/registry.yaml` |
| `ODYSSEY_API_EVAL_REGISTRY` | `evaluation/datasets/registry.yaml` |
| `ODYSSEY_API_EVAL_REPORTS_DIR` | `evaluation/reports` |
| `ODYSSEY_API_EXPORTS_DIR` | `./exports` |

## Tests

```bash
uv run pytest tests
```

`tests/unit/` exercises `repositories/`/`domain/` directly; `tests/integration/`
drives the real FastAPI app via `TestClient` against real files on disk
(`Settings` is overridden per-test through FastAPI's own dependency-override
mechanism — no monkeypatching of module globals).
