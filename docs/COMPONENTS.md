# Components — what each app/service/package actually does

One page per member of the monorepo: what it's for, what's built, how to run
it, and what's deliberately left out. This is the as-built companion to
[`STRUCTURE.md`](STRUCTURE.md) (the original proposal) and
[`WORKING.md`](WORKING.md) (the line-by-line scorecard, ✅/❌ per item). If
this doc and a member's own `README.md` disagree, the member's `README.md`
and its code win — this is a map, not the territory.

State key: **live** = a process you run · **lib** = imported, nothing to run
· **CLI** = a `cli/`-mounted command group · **registry** = tracked
metadata files, no server.

```
odyssey.init(HttpSink) → services/collector (:8787) → services/api (:8000)
                                                            ↓
                                     sdk/python, sdk/javascript ← apps/web (:3000)

data_preparation → corpus → training → models registry → evaluation → reports
                                                              ↑
                                                    services/api serves all of the above
```

---

## packages/odyssey-core — the capture library (`odyssey`, lib)

The library half of odyssey, and the only member with `dependencies = []`
(stdlib only: `json`, `typing`, `dataclasses`, `pathlib`) — nothing above it
in the dependency graph can leak into it.

- `primitives.py` — `JourneyEvent`, the event schema everything else
  validates against.
- `fold.py` — folds a stream of events into a `Journey` projection.
- `jsonl.py` — the versioned JSONL wire codec (truncation handling,
  per-line rejection).
- `spool.py` — append-only local capture: per-journey watermark, secret
  redaction at record time, `drain()`.
- `context.py` — ambient journey context (`ContextVar`, asyncio-native) +
  `SeqAllocator` (disk-seeded, so a restart never reissues a `seq`).
- `client.py` / `config.py` — `odyssey.init()`.
- `capture.py` — `@observe` decorator, `with odyssey.journey(...)`.
- `integrations/` — drop-in capture wrappers for Anthropic, OpenAI (also
  covers OpenAI-compatible providers — Groq, Together, local vLLM/Ollama —
  for free, same SDK shape), and Gemini (own parser, different message
  shape).
- `builders/{journey,messages,metrics,reward,steps}.py` — trace →
  training-example assembly (SFT/DPO shard builders, message adapters for
  Anthropic/LangSmith shapes).
- `hashing.py` — stable content hashing.
- `cli.py` — `push`/`status`, the pre-`cli/` entrypoint; still callable via
  `python -m odyssey.cli`, now also mounted as `odyssey spool ...`.

**Run it**
```bash
cd packages/odyssey-core && uv sync --extra dev
bash scripts/run_tests.sh all
python -m odyssey.cli --spool .odyssey status
```

**Not done here**: streaming capture for OpenAI/Anthropic's
`generate_content_stream`-equivalents.

---

## packages/odyssey-schemas — the wire contract (`odyssey_schemas`, lib)

Pydantic DTOs for `services/api`. A pure wire contract, no business logic —
every field is a narrowed view of data whose real source of truth lives
elsewhere (`odyssey.primitives`, or a `registry.yaml` written by
`odyssey_dataprep`/`odyssey_training`/`odyssey_eval`). `services/api`'s
`openapi.json` is generated from these models, and both SDKs (`sdk/python`,
`sdk/javascript`) are generated from that `openapi.json` in turn — this
package is the root of that chain.

Split out from `services/api` itself specifically so a generated client
never has to depend on `fastapi`/`uvicorn` just to get the DTOs.

**Run it**: `cd packages/odyssey-schemas && uv sync --extra dev && uv run pytest tests`

---

## cli — the one `odyssey` entrypoint (`odyssey-cli`, CLI)

Owns the `odyssey` console script (see [ADR 0003](adr/0003-single-cli-entrypoint.md)).
`typer`/`rich` live here and only here, so every other member stays
dependency-light. Every command group below is a *plugin*, discovered
lazily through the `"odyssey.commands"` entry-point group — `registry.py`'s
`LazyGroup` reads entry-point names via `importlib.metadata` (no import) for
`odyssey --help`, and only imports the target module when that group is
actually dispatched. `odyssey --help` never imports torch.

