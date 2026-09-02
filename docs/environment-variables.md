# Environment variables

Every `ODYSSEY_*` variable in the repo, grouped by which process reads
it. Env-first, explicit-argument-wins is the precedence rule every
member below uses — a CLI flag or constructor argument always overrides
the matching env var, which overrides the hardcoded default. Source of
truth is always the `ENV_*`/`os.environ.get(...)` constant next to each
row below; if this doc and the code disagree, the code wins.

## Naming collision to know about

**`ODYSSEY_API_KEY`** (capture side, below) and
**`ODYSSEY_COLLECTOR_API_KEY`** (`services/collector`, below) are
different secrets for different directions of the same connection:
`ODYSSEY_API_KEY` is the key `odyssey.HttpSink` **sends** when it POSTs
to a collector; `ODYSSEY_COLLECTOR_API_KEY` is the key
`services/collector` **requires** to accept that POST. Set them to the
same value on the two ends of one connection. There is now a third,
unrelated-but-similarly-named variable: **`ODYSSEY_API_AUTH_KEY`**,
the key `services/api` (the read API) optionally **requires** on every
route except `/health` — see its `README.md`'s Auth section. All three
are genuinely separate settings for three separate processes (capture,
collector, read API), easy to confuse by name alone: `ODYSSEY_API_KEY`
is client-side (what `HttpSink` sends to a collector),
`ODYSSEY_COLLECTOR_API_KEY` and `ODYSSEY_API_AUTH_KEY` are both
server-side (what a collector, respectively `services/api`, requires),
and none of the three are interchangeable with each other.

**`ODYSSEY_PROJECT`** (capture side, below) and **`Product`/`products`**
(`services/collector`'s `ODYSSEY_COLLECTOR_PRODUCTS_FILE`/`--products-file`
roster, below) are unrelated concepts despite the similar words: `project`
is a purely descriptive metadata tag (`JourneyHeader.journey_metadata["project"]`,
"which repo/codebase did this capture come from") with no auth meaning at
all, while `Product` is the collector's multi-tenant auth boundary (a
unique `api_key` per top-level tenant, previously — and confusingly —
named `Project` before this rename). See
`docs/superpowers/specs/2026-09-02-product-project-metrics-design.md` for
the full rationale.

---

## Capture (`packages/odyssey-core` — `odyssey.init()`, `HttpSink`)

Source: `packages/odyssey-core/src/odyssey/config.py`,
`packages/odyssey-core/src/odyssey/sinks.py`,
`packages/odyssey-core/src/odyssey/spool.py`.

