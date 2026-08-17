# odyssey — proposed monorepo structure (v2: + data_preparation / training / models / evaluation / sdk)

Source: the `super` repo, branch `odyssey-v1`, subdir `odyssey/` (36 files, ~2600 LOC), extracted with history.

Stack from parent-repo evidence: FastAPI + uvicorn + pydantic, Prefect flows, Mongo/Postgres/Redis/Kafka,
Next.js + TS, uv (python), pnpm (js), Taskfile runner. Trainer adapter = **soup / soup-cli** (cited in branch pyproject).

Legend: `port` from branch · `NEW` scaffold now · `LATER` stub + README · **`ignored`** = dir tracked via `.gitkeep`, contents gitignored.

```
odyssey/
├── .github/
│   ├── workflows/
│   │   ├── ci-core.yml               NEW  packages/**
│   │   ├── ci-api.yml                NEW  services/**
│   │   ├── ci-web.yml                NEW  apps/**
│   │   ├── ci-dataprep.yml           NEW  data_preparation/**
│   │   ├── ci-training.yml           NEW  training/**  (lint+unit only; no GPU in CI)
│   │   ├── ci-sdk.yml                NEW  sdk/**  (py + js matrix)
│   │   ├── dataset-audit.yml         NEW  manifest sha + PII gate on datasets/**
│   │   └── release.yml               NEW  tag -> wheels, npm, images
│   ├── CODEOWNERS · PULL_REQUEST_TEMPLATE.md · ISSUE_TEMPLATE/     NEW
├── pyproject.toml                    NEW  uv workspace root
│                                          members = packages/*, services/*, data_preparation,
│                                                    training, evaluation, sdk/python
├── uv.lock · pnpm-workspace.yaml · package.json                    NEW
├── .python-version(3.12) · .nvmrc(22) · .gitattributes · .gitignore · .pre-commit-config.yaml
├── Taskfile.yml                      NEW  core:check · api:dev · web:dev · data:prep · train:run · eval:run
├── docker-compose.yml                NEW  mongo·postgres·redis·kafka·minio·mlflow (local)
├── README · CONTRIBUTING · CHANGELOG · SECURITY · LICENSE(port) · NOTICE(port)
│
├── docs/
│   ├── architecture.md · journey-schema.md · data-contracts.md · model-lifecycle.md
│   ├── adr/0001-monorepo-layout.md · adr/0002-artifacts-out-of-git.md
│   └── runbooks/{drain,backfill,train,release}.md
├── openspec/changes/add-journey-schema/design.md    NEW (cited by pyproject, absent on branch)
│
├── packages/                         ← libs. no side effects, no framework imports
│   ├── odyssey-core/                 port  extracted branch, untouched
│   │   ├── pyproject.toml (deps = [] — keep it that way) · Taskfile.yml
│   │   ├── src/odyssey/
│   │   │   ├── __init__.py    0 LOC  ** becomes public API surface **
│   │   │   ├── py.typed
│   │   │   ├── primitives.py  367    JourneyEvent schema
│   │   │   ├── fold.py        267    event fold + projection
│   │   │   ├── hashing.py      43
│   │   │   ├── jsonl.py       326    versioned codec
│   │   │   ├── spool.py       423    append-only capture, watermark, drain
│   │   │   ├── cli.py          96    `odyssey` entrypoint
│   │   │   └── builders/{journey,messages,metrics,reward,steps}.py
│   │   ├── scripts/{make_golden,reformat_equivalence}.py · run_tests.sh
│   │   └── tests/ (+ fixtures/golden_journey.jsonl)
│   └── odyssey-schemas/              NEW  pydantic DTOs shared everywhere; source of OpenAPI
│
├── services/                         ← deployables, one process each
│   ├── api/                          NEW  FastAPI
│   │   ├── src/odyssey_api/{main,settings,deps}.py
│   │   │   routers/{health,journeys,datasets,runs,models,exports}.py
│   │   │   domain/          use-cases, zero fastapi imports
│   │   │   repositories/{mongo,postgres,objectstore}.py
│   │   │   workers/drain_consumer.py       kafka -> spool drain
│   │   ├── migrations/ (alembic) · tests/{unit,integration}
│   │   ├── openapi.json   generated, committed — SDK + web codegen input
│   │   └── Dockerfile
│   └── collector/                   LATER trace ingest, spool -> object store
│
├── apps/
│   └── web/                          NEW  Next.js 15 + TS
│       ├── src/app/(dashboard)/{journeys,datasets,experiments,models,reports}/page.tsx
│       ├── src/components/ · src/hooks/ · src/lib/
│       ├── src/lib/api/ -> consumes @odyssey/sdk (sdk/javascript), NOT its own generated client
│       └── tests/{unit,e2e} · Dockerfile
│
├── data_preparation/                 ← uv member. name "odyssey-dataprep", pkg odyssey_dataprep
│   ├── pyproject.toml · Taskfile.yml
│   ├── src/odyssey_dataprep/
│   │   ├── collection/       trace pull from spool/kafka/object store -> raw layer (immutable)
│   │   ├── cleaning/         dedupe, dead-turn drop, encoding repair, PII scrub
│   │   ├── normalization/    schema coercion via odyssey-core fold; role/message canon form
│   │   ├── annotation/       label & reward attach; human-in-loop queue adapters
│   │   ├── augmentation/     paraphrase, synthetic negatives, tool-call perturbation
│   │   ├── validation/       contract tests: schema, leakage, distribution drift, PII assert
│   │   ├── splitting/        train/val/test by group key (session), not by row — leak-safe
│   │   ├── flows/            Prefect orchestration wiring the stages above
│   │   └── recipes/*.yaml    declarative, hashed — recipe_hash is part of corpus version
│   └── tests/                per-stage unit + one end-to-end on golden fixture
│
├── training/                         ← uv member. name "odyssey-training", pkg odyssey_training
│   ├── pyproject.toml                heavy extras isolated here (torch/trl/peft/soup-cli)
│   ├── src/odyssey_training/{data,adapters/soup.py,launch.py,callbacks/}.py
│   ├── configs/                      TRACKED yaml/hydra: base.yaml, sft/*.yaml, dpo/*.yaml, grpo/*.yaml
│   ├── scripts/                      TRACKED launchers: train.sh, sweep.sh, resume.sh
│   ├── experiments/                  TRACKED manifests only: <exp_id>.yaml = config sha + corpus ver + metrics ref
│   ├── checkpoints/   **ignored**    .gitkeep only -> object store / MLflow artifacts
│   ├── logs/          **ignored**    .gitkeep only -> MLflow / W&B
│   └── outputs/       **ignored**    .gitkeep only
│
├── models/                           ← registry, NOT weight storage
│   ├── registry.yaml                 TRACKED name -> version -> sha256 -> URI -> base model -> corpus ver
│   ├── cards/<model>-v1.md           TRACKED model card: data, eval, limits, license
│   ├── pretrained/    **ignored**    base weights cache (.gitkeep)
│   ├── finetuned/     **ignored**    run outputs (.gitkeep)
│   └── exported/      **ignored**    gguf/onnx/safetensors for serving (.gitkeep)
│
├── evaluation/                       ← uv member. name "odyssey-eval", pkg odyssey_eval
│   ├── pyproject.toml · src/odyssey_eval/{runner,judges,harness}.py
│   ├── datasets/                     TRACKED manifests + cards only; frozen eval sets, never trained on
│   ├── benchmarks/                   TRACKED suite defs yaml + task prompts
│   ├── metrics/                      TRACKED metric implementations (code, not numbers)
│   └── reports/       **ignored**    generated html/json (.gitkeep); templates/ tracked
│
├── cli/                              ← uv member "odyssey-cli", pkg odyssey_cli. THE one entrypoint
│   ├── pyproject.toml                typer + rich (core stays stdlib-only; deps live here)
│   │                                 [project.scripts] odyssey = "odyssey_cli.main:app"
│   ├── src/odyssey_cli/
│   │   ├── main.py                   root app, global flags, plugin discovery
│   │   ├── registry.py               loads entry-point group "odyssey.commands" LAZILY
│   │   ├── output.py                 human table vs --json; one renderer, every command
│   │   ├── config.py                 profiles: ~/.odyssey/config.toml + ODYSSEY_* env + flags
│   │   ├── errors.py                 exit-code map, no tracebacks unless -vv
│   │   └── commands/
│   │       ├── spool.py              push · status        (delegates to odyssey-core)
│   │       ├── data.py               prep pipeline stages (delegates to odyssey-dataprep)
│   │       ├── dataset.py            registry + manifests
│   │       ├── train.py              soup adapter          ← imports torch ONLY when invoked
│   │       ├── model.py              registry, export, promote
│   │       ├── eval.py               harness runs, compare
│   │       ├── api.py                serve, openapi dump, routes
│   │       ├── sdk.py                codegen, drift check
│   │       ├── db.py                 alembic passthrough
│   │       └── doctor.py             env + creds + lineage integrity
│   ├── completions/                  bash · zsh · fish (generated, committed)
│   └── tests/                        one golden --help snapshot per command group
│
├── sdk/
│   ├── python/                       NEW  uv member "odyssey-sdk", pkg odyssey_sdk
│   │   ├── src/odyssey_sdk/{client,models,resources/}.py  (generated from openapi.json + wrappers)
│   │   └── tests/
│   ├── javascript/                   NEW  pnpm member "@odyssey/sdk", tsup build, ESM+CJS
│   │   ├── src/{client,resources,types.generated.ts}
│   │   └── tests/
│   ├── examples/                     NEW  py + ts runnable samples, CI-executed (docs that can't rot)
│   └── docs/                         NEW  quickstart, auth, pagination, errors, versioning policy
│
├── datasets/                         ← METADATA ONLY, zero payload bytes
│   ├── registry.yaml                 name -> versions -> manifest sha -> URI
│   ├── cards/<name>-v1.md            provenance · license · PII · splits · intended use
│   └── manifests/<name>/v1.json      shards + sha256 + row counts + recipe_hash
├── data/              **ignored**    local scratch: raw/ interim/ processed/
│
├── infra/{docker,k8s,terraform,ci}   NEW
└── scripts/                          NEW  bootstrap, codegen (openapi -> sdk), release
```

