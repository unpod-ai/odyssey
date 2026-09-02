# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this
project has not yet made a versioned release, so entries accumulate under
`[Unreleased]`. Pre-changelog history lives in `git log`.

## [Unreleased]

### Changed

- `docs/runbooks/run-services.md` now also covers `apps/web` in
  production (`pnpm --filter @odyssey/web build` + `next start`, its own
  systemd unit) — previously only `services/api`/`services/collector`
  were covered and `apps/web` was explicitly out of scope. Both commands
  verified against this repo (`pnpm build` succeeds, `next start` serves
  a real `200`).

- **BREAKING**: `services/collector`'s `Project` auth concept renamed to
  `Product` — `--keys-file`/`ODYSSEY_COLLECTOR_KEYS_FILE` →
  `--products-file`/`ODYSSEY_COLLECTOR_PRODUCTS_FILE`, the JSON shape's
  `"projects"` key → `"products"`, `--init-keys-file`/`--project-slug`/
  `--project-name` → `--init-products-file`/`--product-slug`/
  `--product-name`, `GET /projects` → `GET /products`,
  `config.project_for_key()` → `config.product_for_key()`. A clean rename,
  no alias for the old names — nothing outside this repo has integrated
  against them yet (see
  `docs/superpowers/specs/2026-09-02-product-project-metrics-design.md`).
  Storage layout (`<data_dir>/<slug>/<date>/...`) is unchanged, `slug`
  just belongs to a `Product` now. `--api-key` (single-shared-key mode)
  is untouched — it was never project/product-scoped.
- `packages/odyssey-core/src/odyssey/sinks.py`'s `HttpSink` transport
  extracted into a standalone `HttpTransport` class (connection reuse,
  gzip, backoff/retry-after handling) — no behavior change for `HttpSink`
  itself, but it's now reusable by other stdlib-`http.client` senders in
  this package (the new metrics reporter below is the first).
- **BREAKING**: `SCHEMA_VERSION` bumped `1.1` → `2.0` (item 0′.4). Added a new
  `"voice"` `EventKind` and `VoiceEvent` payload (`voice_kind`, `text`,
  `confidence`, `latency_ms`, `metadata`) for STT/TTS/barge-in/latency
  signals, wired into `integrations/livekit.py`. A schema-1.x reader has no
  branch for `"voice"` and cannot safely ignore an unrecognized kind the way
  a 1.0 reader ignored 1.1's new header keys, so `jsonl.py`'s major-version
  gate now refuses any file declaring a `1.x` (or earlier) schema version. No
  migration tool ships with this change — a schema-1.x `*.jsonl` shard on
  disk simply stops parsing under this reader.
- `HttpSink` (item 1.7) now reuses one `http.client.HTTPConnection` across
  `send()`/`send_batch()` calls instead of opening a fresh connection per
  journey — `services/collector`'s `_Handler.protocol_version = "HTTP/1.1"`
  opts in server-side. Draining N journeys in one process now costs one
  TCP/TLS handshake, not N; a dropped keep-alive connection is retried once
  transparently.
- `packages/odyssey-core`'s `pyrefly` config now permanently checks `tests/`
  as well as `src`/`scripts` (item 9.10). Turning this on surfaced 200
  errors, not the ~157 previously estimated: 4 were real type-safety bugs in
  `src/` (a too-narrow `_guard` callback signature and some loosely-`str`-typed
  fields in `integrations/langchain.py` that should have been the real
  `Role`/`TerminationReason` literals), fixed properly rather than
  suppressed. The remaining 196 were `tests/` narrowing gaps — `Optional[...]`
  fields accessed without narrowing first — fixed per call site with
  `assert x is not None` / `(x or {})[...]` idioms, plus a couple of test
  helper signatures widened to their real `Literal` types.

### Added