**Full command surface today** (one row per member that registers a group):

| Group | Owned by | Commands |
|---|---|---|
| `spool` | odyssey-core | `push`, `export`, `sft`, `dpo`, `status`, `show`, `health` |
| `data` | odyssey-dataprep | `normalize`, `recipe-hash`, `corpus-version`, `build-corpus`, `card`, `collect`, `clean`, `queue`, `apply-reviews`, `augment`, `validate`, `split` |
| `train` | odyssey-training | `sft-config`, `dpo-config`, `grpo-config`, `record-experiment`, `upload-checkpoint` |
| `model` | odyssey-training | `register`, `card`, `promote`, `export` |
| `eval` | odyssey-eval | `run`, `compare`, `build-set`, `card`, `check-overlap` |
| `api` | odyssey-api | `serve`, `openapi`, `routes` |
| `sdk` | odyssey-sdk (python) | `codegen`, `check-drift` |
| — | cli itself | `doctor` (plugin discovery + cold-start timing), `--version` |

`odyssey push` / `odyssey status` survive as deprecated aliases of
`odyssey spool push` / `odyssey spool status` (warn to stderr).

**Run it**: `cd cli && uv sync --extra dev && uv run odyssey --help`

**Not done here**: `--profile`/`~/.odyssey/config.toml` scoped config,
`--dry-run`/lineage-refusal on mutating commands, shell completions, a
global `--json` flag (only `spool health` has one today).

---

## services/collector — ingest, the write side (`odyssey-collector`, live · :8787)

The endpoint `odyssey.HttpSink` posts to. Deliberately **stdlib, not
FastAPI** — the job is pure I/O (accept a JSONL POST, check a bearer token,
persist bytes), not a routing/validation/DTO problem.

- `POST /journeys/<id>/events` — single-journey ingest, `application/x-ndjson`.
- `POST /batch/events` — cross-journey batching in one request (optional
  gzip); always `200` once the envelope parses, each journey validated and
  stored independently so one bad journey never blocks the rest.
- `GET /health`, `GET /projects` (project-scoped mode only).
- Storage: local disk, date-partitioned —
  `<data_dir>/<YYYY-MM-DD>/<journey_id>.jsonl` (project-scoped:
  `<data_dir>/<slug>/<date>/...`), written through the same
  `odyssey.jsonl` codec `FileSink` uses — one wire-format parser, not two.
- Auth: single shared bearer token (`--api-key`) **or** a multi-tenant
  `--keys-file` roster (`{"projects": [{"slug","name","api_key"}]}`) —
  mutually exclusive. Project scoping is structural (each key's directory
  partition), not just an access check.

**Run it**: `cd services/collector && uv sync --extra dev && uv run odyssey-collector --data-dir ./collector-data`

