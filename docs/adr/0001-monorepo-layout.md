# ADR 0001 — Monorepo layout

Status: accepted · Date: 2026-08-17

## Context

`odyssey` lived as a subdirectory of the `super` repo on branch `odyssey-v1`: 36 files, ~2600 LOC,
self-contained with its own `pyproject.toml`, `Taskfile.yml`, `LICENSE` and `NOTICE`. It shared a repo
with ~2900 unrelated files (livekit agents, supervoice, superdialog, superbook) and a history of 7246
commits it had no relationship to.

The work ahead is not one library. It is a library, a backend, a frontend, a data-preparation pipeline,
a training harness, an evaluation harness, two SDKs and a CLI — one product, several release cadences.

## Decision

One repo, explicit tiers, dependencies pointing one direction only:

```
packages/   libs. importable, no side effects, no framework imports
services/   deployables. one process each, own Dockerfile
apps/       user-facing frontends
cli/        the single `odyssey` entrypoint
data_preparation/ training/ evaluation/    ML lifecycle stages, each a workspace member
models/ datasets/                          registries — metadata, never payload
sdk/        generated clients (python, javascript) + executable examples
infra/ docs/ openspec/ scripts/
```

Rules:

1. `packages/` imports nothing above it. `services/` never import each other. Shared code sinks into `packages/`.
2. `packages/odyssey-core` keeps `dependencies = []`. Heavy ML deps live only in `training/` and its extras.
3. One lockfile per ecosystem: `uv.lock` and `pnpm-lock.yaml`. No per-member lockfiles.
4. `services/api/openapi.json` is the only client contract. Both SDKs are generated from it; CI fails on drift.
5. `apps/web` consumes `@odyssey/sdk`. It never hand-rolls a second client.
6. CI is path-filtered per member. A web change does not run the pipelines suite.
7. Versioning: libs semver · services image tag from git sha · datasets and models immutable `v<N>`.

Workspace members are listed explicitly in the root `pyproject.toml` rather than by glob, so a scaffolded
directory without a `pyproject.toml` cannot break `uv sync`. A member is added in the same commit as its
`pyproject.toml`.

## Alternatives rejected

- **Leave it in `super`.** Every clone of a training-data library carries a voice-agent monolith and 7246
  foreign commits; CI has no way to scope to it.
- **A repo per component** (core, api, web, pipelines, sdk). Six repos for one product means version
  matrices, cross-repo PRs for one schema change, and a stale generated client as the normal state.
- **Flat `src/` for everything.** Nothing then prevents the API importing the trainer, or the trainer
  importing FastAPI. The tier boundary is the point.

## Consequences

- The `odyssey/` prefix disappears; imports stay `odyssey.*` because the package moved as `src/odyssey`.
- Root `pyproject.toml` is virtual (no `[project]`); nothing installs from the root.
- Nine path-filtered CI workflows to maintain instead of one — accepted, in exchange for scoped runs.
