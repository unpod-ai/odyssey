# Contributing

## Setup

Python 3.12 exactly (`>=3.12,<3.13` — see `README.md` for why the upper bound is load-bearing), Node 22.

```bash
task setup           # uv sync --all-packages --extra dev
task check           # fmt + lint + types + tests, all members
```

`uv` is the python toolchain, `pnpm` the JS one, `task` the runner. One lockfile per ecosystem —
`uv.lock` and `pnpm-lock.yaml` at the root. Never add a per-member lockfile.

## Adding a workspace member

A scaffolded directory is not a member until it has a `pyproject.toml`. Add both in the same commit:

1. write `<member>/pyproject.toml` (and `Taskfile.yml` if it has its own tasks),
2. add the path to `[tool.uv.workspace] members` in the root `pyproject.toml`,
3. add an `include` + a route in the root `Taskfile.yml`,
4. add a path-filtered workflow under `.github/workflows/`.

Members are listed explicitly, not globbed, so a half-built directory cannot break `uv sync`.

## Tier rules

- `packages/` imports nothing above it. `services/` never import each other. Shared code sinks into `packages/`.
- `packages/odyssey-core` stays `dependencies = []`. Heavy ML deps belong to `training/` and its extras.
- `services/api/openapi.json` is the only client contract. Regenerate both SDKs with `scripts/codegen.sh`;
  CI fails on drift.
- `apps/web` consumes `@odyssey/sdk`. Do not hand-roll a second client.

## Tests

Each member defines a test module map in `scripts/run_tests.sh`. New module → extend the `case`, not just
the `tests/` directory:

```bash
cd packages/odyssey-core
bash scripts/run_tests.sh list
bash scripts/run_tests.sh all
```

Type checking is **pyrefly**, the house checker — not mypy. Formatting is black + isort (`profile = "black"`),
linting flake8 at `--max-line-length=88`.

## Artifacts

Never commit weights, checkpoints, logs, reports or dataset payloads. Git holds the recipe and the hash; the
object store holds the bytes (`docs/adr/0002-artifacts-out-of-git.md`). The gitignored artifact directories
survive as `.gitkeep` — keep them that way.

Test fixtures are the one exception, and only while they stay small and a contract test depends on their
byte-exact content.

## Data and model changes

A dataset or model change lands with its metadata:

- a manifest under `datasets/manifests/<name>/v<N>.json` — shards, sha256, row counts, recipe hash,
- a card under `datasets/cards/` or `models/cards/` — provenance, license, PII posture, splits, intended use,
- a registry entry in `datasets/registry.yaml` or `models/registry.yaml`.

Published versions are immutable. A correction is a new version, never an edit.

Splitting is by session/group key, never by row — enforced in `data_preparation/.../validation/`. Eval sets
are frozen and excluded from every training recipe; `dataset-audit.yml` asserts no overlap and exits 3 on a breach.

## Commits and PRs

Conventional Commits, matching the history this repo carries over:

```
feat(dataprep): split by session key
fix(spool): resumed drain skipped the tail
docs(adr): record the CLI entrypoint decision
```

A PR that changes the layout, a tier boundary, or an artifact rule adds or amends an ADR under `docs/adr/`
and links it in the description.