**Not done here**: real multi-tenant key management (the keys-file is a
flat-file stopgap, edited + process-restarted to change), object-store
backing (still local disk only), any read path (that's `services/api`).

---

## services/api — the read API (`odyssey-api`, live · :8000)

The read side over the same files `services/collector` writes —
`GET /journeys`, `/journeys/{id}`, `/datasets`, `/datasets/{name}`,
`/models`, `/models/{name}`, `/runs`, `/exports`, `/health`. Read-only:
folds `<data_dir>/<date>/<journey_id>.jsonl` through
`odyssey.export.fold_shard`, the same path every exporter uses, and never
writes back into that directory.

```
routers/       parse/validate/render only — zero business logic
domain/        use-cases, zero fastapi imports
repositories/  storage adapters — filesystem.py is the only one built
```

`domain/` depends only on `repositories/` and the sibling members whose
files it reads (`odyssey`, `odyssey-dataprep`, `odyssey-training`,
`odyssey-eval`); `odyssey_schemas` supplies every request/response shape.

Env-first config (`ODYSSEY_API_JOURNEYS_DIR`,
`ODYSSEY_API_DATASETS_REGISTRY`, `ODYSSEY_API_MODELS_REGISTRY`,
`ODYSSEY_API_EVAL_REGISTRY`, `ODYSSEY_API_EVAL_REPORTS_DIR`,
`ODYSSEY_API_EXPORTS_DIR`, `ODYSSEY_API_HOST`/`PORT`); a missing
registry/dir returns empty (`{}`/`[]`), never an error.

**Run it**: `cd services/api && uv sync --extra dev && uv run uvicorn odyssey_api.main:app --port 8000` (or `odyssey api serve`).
`odyssey api openapi --out services/api/openapi.json` regenerates the
committed contract both SDKs are generated from.

**Not done here (deliberate)**: `repositories/{mongo,postgres,objectstore}.py`
(only `filesystem.py` is real), `workers/drain_consumer.py` (no Kafka
broker anywhere in this repo), `migrations/` (alembic — no relational
schema exists), the `odyssey db` CLI group. `services/collector` stays the
separate write side rather than merging into this service — merging now
would mean rewriting the collector's idempotency/backoff/project-scoping
into FastAPI for no functional gain today.

---

## sdk/python — generated Python client (`odyssey-sdk`, lib + CLI)

Generated HTTP client over `services/api/openapi.json`. **Not** the capture
layer — `odyssey-core` (package `odyssey`) is what people usually mean by
"the odyssey SDK"; this is a different, unrelated thing: a read client for
`services/api`, no dependency on `odyssey-core`, never touches the spool or
the JSONL wire format.

```
client.py      hand-written: OdysseySDK, Transport (stdlib urllib)
errors.py      hand-written: OdysseyAPIError / OdysseyAPINotFoundError
models.py      hand-written: re-exports odyssey_schemas DTOs under this namespace
codegen.py     hand-written: the generator itself
resources/*.py generated — do not hand-edit
```

```python
from odyssey_sdk import OdysseySDK
client = OdysseySDK("http://127.0.0.1:8000")
client.journeys.list()
```

Registers `odyssey sdk codegen` / `odyssey sdk check-drift` (exit 3 on
drift — ADR 0003's contract-violation code).

**Not done here**: only `GET` endpoints are generated (matches
`services/api`'s actual surface).

**Tests**: real `services/api` spun up via `uvicorn` in a background
thread, not a mocked transport.

---

## sdk/javascript — generated TypeScript client (`@odyssey/sdk`, lib)

The JS twin of `sdk/python`, generated from the same
`services/api/openapi.json` with the same narrowness (`GET`-only, ≤1 path
param, single object/array-of-object response — the generator raises
rather than guessing outside that shape).

```
src/client.ts            hand-written: OdysseySDK, Transport (fetch, no framework dep)
src/errors.ts            hand-written: OdysseyAPIError / OdysseyAPINotFoundError
src/codegen.ts           hand-written: the generator itself
src/types.generated.ts   generated
src/resources/*.ts       generated
scripts/codegen.ts       CLI entry (pnpm codegen / codegen:check)
```

```ts
import { OdysseySDK } from "@odyssey/sdk";
const client = new OdysseySDK("http://127.0.0.1:8000");
await client.journeys.list();
```

pnpm workspace member (root `pnpm-workspace.yaml`, one `pnpm-lock.yaml` for
the whole JS side). Built with `tsup` — ESM+CJS+`.d.ts`.

**Tests**: real `services/api` spun up via `uvicorn` as a child process,
not a mocked `fetch`.

---

## sdk/examples — runnable samples for both SDKs

`sdk/examples/python/basic_usage.py` and
`sdk/examples/javascript/basic-usage.mjs` — the same walkthrough in both
languages (`health()`, `journeys.list()`/`.get()`, a 404, then
`datasets`/`models`/`runs`/`exports`), run against a real `services/api`,
not mocked. `sdk/examples/README.md` has the setup steps.

---

## apps/web — the dashboard (`@odyssey/web`, live · :3000)

Read-only Next.js 16 (App Router, TypeScript) dashboard over
`services/api`: journeys, datasets, models, eval runs, exports.
`STRUCTURE.md` names the page set as
`{journeys,datasets,experiments,models,reports}` — actually shipped as
`{journeys,datasets,models,runs,exports}`, the resources `services/api`
really exposes; "experiments"/"reports" have no backing endpoint yet.

Consumes `@odyssey/sdk` directly — `src/lib/api/index.ts`'s `apiClient()`
is the one place that knows `ODYSSEY_API_BASE_URL` and builds an
`OdysseySDK`; every page imports that plus `@odyssey/sdk`'s own types.
Every page is a React Server Component (server-side fetch per request, no
client-side data-fetching library); a failed fetch renders inline, doesn't
crash the page.

**Run it**: `ODYSSEY_API_BASE_URL=http://127.0.0.1:8000 pnpm --filter @odyssey/web dev`

**Not done here**: browser/e2e tests (`tests/e2e/` stays empty) — verified
instead by curling every route against a real running API and inspecting
the server-rendered HTML.

---

## data_preparation — the 7-stage prep pipeline (`odyssey-dataprep`, lib + CLI)

Raw traces → curated, split corpus. Registers `odyssey data ...`. All 9
tracked sub-items are built:

| Stage | What it does |
|---|---|
| `collection/` | pulls from spool / collector / object store into one flat raw layer per journey |
| `cleaning/` | dedupe by `content_hash`, drop dead turns, repair encoding, opt-in PII scrub |
| `normalization/` | raw shapes → canonical `Journey`, via `odyssey-core`'s own `fold()` + BYOD builders (OpenAI/Anthropic/Vercel AI SDK formats) |
| `annotation/` | `build_queue` / `apply_reviews` — a local JSONL file is the review queue, no external system |
| `augmentation/` | deterministic tool-call perturbation (always on) + opt-in LLM-backed paraphrase / synthetic-negative generation |
| `validation/` | schema, PII-redaction, leakage, drift checks — `odyssey data validate` exits 3 on breach |
| `splitting/` | train/val/test by group key (`trace_id`), never by row — leak-safe |
| `flows/` | `run_recipe`, a stdlib sequencer (deliberately not Prefect — no scheduling/retry need to justify it) |
| `recipes/*.yaml` | declarative stage order; `recipe_hash` feeds corpus versioning |

Also owns the `datasets/` registry (`odyssey data build-corpus`/`card`) —
corpus `version = sha(recipe_hash + curated_watermark)`.

**Run it**: `cd data_preparation && uv sync --extra dev && uv run pytest tests`

---

## training — the soup-cli adapter (`odyssey-training`, lib + CLI)

Registers `odyssey train ...` and `odyssey model ...`. Never trains
anything itself and never imports torch — writes a `soup.yaml` for
[soup-cli](https://trysoup.dev) to actually run, elsewhere, on the GPU box.
Every config is validated against the real installed
`soup_cli.config.schema.SoupConfig` before being written.

- `train sft-config` / `dpo-config` / `grpo-config` — corpus shard → real,
  schema-valid `soup.yaml` (SFT needs no translation; DPO wraps
  chosen/rejected into one-element message lists; GRPO takes a
  caller-supplied prompts shard + a reward fn since odyssey has no GRPO
  exporter).
- `train record-experiment` — writes `experiments/<exp_id>.yaml` (config
  sha + corpus version + metrics pointer); refuses to clobber an existing
  `exp_id` without `--overwrite`.
- `model register` / `card` / `promote` / `export` — the `models/registry.yaml`
  entries + model cards (item 6.1/6.2/6.4).

**Run it**: `odyssey train sft-config --base <hf-id> --shard sft.jsonl --out soup.yaml`, then `soup train --config soup.yaml` on the GPU machine.

---

## models — model registry, not weight storage (registry)

`registry.yaml` (name → version → sha256 → URI → base model → corpus
version) + `cards/<model>-v1.md`. `pretrained/`, `finetuned/`, `exported/`
are `.gitkeep`-only — actual weights live in an object store / HF hub, git
holds the recipe and the hash. Managed via `odyssey model ...`
(see **training** above, which owns this registry's CLI).

---

## evaluation — the offline eval harness (`odyssey-eval`, lib + CLI)

Registers `odyssey eval ...`. Never calls a model — takes a benchmark
(`benchmarks/*.yaml`: prompts + references) and a completions file the
caller produced however they like (soup-cli run, raw API call, whatever),
scores the pairing.

- `src/odyssey_eval/runner.py` — load + score; `metrics/` are loaded
  dynamically (`runner.load_metric`), not imported into the package, so a
  new metric ships without a release. Includes `exact_match` and
  `tool_call_accuracy` (reuses `odyssey.primitives.JourneyMetrics`'
  `tool_error_rate`).
- `harness.py` — report writing (`reports/`, gitignored, `templates/`
  tracked).
- `eval_datasets.py` — frozen eval set manifests/registry/cards.
- `overlap.py` — the no-overlap gate: `odyssey eval check-overlap` proves a
  frozen eval set was never trained on.

**Not done here**: `judges.py` (LLM-as-judge scoring) — deliberately not
built; no live model-serving path existed in this repo when this member
was built.

**Run it**: `odyssey eval run --benchmark benchmarks/example-arithmetic.yaml --completions completions.jsonl` (from `evaluation/`).

---

## datasets — dataset registry, metadata only (registry)

`registry.yaml` (name → versions → manifest sha → URI) + `cards/<name>-v1.md`
(provenance, license, PII, splits, intended use) + `manifests/<name>/v1.json`
(shards + sha256 + row counts + recipe_hash). Zero payload bytes — actual
shard data lives in an object store. Written by `data_preparation`'s
`build-corpus`/`card` commands.

---

## infra, docs, openspec, scripts

- `scripts/codegen.sh` — the one script that regenerates the whole chain in
  order: `services/api/openapi.json` → `sdk/python` → `sdk/javascript`.
  `codegen-drift.yml` runs it in `--check` mode in CI.
- `docs/` — this file, `architecture.md` (system-level design),
  `journey-schema.md` (the wire format field by field),
  `data-contracts.md` (the codegen/drift-check chain),
  `model-lifecycle.md` (corpus → training → registry → eval, in
  sequence), `environment-variables.md` (every `ODYSSEY_*` var, grouped
  by which process reads it), `STRUCTURE.md` (the original proposal),
  `WORKING.md` (the real item-by-item scorecard), `NEXT.md` (session
  handoff notes), `adr/` (numbered decisions),
  `runbooks/run-services.md` (gunicorn/systemd for
  `services/api`/`services/collector` — the one runbook this repo has;
  the rest of `docs/STRUCTURE.md`'s named runbooks stay unwritten, no
  concrete backfill/release process exists yet).
- `infra/`, `openspec/` — scaffolding; no concrete deployment target yet
  (no k8s/terraform in use, no accepted `openspec` change proposals).

---

## Everything, run together

See the root [`README.md`](../README.md#run-the-whole-stack) "Run the whole
stack" section for the actual commands — collector → api → sdk → web, plus
the training/eval pipeline, which reads/writes corpora and reports
independently of that request path.

## Current gaps

The only tracked item left open anywhere in the roadmap is **9.4 — `NOTICE`
copyright holder** (blocks public release, a governance decision not
engineering work). Everything else in `WORKING.md`'s Steps 0–9 is ✅. Each
member's own "Not done here" section above lists smaller, deliberate scope
cuts within already-shipped members — those are permanent design decisions,
not open checklist items.
