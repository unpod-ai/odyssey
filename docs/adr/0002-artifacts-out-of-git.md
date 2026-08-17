# ADR 0002 — Artifacts stay out of git

Status: accepted · Date: 2026-08-17

## Context

The tree contains directories whose natural contents are large, binary and rewritten on every run:
`training/checkpoints`, `training/logs`, `training/outputs`, `models/{pretrained,finetuned,exported}`,
`evaluation/reports`, and `data/{raw,interim,processed}`.

Committed, they inflate the pack permanently. Git stores every version forever, binary weights do not
delta-compress, shallow clones stop helping, and a mistake is unremovable without rewriting history for
everyone.

## Decision

**Git holds the recipe and the hash. The object store holds the bytes.**

Those directories are tracked as empty directories via `.gitkeep`; their contents are gitignored with an
explicit negation so the directory survives:

```gitignore
training/checkpoints/*
!training/checkpoints/.gitkeep
```

What git does track for each artifact class:

| Artifact | Tracked in git | Bytes live in |
|---|---|---|
| corpus | `datasets/manifests/<name>/v<N>.json`, `datasets/cards/`, the recipe yaml | object store `corpora/<name>/<version>/` |
| checkpoint | `training/experiments/<exp_id>.yaml` — config sha + corpus version + metrics ref | object store / MLflow |
| model | `models/registry.yaml` entry — sha256, base model, corpus version — plus `models/cards/` | object store / HF hub |
| eval report | metric code in `evaluation/metrics/`, `evaluation/reports/templates/` | object store; served by the API |
| training log | nothing | MLflow / W&B |

A manifest names shards, their sha256, and row counts, so a corpus is verifiable without being present.

Test fixtures are the exception: `packages/odyssey-core/tests/fixtures/golden_journey.jsonl` is committed
because it is tiny and a contract test depends on byte-exact content.

## Alternatives rejected

- **Commit the artifacts.** Unrecoverable repo growth; see above.
- **git-lfs on those paths.** Keeps `git`-native ergonomics, but every clone needs LFS configured, the
  quota is real money, and it still records one pointer version per run. Not chosen now; it remains the
  cheapest escape hatch if in-repo versioning is ever required.
- **DVC.** Solid fit for the corpus and model layers and worth revisiting when more than one person
  produces datasets. Rejected for now because our manifest already carries sha256 + row counts and the
  `dataset-audit` CI workflow verifies it — a second metadata system with no extra guarantee.

## Consequences

- A checkout is code and metadata only; nothing trains or serves without object-store credentials
  (`.env`, keys listed in the deployment runbook).
- Reproducibility rests on hashes, not on files being present. A corpus version is
  `sha(recipe_hash + curated_watermark)`; same inputs must yield the same bytes or the build is broken.
- Deleting an artifact from the store while its manifest stays in git leaves a dangling reference.
  `odyssey doctor` verifies lineage integrity, and `dataset-audit.yml` exits 3 on a breach.
