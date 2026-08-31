# odyssey

Training-data framework — agent traces in, training corpora out.

Extracted from `super.ai/super @ odyssey-v1` (subdir `odyssey/`) with history preserved. The library
that came across is at `packages/odyssey-core`. `services/collector`, `cli`, and `data_preparation`
(normalization only) now have real code too; everything else in the tree is still scaffolding for the
phases below.

## Layout

| Path | What it is | State |
|---|---|---|
| `packages/odyssey-core` | journey schema, fold, JSONL codec, spool, builders, CLI | **code, tested** |
| `packages/odyssey-schemas` | pydantic DTOs shared by API and pipelines; source of OpenAPI | scaffold |
| `cli` | the single `odyssey` entrypoint, lazy plugin dispatch | **code, tested** |
| `services/api` | FastAPI backend; emits `openapi.json` | scaffold |
| `services/collector` | high-write trace ingest, spool → object store | **code, tested** |
| `apps/web` | Next.js dashboard, consumes `@odyssey/sdk` | scaffold |
| `data_preparation` | collection → cleaning → normalization → annotation → augmentation → validation → splitting | normalization done; rest scaffold |
| `training` | soup/soup-cli adapter, configs, experiment manifests | scaffold |
| `models` | model registry + cards. **Not** weight storage | scaffold |
| `evaluation` | harness, benchmarks, metric code, frozen eval sets | scaffold |
| `sdk/python`, `sdk/javascript` | generated clients over `openapi.json` | scaffold |
| `datasets` | registry, cards, manifests — metadata only | scaffold |
| `data` | local scratch, gitignored | — |
| `infra`, `docs`, `openspec`, `scripts` | deployment, decisions, specs, repo tooling | scaffold |

Full tree and the rules behind it: [`docs/STRUCTURE.md`](docs/STRUCTURE.md).

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
- [ ] **3** `packages/odyssey-schemas` + `services/api` + `openapi.json` + `sdk/python`
- [ ] **4** `data_preparation` stages over the existing fold/builders + `datasets/` registry — `normalization` done
- [ ] **5** `training` (soup adapter) + `models/registry.yaml` + `evaluation` harness
- [ ] **6** `apps/web` + `sdk/javascript` + `sdk/examples`

## License

Apache-2.0. See `LICENSE` and `NOTICE` in `packages/odyssey-core`.