## Why `checkpoints/ logs/ outputs/ models/* reports/` are `.gitkeep`-only

Weights and logs are large, binary, and rewritten every run. In git they blow the pack, break shallow clones,
and can never be deleted from history. Contract instead: **git holds the recipe and the hash; the store holds the bytes.**

| Kind | Git tracks | Bytes live in |
|---|---|---|
| corpus | manifest + recipe + card | object store (`corpora/<name>/<ver>/`) |
| checkpoint | `experiments/<exp_id>.yaml` + sha | object store / MLflow |
| model | `models/registry.yaml` + card | object store / HF hub |
| eval report | template + metric code | store; served by API |

Escape hatch if you want them versioned in-repo anyway: DVC or `git-lfs` on those paths — decide once, in ADR 0002.

## Data + model lineage (one chain, hashable end to end)

```
raw traces (immutable)
  -> data_preparation: collection -> cleaning -> normalization -> annotation
                       -> augmentation -> validation -> splitting
  -> corpus  v = sha(recipe_hash + curated_watermark)
  -> training: config sha + corpus v  -> checkpoint
  -> models/registry.yaml entry (sha256 + base + corpus v)
  -> evaluation: frozen eval set -> report
  -> services/api serves journeys, datasets, experiments, models
  -> sdk/{python,javascript} -> apps/web
```