| Variable | Default | What it controls |
|---|---|---|
| `ODYSSEY_SPOOL` | `.odyssey` | Local append-only spool directory `odyssey.init()` captures into |
| `ODYSSEY_OUT` | `odyssey-out` | Default output directory for export commands (`odyssey spool push --out`, etc.) |
| `ODYSSEY_ENABLED` | `true` | Falsey (`0`/`false`/`no`/`off`/`none`/empty) disables capture entirely — every `journey()`/`@observe` call becomes a no-op |
| `ODYSSEY_DRAIN_INTERVAL` | `30` (seconds) | Declared (`config.ENV_DRAIN_INTERVAL`) but **not currently reachable** through `odyssey.init()` — `init()`'s own `drain_interval` parameter always defaults to a concrete `30.0`, so `resolve()`'s env-var branch (gated on an internal `drain_interval_set=False` no call site ever passes) never runs. Pass `odyssey.init(drain_interval=...)` explicitly instead; verified against `packages/odyssey-core/src/odyssey/config.py` — setting this env var and calling `resolve()` with no arguments still returns `30.0` |
| `ODYSSEY_DRAIN_BATCH_SIZE` | `1` | How many journeys `HttpSink.send_batch()` bundles into one `POST /batch/events` — `1` never calls `send_batch` at all (item 1.7's cross-journey batching is opt-in) |
| `ODYSSEY_DEBUG` | `false` | Truthy re-raises capture failures instead of swallowing them — a development aid; production must never set this (see [`adr/0004-capture-layer.md`](adr/0004-capture-layer.md) decision 4, "never crash the host") |
| `ODYSSEY_MAX_OPEN_SHARDS` | `256` | Cap on simultaneously open spool shard file handles |
| `ODYSSEY_SAMPLE_RATE` | `1.0` | Fraction of journeys actually recorded (clamped to `[0, 1]`), decided once per journey so a sampled-out journey is never partially written |
| `ODYSSEY_TIMEZONE` | `UTC` | IANA name (e.g. `Asia/Kolkata`) — which day a spool shard's rotation/date-partition belongs to. Unrecognised names fall back to UTC |
| `ODYSSEY_ENDPOINT` | unset | `HttpSink`'s target URL when constructed with no explicit `endpoint` — e.g. `http://127.0.0.1:8787` for a local `services/collector` |
| `ODYSSEY_API_KEY` | unset | The bearer token `HttpSink` sends as `Authorization: Bearer <key>` — see "Naming collision" above |
| `ODYSSEY_PROJECT` | unset (auto-detected) | Explicit override for `odyssey.init(project=...)`'s auto-detect chain (`ODYSSEY_PROJECT` → `.git/config`'s `origin` remote → cwd dirname). Tags `JourneyHeader.journey_metadata["project"]` — descriptive only, see "Naming collision" above |
| `ODYSSEY_COLLECT_METRICS` | `false` | Opt-in, off-by-default host telemetry (hostname, OS, CPU count, disk usage, Linux-only memory). When truthy, starts a background thread posting one snapshot per `ODYSSEY_METRICS_INTERVAL` to `POST /metrics` on the configured `HttpSink` endpoint. See `packages/odyssey-core/src/odyssey/metrics.py` |
| `ODYSSEY_METRICS_INTERVAL` | `30` (seconds) | How often the metrics background thread posts a snapshot, when `ODYSSEY_COLLECT_METRICS` is enabled |

## `services/collector` — ingest

Source: `services/collector/src/odyssey_collector/server.py`.

| Variable | CLI flag | Default | What it controls |
|---|---|---|---|
| `ODYSSEY_COLLECTOR_HOST` | `--host` | `127.0.0.1` | Bind host |
| `ODYSSEY_COLLECTOR_PORT` | `--port` | `8787` | Bind port |
| `ODYSSEY_COLLECTOR_DATA_DIR` | `--data-dir` | `./collector-data` | Where `<date>/<journey_id>.jsonl` (or `<slug>/<date>/...` in product-scoped mode) files land |
| `ODYSSEY_COLLECTOR_API_KEY` | `--api-key` | unset (open) | One shared bearer token, unscoped. Mutually exclusive with `ODYSSEY_COLLECTOR_PRODUCTS_FILE` |
| `ODYSSEY_COLLECTOR_PRODUCTS_FILE` | `--products-file` | unset | Path to a `{"products": [{"slug", "name", "api_key"}, ...]}` roster (product scoping). Mutually exclusive with `ODYSSEY_COLLECTOR_API_KEY`. `odyssey-collector --init-products-file` bootstraps this file — see `services/collector/README.md` |
| `ODYSSEY_COLLECTOR_TIMEZONE` | `--timezone` | `UTC` | Which day a batch's date-partition belongs to |
| `ODYSSEY_COLLECTOR_DEBUG` | `--debug` | unset (off) | Per-request access log (method, path, status) to stdout via the `odyssey_collector.requests` logger — quiet by default, same as before this existed |

