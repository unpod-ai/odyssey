# Architecture

The system in one page: what talks to what, and why it's shaped this way.
For what each individual piece does, see [`COMPONENTS.md`](COMPONENTS.md).
For the original proposed layout and the organiser rules, see
[`STRUCTURE.md`](STRUCTURE.md). For line-by-line build status, see
[`WORKING.md`](WORKING.md).

## Two pipelines, one schema

Everything in this repo is either **getting a `JourneyEvent` onto disk**
or **turning already-recorded `JourneyEvent`s into something trained,
evaluated, or displayed**. There is no third data shape — see
[`journey-schema.md`](journey-schema.md) for the wire format both
pipelines share.

```
CAPTURE & SERVE (live processes, request/response)

  caller process
    │  odyssey.init(sink=HttpSink(...))
    │  @observe / with odyssey.journey(...)
    ▼
  packages/odyssey-core          in-process: ambient context, local spool, drain
    │  HttpSink.send()/send_batch()  (JSONL over HTTP, gzip optional)
    ▼
  services/collector  (:8787)    stdlib HTTP receiver, writes
    │                            <data_dir>/<date>/<journey_id>.jsonl
    ▼
  (shared filesystem / object store)
    │  read-only, via odyssey.export.fold_shard
    ▼
  services/api  (:8000)          FastAPI read API: journeys/datasets/models/runs/exports
    │  openapi.json               │
    ▼                             ▼
  sdk/python, sdk/javascript     generated clients, GET-only
    │
    ▼
  apps/web  (:3000)              Next.js dashboard (RSC, server-fetches via @odyssey/sdk)


TRAIN & EVALUATE (CLI-driven, batch, no long-running process)

  raw traces (immutable, from the filesystem above)
    ▼
  data_preparation   collection → cleaning → normalization → annotation
                     → augmentation → validation → splitting
    ▼
  corpus             version = sha(recipe_hash + curated_watermark)
                     datasets/registry.yaml + cards/
    ▼
  training            odyssey train sft-config/dpo-config/grpo-config
                       → soup.yaml  (soup-cli runs separately, on the GPU box)
                       → experiments/<exp_id>.yaml (config sha + corpus version + metrics ref)
    ▼
  models               odyssey model register/card/promote/export
                       registry.yaml (sha256 + base model + corpus version) + cards/
    ▼
  evaluation            odyssey eval run --benchmark ... --completions ...
                       (never calls a model — scores a completions file the
                       caller produced however they like)
                       → reports/  (served back through services/api's /runs)
```

The two pipelines meet at `services/api`: it reads journeys from the
capture side and registries/reports from the train/eval side, through the
same read-only filesystem repository pattern (`repositories/filesystem.py`).

## Design principles that shape every member

1. **Event-sourced, not state-sourced.** The only thing ever written to
   disk or sent over a network is a `JourneyEvent`. Cumulative state
   (`Step[]`, a folded `Journey`) is always a read-time projection,
   computed by `fold()`, never persisted. See
   [`adr/0004-capture-layer.md`](adr/0004-capture-layer.md).
2. **One integration point.** `odyssey.init()` is the only thing a caller
   ever touches; everything downstream (collector, api, dataprep,
   training, eval) consumes what that one point already produced. No
   second capture path exists anywhere in the repo.
3. **Never block, never crash the host on the capture path.** Every
   capture failure is counted, not raised (`ODYSSEY_DEBUG=1` is the one
   opt-in exception). The local spool *is* the retry queue — no in-memory
   buffer to lose, no backoff scheduler to get wrong.
4. **`packages/` import nothing above them; `services/` never import each
   other.** Shared code sinks into `packages/`. `services/api` and
   `services/collector` are two separate deployables on purpose — merging
   them now would mean rewriting the collector's idempotency/backoff/
   project-scoping into FastAPI for no functional gain today (see
   `services/api/README.md`'s "Not the ingest endpoint").
5. **One wire contract, generated everywhere downstream.**
   `packages/odyssey-schemas` → `services/api/openapi.json` →
   `sdk/python`/`sdk/javascript` (codegen) → `apps/web`. CI fails on drift
   (`codegen-drift.yml`); nothing hand-maintains a shape that's supposed
   to be generated. See [`data-contracts.md`](data-contracts.md).
6. **Lineage is provable end to end.** Every published artifact (corpus,
   checkpoint, model, eval report) can answer *which recipe, which corpus
   version, which config, which base model* — see
   [`model-lifecycle.md`](model-lifecycle.md).
7. **Git holds the recipe and the hash; an object store holds the
   bytes.** `training/{checkpoints,logs,outputs}`, `models/{pretrained,
   finetuned,exported}`, `evaluation/reports`, `data/` are `.gitkeep`-only.
   See [`adr/0002-artifacts-out-of-git.md`](adr/0002-artifacts-out-of-git.md).
8. **One CLI, plugin-dispatched, lazily.** `odyssey --help` never imports
   torch. Every command group (`spool`, `data`, `train`, `model`, `eval`,
   `api`, `sdk`, ...) is a separate member's entry point, loaded only when
   dispatched. See [`adr/0003-single-cli-entrypoint.md`](adr/0003-single-cli-entrypoint.md).

## What's genuinely not built

No live model-serving path exists anywhere in this repo — `evaluation`
scores a completions file the caller produced however they liked (a
`soup-cli`-trained model run through any inference tool), it never calls
a model itself. No Kafka broker, no object-store integration, no
relational database — every registry/journey store `services/api` reads
is a real file on disk today. See each member's "Not done here" section
in [`COMPONENTS.md`](COMPONENTS.md) for the complete, per-member list of
deliberate scope cuts.