Any published artifact answers: which recipe, which corpus version, which config, which base model. No answer = not publishable.

## CLI design

### Name collision — must resolve before extraction

`packages/odyssey-core/pyproject.toml` already declares `odyssey = "odyssey.cli:main"`. Two wheels claiming the
same console script means last-installed wins — a silent, environment-dependent break.

Resolution: **`cli/` owns the `odyssey` name.** Core drops its `[project.scripts]` block and instead exposes

```toml
[project.entry-points."odyssey.commands"]
spool = "odyssey.cli:register"
```

Its existing `argparse` parser stays intact (still callable as `python -m odyssey.cli`), so core keeps
working standalone with zero deps. `odyssey push` / `odyssey status` survive one minor as deprecated aliases
of `odyssey spool push` / `odyssey spool status`, printing a warning to stderr.

### Command surface

```
odyssey [--profile P] [--config F] [--json] [-v|-q] [--dry-run] [--version]
├── spool     push · status                                 (core)
├── data      collect · clean · normalize · annotate · augment · validate · split
│             prep (full chain) · recipe {ls,show,hash}      (dataprep)
├── dataset   ls · show · build · verify · publish · card    (datasets registry)
├── train     run · resume · sweep · ls                      (training)
├── model     ls · show · register · export · promote        (models registry)
├── eval      run · compare · report                         (evaluation)
├── api       serve · openapi · routes                        (services/api)
├── sdk       codegen · check-drift                           (scripts/codegen)
├── db        migrate · revision · downgrade                  (alembic passthrough)
└── doctor    env · store creds · lineage integrity
```

