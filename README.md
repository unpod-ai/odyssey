# odyssey

Training-data framework — agent traces in, training corpora out.

Extracted from `super.ai/super @ odyssey-v1` (subdir `odyssey/`) with history preserved. The library
that came across is at `packages/odyssey-core`. Every member below the table now has real, tested
code — see the phase/step checklists further down for what's still genuinely open.

## Layout

| Path | What it is | State |
|---|---|---|
| `packages/odyssey-core` | journey schema, fold, JSONL codec, spool, builders, CLI | **code, tested** |
| `packages/odyssey-schemas` | pydantic DTOs shared by API and pipelines; source of OpenAPI | **code, tested** |
| `cli` | the single `odyssey` entrypoint, lazy plugin dispatch | **code, tested** |
| `services/api` | FastAPI backend; emits `openapi.json` | **code, tested** |
| `services/collector` | high-write trace ingest, spool → object store | **code, tested** |
| `apps/web` | Next.js dashboard, consumes `@odyssey/sdk` | **code, tested** |
| `data_preparation` | collection → cleaning → normalization → annotation → augmentation → validation → splitting | **code, tested** |
| `training` | soup/soup-cli adapter, configs, experiment manifests | **code, tested** |
| `models` | model registry + cards. **Not** weight storage | **code, tested** |
| `evaluation` | harness, benchmarks, metric code, frozen eval sets | **code, tested** |
| `sdk/python`, `sdk/javascript` | generated clients over `openapi.json` | **code, tested** |
| `datasets` | registry, cards, manifests — metadata only | **code, tested** |
| `data` | local scratch, gitignored | — |
| `infra`, `docs`, `openspec`, `scripts` | deployment, decisions, specs, repo tooling | scaffold |

Full tree and the rules behind it: [`docs/STRUCTURE.md`](docs/STRUCTURE.md).
What each app/service/package actually does today: [`docs/COMPONENTS.md`](docs/COMPONENTS.md).

## Artifacts are not in git

Git holds the recipe and the hash; the object store holds the bytes. `training/checkpoints`,
`training/logs`, `training/outputs`, `models/{pretrained,finetuned,exported}`, `evaluation/reports`
and `data/` are tracked as empty directories only. See [`docs/adr/0002-artifacts-out-of-git.md`](docs/adr/0002-artifacts-out-of-git.md).

## Lineage

```
raw traces (immutable)
  → data_preparation: collection → cleaning → normalization → annotation
                      → augmentation → validation → splitting
  → corpus         version = sha(recipe_hash + curated_watermark)
  → training       config sha + corpus version → checkpoint
  → models         registry entry: sha256 + base model + corpus version
  → evaluation     frozen eval set → report
  → services/api → sdk → apps/web
```

Every published artifact answers: which recipe, which corpus version, which config, which base model.

## Quickstart

Python 3.12 exactly — `requires-python = ">=3.12,<3.13"`. The upper bound is load-bearing: soup
(the trainer adapter this project targets) pins `>=3.10,<3.13`, the super workspace is `>=3.12`, and
the intersection is 3.12.

```bash
task setup           # uv sync --all-packages --extra dev
task check           # fmt + lint + types + tests
task test            # tests only
```

Core package directly:

```bash
cd packages/odyssey-core
bash scripts/run_tests.sh list        # test module map
bash scripts/run_tests.sh all
python -m odyssey.cli --spool .odyssey status
python -m odyssey.cli --spool .odyssey push --out ./out
```

Or via the installed `odyssey` console script (`cli/`, ADR 0003) — every
member's commands, discovered lazily:

```bash
odyssey --help
odyssey spool status --spool .odyssey
odyssey data normalize --raw ./raw_exports --format openai_chat --data-source demo --out ./normalized
odyssey doctor           # plugin discovery + cold-start timing
```

## Run the whole stack

One-time setup for everything (Python workspace + JS workspace):

```bash
task setup                          # uv sync --all-packages --extra dev
pnpm install                        # root pnpm workspace: apps/web + sdk/javascript
```

Each piece below is independent — run only what you need. Ports/dirs shown
are the defaults; every service is env-first (see each README for the full
var table).

### 1. Collector — ingest (`services/collector`)

Where `odyssey.HttpSink` posts traces. Start this if you're capturing data:

```bash
cd services/collector
uv sync --extra dev
uv run odyssey-collector --data-dir ./collector-data     # http://127.0.0.1:8787
```

```python
import odyssey
odyssey.init(sink=odyssey.HttpSink("http://127.0.0.1:8787"))
```

### 2. API — read (`services/api`)

Serves journeys/datasets/models/runs/exports read from the same
`<data_dir>/<date>/<journey_id>.jsonl` files the collector writes:

```bash
cd services/api
uv sync --extra dev
uv run uvicorn odyssey_api.main:app --host 127.0.0.1 --port 8000
# or: odyssey api serve --port 8000
```

Regenerate `openapi.json` + both SDKs from it in one shot after any schema
change: `./scripts/codegen.sh` (also gate-checked in CI via `codegen-drift.yml`).

### 3. SDKs — `sdk/python`, `sdk/javascript`

Generated clients over `services/api`'s `openapi.json`. Nothing to "run" —
install and call:

```python
# sdk/python — pip install odyssey-sdk, or uv sync in this workspace
from odyssey_sdk import OdysseySDK
client = OdysseySDK("http://127.0.0.1:8000")
client.journeys.list()
```

```ts
// sdk/javascript — pnpm add @odyssey/sdk (workspace:* inside this repo)
import { OdysseySDK } from "@odyssey/sdk";
const client = new OdysseySDK("http://127.0.0.1:8000");
await client.journeys.list();
```

### 4. Web dashboard (`apps/web`)

Next.js UI over the API, via `@odyssey/sdk` — needs the API running first:

```bash
ODYSSEY_API_BASE_URL=http://127.0.0.1:8000 pnpm --filter @odyssey/web dev
# http://localhost:3000
```

### 5. Training / evaluation — the "AI" pipeline (`training`, `evaluation`)

No live model-serving in this repo — `training` writes a `soup.yaml` config
for [soup-cli](https://trysoup.dev) to actually run (GPU box, separate
step), and `evaluation` scores completions you already produced. Both are
CLI-driven, nothing to leave running:

```bash
# turn an odyssey corpus into a soup-cli config, then train elsewhere
odyssey train sft-config --base meta-llama/Llama-3.1-8B-Instruct --shard sft.jsonl --out soup.yaml
soup train --config soup.yaml                 # on the GPU machine, separately

# score a completions file against a frozen benchmark (run from evaluation/)
odyssey eval run --benchmark benchmarks/example-arithmetic.yaml \
  --completions completions.jsonl
```

### All together

```
odyssey.init(HttpSink) → services/collector (:8787) → services/api (:8000)
                                                            ↓
                                     sdk/python, sdk/javascript ← apps/web (:3000)
```
`training`/`evaluation` read corpora and write reports independently — they
don't sit on this request path.

## Phases

- [x] **0** extract `odyssey/` → `packages/odyssey-core`, history preserved
- [x] **1** workspace root, gitignore contract, version pins, docs, ADRs
- [x] **2** `cli/` — root app, plugin registry, `spool` group; core's console script moved here (ADR 0003)
- [x] **3** `packages/odyssey-schemas` + `services/api` + `openapi.json` + `sdk/python`
- [x] **4** `data_preparation` stages over the existing fold/builders + `datasets/` registry
- [x] **5** `training` (soup adapter) + `models/registry.yaml` + `evaluation` harness
- [x] **6** `apps/web` + `sdk/javascript` (`sdk/examples/{python,javascript}` stay stubs — not a tracked Step 8 item)

Full item-by-item scorecard (0–9, including repo hygiene): [`docs/WORKING.md`](docs/WORKING.md).
The only open item left in the whole roadmap is `NOTICE` copyright holder
(9.4) — a governance decision, not engineering work; it blocks public
release.

## Documentation

Every doc in the repo, grouped by topic, each group ordered by how likely
you are to need it first.

**Start here**
1. This README — layout, quickstart, how to run the whole stack
2. [`docs/COMPONENTS.md`](docs/COMPONENTS.md) — what each app/service/package actually does
3. [`docs/WORKING.md`](docs/WORKING.md) — item-by-item build scorecard (✅/❌), the source of truth for "is X done"
4. [`docs/STRUCTURE.md`](docs/STRUCTURE.md) — the original proposed monorepo layout + the organiser rules everything else follows

**Per-component**
1. [`packages/odyssey-core/README.md`](packages/odyssey-core/README.md) — capture library: schema, fold, spool, builders
2. [`packages/odyssey-schemas/README.md`](packages/odyssey-schemas/README.md) — the wire-contract DTOs
3. [`cli/README.md`](cli/README.md) — the `odyssey` entrypoint, plugin dispatch
4. [`services/collector/README.md`](services/collector/README.md) — ingest (write side)
5. [`services/api/README.md`](services/api/README.md) — read API
6. [`sdk/python/README.md`](sdk/python/README.md) — generated Python client
7. [`sdk/javascript/README.md`](sdk/javascript/README.md) — generated TypeScript client
8. [`apps/web/README.md`](apps/web/README.md) — the dashboard
9. [`data_preparation/README.md`](data_preparation/README.md) — the 7-stage prep pipeline
10. [`training/README.md`](training/README.md) — soup-cli adapter + models registry CLI
11. [`evaluation/README.md`](evaluation/README.md) — offline eval harness

**Decisions (ADRs)**
1. [`docs/adr/0001-monorepo-layout.md`](docs/adr/0001-monorepo-layout.md)
2. [`docs/adr/0002-artifacts-out-of-git.md`](docs/adr/0002-artifacts-out-of-git.md)
3. [`docs/adr/0003-single-cli-entrypoint.md`](docs/adr/0003-single-cli-entrypoint.md)
4. [`docs/adr/0004-capture-layer.md`](docs/adr/0004-capture-layer.md)

**Project meta**
1. [`CONTRIBUTING.md`](CONTRIBUTING.md)
2. [`SECURITY.md`](SECURITY.md)
3. [`CHANGELOG.md`](CHANGELOG.md)
4. [`docs/NEXT.md`](docs/NEXT.md) — session handoff notes; useful for picking up where a prior session left off, not a stable reference

`docs/runbooks/` is still empty (`.gitkeep` only) — nothing to link yet.

## License

Apache-2.0. See `LICENSE` and `NOTICE` in `packages/odyssey-core`.
