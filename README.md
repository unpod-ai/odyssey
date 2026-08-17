# odyssey

Training-data framework — agent traces in, training corpora out.

Extracted from `super.ai/super @ odyssey-v1` (subdir `odyssey/`) with history preserved. The library
that came across is at `packages/odyssey-core`; everything else in the tree is scaffolding for the
phases below and holds no code yet.

## Layout

| Path | What it is | State |
|---|---|---|
| `packages/odyssey-core` | journey schema, fold, JSONL codec, spool, builders, CLI | **code, tested** |
| `packages/odyssey-schemas` | pydantic DTOs shared by API and pipelines; source of OpenAPI | scaffold |
| `cli` | the single `odyssey` entrypoint, lazy plugin dispatch | scaffold |
| `services/api` | FastAPI backend; emits `openapi.json` | scaffold |
| `services/collector` | high-write trace ingest, spool → object store | scaffold |
| `apps/web` | Next.js dashboard, consumes `@odyssey/sdk` | scaffold |
| `data_preparation` | collection → cleaning → normalization → annotation → augmentation → validation → splitting | scaffold |
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

## Phases

- [x] **0** extract `odyssey/` → `packages/odyssey-core`, history preserved
- [x] **1** workspace root, gitignore contract, version pins, docs, ADRs
- [ ] **2** `cli/` — root app, plugin registry, `spool` group; core's console script moves here (ADR 0003)
- [ ] **3** `packages/odyssey-schemas` + `services/api` + `openapi.json` + `sdk/python`
- [ ] **4** `data_preparation` stages over the existing fold/builders + `datasets/` registry
- [ ] **5** `training` (soup adapter) + `models/registry.yaml` + `evaluation` harness
- [ ] **6** `apps/web` + `sdk/javascript` + `sdk/examples`

## License

Apache-2.0. See `LICENSE` and `NOTICE` in `packages/odyssey-core`.