`--init-products-file`/`--product-slug`/`--product-name` (the bootstrap
command) have **no** env var equivalents, deliberately — see
`docs/runbooks/run-services.md`'s "Switching to product-scoped mode"
section for why. `services/collector` also accepts `POST /metrics` (an
opt-in metrics channel, see `services/collector/README.md`'s "Opt-in
metrics") — it has no env var of its own; it's a route, not a config
knob.

## `services/api` — read API

Source: `services/api/src/odyssey_api/settings.py`. A missing registry
file or directory returns empty (`{}`/`[]`), never an error — every path
below points at storage another member already owns and writes;
`services/api` only reads it.

| Variable | Default | What it controls |
|---|---|---|
| `ODYSSEY_API_HOST` | `127.0.0.1` | Bind host (`odyssey api serve`) |
| `ODYSSEY_API_PORT` | `8000` | Bind port |
| `ODYSSEY_API_JOURNEYS_DIR` | `./collector-data` | Where journeys are read from — point this at `services/collector`'s own `--data-dir` |
| `ODYSSEY_API_DATASETS_REGISTRY` | `data_preparation/datasets/registry.yaml` | `data_preparation`'s corpus registry |
| `ODYSSEY_API_MODELS_REGISTRY` | `training/models/registry.yaml` | `training`'s model registry |
| `ODYSSEY_API_EVAL_REGISTRY` | `evaluation/datasets/registry.yaml` | `evaluation`'s frozen eval-set registry |
| `ODYSSEY_API_EVAL_REPORTS_DIR` | `evaluation/reports` | Where `odyssey eval run` writes reports |
| `ODYSSEY_API_EXPORTS_DIR` | `./exports` | Where export artifacts are read from |
| `ODYSSEY_API_AUTH_KEY` | unset (open) | Optional bearer-token auth (`Authorization: Bearer <key>`) required on every route except `/health`. Same as `odyssey api serve --api-key`. See "Naming collision" above |

## `apps/web` — dashboard

Source: `apps/web/src/lib/api/index.ts`.

| Variable | Default | What it controls |
|---|---|---|
| `ODYSSEY_API_BASE_URL` | `http://127.0.0.1:8000` | The `services/api` instance `apiClient()`/`@odyssey/sdk` points at. Unrelated to `ODYSSEY_API_HOST`/`PORT` above — those configure the server side of the same service, this configures a client pointed at it |
| `ODYSSEY_API_AUTH_KEY` | unset | The bearer token `apiClient()` sends to `services/api` — must match that service's own `ODYSSEY_API_AUTH_KEY` if it has auth enabled |

## Referenced in docs, not implemented in this repo

`docs/WORKING.md` (§ "Environment variables") notes three more —
`ODYSSEY_WIRE_DIR`, `ODYSSEY_ALL_STEPS`, `ODYSSEY_SYSTEM_PROMPT` — but is
explicit that they belong to **the LiveKit deployment in the separate
`super` repo this project was extracted from, not to `odyssey-core`
itself**: "they live in the integration, not in core." A repo-wide grep
here confirms zero implementation — no `os.environ.get(...)` reads any
of the three anywhere in this codebase. If a LiveKit (or similar)
integration is ever built in this repo, it would define and read them
itself; nothing here does today.

## What has no env var, on purpose

- `sdk/python`, `sdk/javascript` — the base URL is a required constructor
  argument (`OdysseySDK(base_url)`), not an env var. The API key
  argument is optional and *does* fall back to an env var
  (`ODYSSEY_API_AUTH_KEY`, read directly by `OdysseySDK.__init__` in
  both SDKs) when not passed explicitly — `apps/web`'s thin wrapper
  reads the same variable itself rather than relying on that fallback.
- `data_preparation`, `training`, `evaluation` — every knob is a CLI flag
  (`odyssey data ...`, `odyssey train ...`, `odyssey eval ...`), no
  `ODYSSEY_DATAPREP_*`/`ODYSSEY_TRAIN_*`/`ODYSSEY_EVAL_*` variables exist.
- `odyssey-collector --init-products-file` — see above; a one-shot bootstrap
  action deliberately kept CLI-only.
- `cli/`'s own planned `--profile`/`~/.odyssey/config.toml` scoped config
  — not built yet (see `cli/README.md`'s "Not done here").