- **`GET /metrics` on `services/api` + a `apps/web` dashboard page** —
  exposes `services/collector`'s `POST /metrics` host telemetry snapshots
  (opt-in via `ODYSSEY_COLLECT_METRICS`, off by default) through the read
  API and a new `/metrics` page, closing the scope cut the original
  Product/Project/metrics design explicitly deferred ("no route reads
  `/metrics` data"). New `MetricsSnapshotOut` DTO in `odyssey_schemas`;
  `repositories/filesystem.list_metrics` reads
  `<journeys_dir>/metrics/*.jsonl` (mirrors `services/collector`'s
  storage path exactly, skips malformed lines rather than raising, same
  defensiveness as journey-shard folding), a thin `domain/metrics.py`
  passthrough, `routers/metrics.py` (`GET /metrics` → `List[MetricsSnapshotOut]`),
  registered in `main.py` behind `require_api_key` like every other data
  route. Deliberately only the flat, non-product-scoped layout, same
  documented scope cut `/journeys` already has. Both SDKs and `apps/web`
  regenerated a matching `metrics` resource/page (`sdk/python`,
  `sdk/javascript` codegen; `apps/web/src/app/(dashboard)/metrics/page.tsx`
  mirrors the existing `runs`/`exports` pages exactly, `Nav.tsx` gained a
  `Metrics` link). `client.py`/`client.ts` in both hand-written SDK
  wrappers updated to register the new resource (codegen only emits
  `resources/*`, not the top-level client wiring). `ODYSSEY_METRICS_INTERVAL`'s
  default lowered from 300s to 30s. Verified end to end against a real
  running `services/collector` + `services/api` + `apps/web` stack: a real
  host snapshot posted to the collector, read back through `GET /metrics`,
  and rendered in the server-rendered `/metrics` page HTML.
- **`services/api` optional bearer-token auth** — previously had none at
  all. `ODYSSEY_API_AUTH_KEY` env var / `--api-key` CLI flag on
  `odyssey api serve`, checked via `Authorization: Bearer <key>` on every
  route except `GET /health` (always open, for monitoring/liveness
  probes), open by default when unset — matches prior (unauthenticated)
  behavior exactly. Deliberately not named `ODYSSEY_API_KEY` — that name
  already belongs to `packages/odyssey-core`'s `HttpSink` (a different,
  client-side setting). Implemented with `fastapi.security.HTTPBearer` +
  `secrets.compare_digest` (not a raw `Header()` param — that leaked a
  spurious `authorization` parameter and `422` response into every
  route's OpenAPI schema) — `HTTPBearer` also adds the RFC-required
  `WWW-Authenticate: Bearer` header on a 401 and makes the scheme name
  case-insensitive. Both SDKs (`sdk/python`, `sdk/javascript`) gained a
  matching `ODYSSEY_API_AUTH_KEY` env var fallback for their existing
  `api_key`/`apiKey` constructor parameter; `apps/web`'s `apiClient()`
  passes it through (server-side only — confirmed it can never reach the
  browser bundle, and the app has zero client components to leak it
  through anyway). New tests across all four members proving 401 without
  auth, 200 with the right key, 401 with the wrong key, `/health` staying
  open even when a key is configured, and byte-for-byte unchanged
  behavior when no key is set at all. `services/api/openapi.json`
  regenerated (the only file the new security scheme touches — both SDK
  generators ignore header/security params, so no generated resource
  code moved). Verified end to end against a real running server.
- **`odyssey.init(project=...)`** (`packages/odyssey-core/src/odyssey/project.py`,
  `resolve_project()`) — tags `JourneyHeader.journey_metadata["project"]`
  with which repo/codebase a capturing process belongs to: explicit
  argument wins, then `ODYSSEY_PROJECT`, then `.git/config`'s
  `[remote "origin"]` URL (parsed with stdlib `configparser`, no `git`
  subprocess), then the cwd's directory name as the always-succeeding
  fallback. `odyssey.init(project=None)` explicitly opts out (no `project`
  key written at all) — same `_UNSET` sentinel trick `drain_interval_set`
  already uses to distinguish "not given" from "given as `None`". Purely
  descriptive metadata, never an auth boundary — unrelated to
  `services/collector`'s `Product` concept below despite the similar word
  (see `docs/environment-variables.md`'s "Naming collision" section).
- **`odyssey.init(collect_metrics=..., metrics_interval=...)`**
  (`packages/odyssey-core/src/odyssey/metrics.py`, `MetricsReporter`) —
  opt-in, off-by-default host telemetry. When enabled
  (`ODYSSEY_COLLECT_METRICS`/`collect_metrics=True`, default `False`), a
  background daemon thread modeled directly on `spool.IntervalDrainer`
  posts one OS/CPU/mem/disk snapshot (`build_snapshot()` — stdlib
  `platform`/`os.cpu_count`/`shutil.disk_usage`, plus Linux-only memory
  from `/proc/meminfo`, simply omitted elsewhere) to `POST /metrics` on
  the collector every `metrics_interval` seconds (`ODYSSEY_METRICS_INTERVAL`,
  default `300`). Reuses the new `HttpTransport` extraction above for
  connection reuse/gzip/backoff. Failures are counted (`Client.stats`),
  never raised — a metrics POST failure never crashes the host (ADR
  0004). When disabled, no code in this module runs and nothing leaves
  the process.
- **`POST /metrics`** on `services/collector` — the server side of the
  opt-in metrics channel above. Same auth path as every other POST
  (`--api-key` or a `--products-file` key); adds `public_ip` server-side
  from `self.client_address[0]` (the real TCP peer address — the SDK
  never sends or determines its own public IP). Stores one line per
  snapshot at `<data_dir>/<product_slug>/metrics/<date>.jsonl` in
  product-scoped mode, `<data_dir>/metrics/<date>.jsonl` in
  single-shared-key mode — its own subdirectory, never mixed into a
  journey shard file. `prune.py` is unaware of `metrics/` in this pass.
- **`docs/environment-variables.md`** — every `ODYSSEY_*` variable in the
  repo, grouped by which process reads it (capture layer,
  `services/collector`, `services/api`, `apps/web`), with the
  `ODYSSEY_API_KEY` (client, sent to a collector) vs
  `ODYSSEY_COLLECTOR_API_KEY` (server, required by a collector) naming
  collision called out explicitly. Also documents a real finding:
  `ODYSSEY_DRAIN_INTERVAL` is declared but not currently reachable
  through `odyssey.init()`'s public API (`init()`'s own `drain_interval`
  parameter always defaults to a concrete `30.0`, so `resolve()`'s
  env-var branch never runs) — verified by setting the env var and
  calling `resolve()` with no arguments. Notes `ODYSSEY_WIRE_DIR`/
  `ODYSSEY_ALL_STEPS`/`ODYSSEY_SYSTEM_PROMPT` (named in `docs/WORKING.md`)
  belong to a LiveKit deployment in the separate `super` repo, not to
  anything implemented here — confirmed via repo-wide grep, zero hits.
- **`odyssey-collector --init-keys-file`** — bootstraps a real
  `--keys-file` roster (one project, a fresh `secrets.token_urlsafe(32)`
  `api_key`, printed once) and exits without starting the server. A
  deployment following `docs/runbooks/run-services.md` hit
  `_load_keys_file`'s `FileNotFoundError` on startup after pointing
  `ODYSSEY_COLLECTOR_KEYS_FILE` at a path that was never created;
  `_load_keys_file` deliberately still refuses to start on a missing,
  empty, or malformed roster (a silently-created empty/placeholder file
  would look identical to "no keys configured" and every request would
  just 401 with no explanation), so this adds a safe, explicit, one-shot
  way to create a real one instead — never a side effect of `serve`, and
  deliberately with **no** `ODYSSEY_COLLECTOR_*` env var equivalent
  (giving it one would make every `Restart=on-failure` restart re-run the
  bootstrap and restart-loop once the file exists). Refuses to overwrite
  an existing file. 3 new tests; verified end to end against a real
  server (bootstrap → start with `--keys-file` → the generated key
  authenticates, a wrong key doesn't).
- **`docs/runbooks/run-services.md`** — how to run `services/collector`
  and `services/api` in production: a systemd unit for the collector
  (stdlib `ThreadingHTTPServer`, not WSGI/ASGI — gunicorn does not apply
  to it), and two verified options for the API — `uvicorn --workers N`
  (no new dependency) or gunicorn with `uvicorn.workers.UvicornWorker`
  (new optional `prod` extra on `services/api`, `gunicorn>=23`). Both
  commands actually run against this repo before being documented.
- `services/api`'s `prod` optional-dependency extra (`gunicorn>=23`) —
  only pulled in by `uv sync --extra prod`, not part of the base install.
- `integrations/langchain.py` — `OdysseyCallbackHandler()` for LangChain
  (optional `odyssey[langchain]` extra), one flat journey per top-level
  `run_id`.
- `odyssey.pii` — regex-based `scan_pii`/`redact_pii` for content-level PII
  (email/phone/credit card with Luhn check/SSN), wired into
  `data_preparation`'s `clean_dir`/`validate_dir` as opt-in.
- Sampling: `ODYSSEY_SAMPLE_RATE` / `Config.sample_rate`, one coin-flip per
  journey at open time.
- `HttpSink` gzip compression (default on) and client-side `Retry-After`
  backoff on HTTP 429; `services/collector` decompresses accordingly.
- `data_preparation`'s `collect_from_object_store()` — S3-compatible raw-layer
  collection (optional `odyssey-dataprep[s3]` extra), wired into
  `odyssey data collect --bucket`.
- `spool.gc()` / `odyssey spool prune` and `services/collector`'s
  `prune.py` / `python -m odyssey_collector.prune` — retention/TTL for
  fully-drained shards and stale date partitions, operator-invoked only.
- `integrations/anthropic.py`: async streaming capture (item 0′.5) —
  `AsyncAnthropic.messages.stream()` now records the assembled final message,
  matching the existing sync `messages.stream()` behavior.
- `services/collector`: server-side idempotency (item 1.9) — `_store()` skips
  any `event_id` already committed to the destination file, so a retried
  `HttpSink` POST no longer double-writes the raw layer.
- `integrations/gemini.py` (item 0.9) — drop-in `Client` for `google-genai`
  (optional `odyssey[gemini]` extra), sync (`client.models`) + async
  (`client.aio.models`) + opt-in `instrument()` patch. New
  `builders.messages.messages_from_gemini` parser for Gemini's
  `Content`/`parts` shape (`function_call`/`function_response` parts,
  `thought` parts → `Message.reasoning`).
- LangGraph compatibility (item 0′.2) — no new code: verified that a
  compiled `StateGraph`'s `invoke()`/`ainvoke()` and every node (including
  `langgraph.prebuilt.ToolNode`) dispatch through the same
  `on_chain_start`/`on_chain_end`/`on_llm_*`/`on_tool_*` callback tree the
  existing `OdysseyCallbackHandler()` already records, against real
  installed `langgraph`/`langchain-core`.
- `services/collector`: project scoping (item 1.6) — a `projects` roster
  (`--keys-file`/`ODYSSEY_COLLECTOR_KEYS_FILE`, JSON `{"projects":
  [{"slug", "name", "api_key"}, ...]}`), mutually exclusive with the
  existing single shared `api_key`. Each project's key writes into its own
  `<data_dir>/<slug>/<date>/` partition — structural isolation, not just an
  access check on shared storage. New `GET /projects` (any registered key)
  lists `{slug, name}` for the roster, never keys.
- `integrations/otel.py` (items 0.11/0′.3) — `OdysseySpanProcessor()`, an
  `opentelemetry.sdk.trace.SpanProcessor` (optional `odyssey[otel]` extra).
  One journey per OTel trace; a span becomes a `Message` only when it
  carries `gen_ai.*` content, checked across the three shapes actually
  emitted in practice (`gen_ai.input.messages`/`.output.messages`
  attributes, `gen_ai.content.prompt`/`.completion` events, legacy
  `gen_ai.prompt`/`.completion` attributes). Other instrumentation
  vocabularies (OpenInference, etc.) are an explicit, documented scope cut.
- `data_preparation`'s `augmentation.paraphrase_journey`/
  `.generate_synthetic_negative` (item 3.5, optional `odyssey-dataprep[llm]`
  extra) — LLM-backed paraphrase and synthetic-negative generation, both
  opt-in and off by default. `paraphrase_journey` rewords only real user
  turns, keeping the assistant's trainable output untouched.
  `generate_synthetic_negative` emits a `superseded`-then-`trainable` step
  chain — the shape `odyssey.dpo.dpo_pairs` looks for. Wired into
  `odyssey data augment --paraphrase N --synthetic-negatives`.
- Cross-journey payload batching (item 1.7) — `HttpSink.send_batch()` posts
  several journeys' events in one `POST /batch/events` request instead of
  one POST per journey; `services/collector` validates and stores each
  journey independently through the same path a single-journey POST uses
  and reports a per-journey `{"ok": true|false, ...}` result. `drain()`
  gains `batch_size` (`Spool.push(..., batch_size=N)`,
  `odyssey.init(drain_batch_size=N)` / `ODYSSEY_DRAIN_BATCH_SIZE`), opt-in
  and defaulting to `1` (today's per-journey behavior, unchanged for a sink
  without `send_batch`). Every journey's watermark still advances or
  retries off *that journey's own* reported outcome, never off other
  journeys in the same batch — resolves the earlier "needs a redesign of
  `drain()`'s per-journey semantics for partial-batch failure" concern
  rather than working around it.

- `training/checkpoints.py`'s `upload_checkpoint()` (item 5.9) — uploads a
  `soup train --output` checkpoint dir's bytes to an S3-compatible object
  store (`odyssey-training[s3]` extra, `boto3` lazily imported only when
  no `client=` double is injected — the same seam `data_preparation`'s
  `collect_from_object_store` (item 1.10) uses), returning
  `{uri, files, manifest_sha256}`. `experiments.write_experiment_manifest`
  gained `checkpoint_uri`/`checkpoint_sha256` params to record that
  pointer, per ADR 0002's "git holds the recipe and the hash, the object
  store holds the bytes." New `odyssey train upload-checkpoint` /
  `odyssey train record-experiment --checkpoint-uri/--checkpoint-sha256`.
  Closes Step 5.

- `training/models_registry.py`'s `register_model()` (item 6.1) — writes
  `models/registry.yaml` entries (`name -> version -> sha256 -> URI ->
  base model -> corpus version`, per `docs/STRUCTURE.md`'s schema),
  idempotent on `(name, version)` — replaces in place rather than
  duplicating, mirroring `data_preparation`'s `datasets.update_registry`.
  `sha256`/`uri` are meant to be `checkpoints.upload_checkpoint`'s own
  output (item 5.9). New top-level `odyssey model register` CLI group.
- `models_registry.write_model_card()` (item 6.2) — `models/cards/<name>
  -v<version>.md`, mirroring `datasets.write_card`'s provenance + policy
  shape; `promote_model()`/`resolve_model()`/`export_model()` (item 6.4)
  — a named alias (default `"production"`) points at a registered
  version, kept separate from registering it in the first place;
  `export_model()` downloads that version's checkpoint bytes via a new
  `checkpoints.download_checkpoint()` (the inverse of `upload_checkpoint`)
  and verifies the result against the registry's own recorded `sha256`.
  Deliberately does not convert to a serving format (GGUF/ONNX/
  safetensors) — real, format-specific tooling with no named consumer
  yet, the same scope cut 0.11/3.5 got before either had one. New
  `odyssey model card`/`promote`/`export` commands. Closes Step 6.

- **New `evaluation/` workspace member (`odyssey-eval`), closing Step 7.**
  Offline evaluation harness — item 7.1's `runner.py` (load a
  `benchmarks/*.yaml` suite + a caller-produced completions JSONL + a
  metric, score, aggregate) and `harness.py` (write `reports/*.json`/`*.md`
  from `reports/templates/`). No live model-serving path exists in this
  repo, so the harness never calls a model itself — same "operate on an
  already-produced shard" shape `odyssey sft`/`odyssey dpo` already have.
  `judges.py` (LLM-as-judge, named in `docs/STRUCTURE.md`) is deliberately
  **not built** — same explicit-deferral treatment items 0.11/3.5 got
  before a named consumer existed.
- `eval_datasets.py` (item 7.2) — frozen eval set manifests/registry/cards,
  mirroring `data_preparation`'s `datasets.py` shape minus
  `recipe_hash`/`curated_watermark` (not applicable to a frozen/hand-built
  eval set). "Frozen" is enforced by the no-overlap gate, not
  write-protection here.
- `metrics/exact_match.py` and `metrics/tool_call_accuracy.py` (item 7.3) —
  tracked metric implementation code outside the installable package,
  loaded dynamically by `runner.load_metric` so a new metric needs no
  package release; `tool_call_accuracy` reuses `JourneyMetrics.
  tool_error_rate` rather than re-deriving it.
- `overlap.py`'s `check_no_overlap()` (item 7.4) — reuses
  `odyssey_dataprep.validation.check_leakage` directly for the "an eval-set
  journey id must never also appear in a training split" gate.
  `audit.py`'s `audit_registry()` additionally verifies every registered
  corpus/eval-set manifest's `sha256` still matches its on-disk file. New
  `dataset-audit.yml` CI workflow runs the audit; exits 3 on either kind of
  breach, same lineage-violation exit code `odyssey data validate`/`odyssey
  eval check-overlap` use. New `ci-eval.yml`, path-filtered on
  `evaluation/**` + `packages/odyssey-core/**` + `data_preparation/**`.
  New `odyssey eval run/compare/build-set/card/check-overlap` CLI commands.

- **New `packages/odyssey-schemas` and `services/api` workspace members,
  closing items 8.1-8.3.** `odyssey-schemas` is a pure pydantic-DTO
  package (no `fastapi`/`odyssey-core` dependency) — a stable wire
  contract a future generated SDK (item 8.4, not built) can depend on
  independently of the service. `services/api` (`odyssey-api`) is a new
  FastAPI read service layered `routers/` (parse/validate/render) ->
  `domain/` (use-cases, zero fastapi imports) -> `repositories/
  filesystem.py`, exposing `GET /health`, `/journeys`+`/journeys/{id}`
  (via `odyssey.export.fold_shard`, the same folding path every exporter
  uses), `/datasets`+`/datasets/{name}`, `/models`+`/models/{name}`
  (reading `data_preparation`'s/`training`'s own `registry.yaml` files
  directly), `/runs` (`odyssey eval run`'s own report JSON), and
  `/exports` (a caller-configured directory of `*.jsonl` shards, sha256/row
  count computed fresh). New `odyssey api serve/openapi/routes` CLI
  commands; `services/api/openapi.json` generated and committed.
  **Deliberately not built**, same explicit-deferral treatment `judges.py`
  (item 7) got: `repositories/mongo.py`/`postgres.py`/`objectstore.py`
  (only `filesystem.py`), `workers/drain_consumer.py` (no Kafka anywhere
  in this repo), `migrations/` (alembic — no relational schema). This
  service is a pure read layer — `services/collector` (item 1.8) keeps
  owning ingest; merging "8.2 and 1.8 are the same server" (flagged in
  `docs/WORKING.md`) was deliberately not attempted, since it would mean
  rewriting the collector's idempotency/project-scoping/backoff handling
  into FastAPI for no functional gain today. 25 new tests; full workspace
  (919 tests, 8 members) re-verified green.

- **New `sdk/python` workspace member (`odyssey-sdk`), closing items
  8.4/8.7.** `client.py`/`errors.py`/`models.py`/`codegen.py` are
  hand-written; `resources/{journeys,datasets,models,runs,exports}.py`
  are generated from `services/api/openapi.json` by `odyssey_sdk.codegen`
  — one resource class per path group (`client.journeys.list()`/
  `.get(id)`, etc.), each method returning an `odyssey_schemas` DTO
  parsed via `.model_validate()`. `Transport` is stdlib `urllib` only
  (mirrors `odyssey.sinks.HttpSink`'s own choice) — this package depends
  on `odyssey-schemas` only at runtime, not `odyssey-core`/`odyssey-api`,
  so it stays usable by someone with only network access to a deployed
  `services/api`. New `odyssey sdk codegen`/`check-drift` CLI commands;
  `services/api`'s own `odyssey api openapi` gained `--check` (exits 3 on
  drift). New `scripts/codegen.sh` (regenerates both, in order) and
  `codegen-drift.yml`/`ci-sdk.yml` CI workflows. The `docs/WORKING.md`
  naming-collision flag (`odyssey-sdk` vs. "the SDK" people mean when
  they say `odyssey-core`) is resolved by documentation
  (`sdk/python/README.md`), not a rename — `STRUCTURE.md`'s names stay as
  specified. 11 new tests (client tests run against a real live
  `services/api` instance via `uvicorn`, not a mock); full workspace (926
  tests, 9 members) green.

- **New `apps/web` workspace (Next.js 16, App Router, TypeScript), closing
  item 8.6.** Read-only dashboard over `services/api`:
  `journeys`/`datasets`/`models`/`runs`/`exports` list pages plus a
  journey-detail page, adapted from `docs/STRUCTURE.md`'s
  `journeys/datasets/experiments/models/reports` page list to the
  resources `services/api` actually exposes. Every page is a React Server
  Component (`fetch(..., {cache: "no-store"})`, no client-side
  data-fetching library). `src/lib/api/{types,client}.ts` is a
  hand-written stand-in for `@odyssey/sdk` (`sdk/javascript`, item 8.5 —
  not built this pass); `docs/STRUCTURE.md`'s "consumes `@odyssey/sdk`,
  NOT its own generated client" rule is a documented, temporary scope cut
  here, not the intended end state. New `ci-web.yml`. 3 new vitest unit
  tests; verified end-to-end against a real `uvicorn`-served `services/api`
  instance via `npm run dev` + `curl` (no browser tool available in this
  environment — documented in `apps/web/README.md`'s "Tests" section,
  `tests/e2e/` stays empty until one exists).

- **New `sdk/javascript` workspace member (`@odyssey/sdk`), closing item
  8.5 — Step 8 (`api → sdk → web`) has no open items left.** Mirrors
  `sdk/python` 1:1: `client.ts`/`errors.ts`/`codegen.ts` are hand-written,
  `types.generated.ts` + `resources/{journeys,datasets,models,runs,
  exports}.ts` are generated from `services/api/openapi.json` by
  `src/codegen.ts` — same narrowness as the Python generator (`GET`-only,
  ≤1 path param, one object/array-of-object response). `tsup` builds
  ESM+CJS+`.d.ts`. Converted the whole JS side of the repo to a single
  root `pnpm-workspace.yaml`/`pnpm-lock.yaml` (`apps/web` and
  `sdk/javascript` as members), replacing `apps/web`'s prior standalone
  npm setup — pnpm is available via corepack in this environment, closing
  the deviation `docs/WORKING.md`/`docs/NEXT.md` had flagged. New
  `pnpm --filter @odyssey/sdk codegen`/`codegen:check` CLI entry points;
  `scripts/codegen.sh` and `codegen-drift.yml` now regenerate/check all
  three codegen'd artifacts (openapi.json, sdk/python, sdk/javascript) in
  sequence. `ci-sdk.yml` gained the `js` matrix leg `docs/STRUCTURE.md`
  names ("sdk/** (py + js matrix)"). 7 new tests (client tests run
  against a real `uv run odyssey api serve` child process, not a mocked
  `fetch`).
- **`apps/web` now consumes `@odyssey/sdk` directly**, closing the scope
  cut from item 8.6 — `src/lib/api/{types,client}.ts` (hand-written
  stand-in) is gone; `src/lib/api/index.ts` is now a single `apiClient()`
  wrapping `OdysseySDK`, and every page imports `@odyssey/sdk`'s own
  types. `ci-web.yml` installs via the root pnpm workspace instead of
  `npm ci`. Re-verified end to end the same way item 8.6 originally was:
  a real `uvicorn`-served `services/api` + `pnpm dev` + `curl` against
  every route (including the 404 path), resolved SSR HTML inspected for
  real API data.
- **New `docs/COMPONENTS.md`** — one page per app/service/package: what it
  does, its real CLI/API surface (verified against each member's
  `pyproject.toml` entry points and `cli.py` registrations, not just
  README prose), how to run it, and its deliberate scope cuts. Root
  `README.md` gained a "Run the whole stack" section (collector → api →
  sdk → web, plus the training/eval pipeline) and a full "Documentation"
  index linking every doc in the repo, topic-grouped and priority-ordered.
  Fixed several member READMEs (`cli`, `data_preparation`, `evaluation`,
  `packages/odyssey-schemas`, `sdk/python`, `services/collector`) that
  still described an earlier, unbuilt state (e.g. "services/api not built
  yet", "only normalization exists") even though those items had long
  since shipped. README's Phases checklist and layout table also updated
  to match `docs/WORKING.md`'s real Steps 3-8 scorecard (all ✅).

- **Four new top-level docs**, closing the gap between `docs/STRUCTURE.md`'s
  original plan and what actually got written: `docs/architecture.md`
  (the two pipelines — capture→serve, train→evaluate — and the design
  principles behind every member), `docs/journey-schema.md` (the
  `JourneyEvent` wire format, field by field, cross-referenced against
  `primitives.py`/`fold.py`), `docs/data-contracts.md` (the
  `odyssey_schemas` → `openapi.json` → both SDKs codegen/drift-check
  chain, including the narrowness both generators share), and
  `docs/model-lifecycle.md` (corpus → training config → checkpoint →
  registered model → evaluation, as a sequence of real `odyssey`
  commands). Root `README.md`'s Documentation index and
  `docs/COMPONENTS.md` both updated to link them.
- **`sdk/examples/{python,javascript}` are runnable now**, closing the
  README Phases checklist's last stub note. `basic_usage.py` and
  `basic-usage.mjs` walk through the same sequence in both languages
  (`health()`, `journeys.list()`/`.get()`, a 404, then
  `datasets`/`models`/`runs`/`exports`) against a real `services/api`
  instance, not mocked — both verified locally against a live `uvicorn`
  instance before committing. `sdk/examples/README.md` covers setup;
  linked from both SDKs' own READMEs.

### Fixed

- `ci-web.yml` now builds `@odyssey/sdk` (`pnpm --filter @odyssey/sdk
  build`) before apps/web's lint/test/build steps — `apps/web` imports
  `@odyssey/sdk`'s `package.json` `exports`, which point at `dist/`,
  never built in CI before this; `pnpm test` failed with "Failed to
  resolve entry for package @odyssey/sdk". Reproduced locally
  (`rm -rf sdk/javascript/dist`) before fixing.

### Removed

- `primitives.TelemetryEvent` (item 1.11) — dead code targeting a
  `push_events()` pipeline and a `POST /api/v1/telemetry/events` backend,
  neither of which exists anywhere in this repo. `Telemetry` (no suffix,
  `JourneyEvent.telemetry`) is unrelated and unaffected.