### Rules

1. **Zero logic in the CLI.** Each command parses args, calls a member's public API, renders. If a command
   grows a branch worth testing, that branch belongs in the member, not here.
2. **Lazy plugin loading.** `odyssey --help` must not import torch. `registry.py` reads entry-point metadata
   and imports the module only when its subcommand is dispatched. A `doctor` check asserts cold `--help` under 200 ms.
3. **Every command is scriptable.** `--json` on all of them; humans get tables, CI gets stable JSON.
4. **Exit codes:** `0` ok · `1` runtime failure · `2` usage error · `3` contract/lineage violation
   (leakage, missing manifest, drifted openapi). CI greps on `3` specifically.
5. **Mutations name their lineage.** `dataset publish`, `model promote`, `train run` refuse to proceed without
   recipe hash + corpus version resolved; `--dry-run` prints the resolved plan and exits 0.
6. `sdk/javascript` may later ship `npx @odyssey/cli` for frontend devs — read-only commands only, never a
   second implementation of the python one. Marked LATER.

## Organiser rules

1. `packages/` imports nothing above it. `services/` never import each other. Shared code sinks into `packages/`.
2. `odyssey-core` stays `dependencies = []`. Heavy ML deps live only in `training/` (and its extras).
3. One lockfile per ecosystem: `uv.lock` + `pnpm-lock.yaml`. No per-package lockfiles.
4. `services/api/openapi.json` is the single contract. `scripts/codegen` regenerates both SDKs; CI fails on drift.
5. `apps/web` consumes `@odyssey/sdk` — no second hand-rolled client.
6. Splitting is by session/group key, never by row. Enforced by a test in `data_preparation/validation/`.
7. Eval sets are frozen and excluded from every training recipe; `dataset-audit.yml` asserts no overlap.
8. Path-filtered CI per member; `training/` runs lint + unit only (no GPU runners).
9. Versioning: libs semver · services image tag from git sha · datasets & models immutable `v<N>`.

## Migration phases

- **0** extract `odyssey/` -> `packages/odyssey-core/`, history preserved (`git subtree split`).
- **1** root workspace + CI + README + `__init__.py` public API + `.python-version`.
- **2** `cli/` skeleton: root app, plugin registry, `spool` group ported off core's script + entry-point rename.
       Nothing else can be driven ergonomically until this exists.
- **3** `packages/odyssey-schemas` + `services/api` (health, journeys) + `openapi.json` + `sdk/python`
       + `odyssey api|sdk|db` command groups.
- **4** `data_preparation/` stages wrapping existing fold/builders + `datasets/` registry + first card
       + `odyssey data|dataset` groups.
- **5** `training/` (soup adapter, configs) + `models/registry.yaml` + `evaluation/` harness
       + `odyssey train|model|eval` groups.
- **6** `apps/web` dashboard + `sdk/javascript` + `sdk/examples`.
