# Data contracts

How a shape defined once ends up typed in three languages, and how CI
proves none of them drifted apart.

## The chain

```
packages/odyssey-schemas       pydantic DTOs — the one place a response shape is hand-written
        │  services/api imports these for every request/response model
        ▼
services/api/openapi.json      generated: `odyssey api openapi --out services/api/openapi.json`
        │  committed to git — the single contract everything downstream reads
        ├──────────────────────────────┐
        ▼                              ▼
sdk/python/.../resources/*.py   sdk/javascript/src/{resources,types.generated}/*
generated: `odyssey sdk codegen`  generated: `pnpm --filter @odyssey/sdk codegen`
        │                              │
        ▼                              ▼
   any Python caller              apps/web (via @odyssey/sdk)
```

Nothing downstream of `openapi.json` is hand-maintained. If a route or a
DTO field changes, it changes in exactly one place
(`packages/odyssey-schemas` and/or `services/api/routers/`), and
everything below it is regenerated, not edited.

## Regenerating

```bash
./scripts/codegen.sh
```

Runs the three steps in dependency order — `odyssey api openapi` first
(services/api's live `FastAPI` app → `openapi.json`), then both clients
that read it (`odyssey sdk codegen`, `pnpm --filter @odyssey/sdk
codegen`). Running it twice back to back produces zero diff — the
generators are pure functions of `openapi.json`.

## The drift gate

`codegen-drift.yml` runs the `--check`/`check-drift` variant of each step
in CI, on every push/PR touching `services/api/**`,
`packages/odyssey-schemas/**`, `sdk/python/**`, or `sdk/javascript/**`:

```bash
uv run odyssey api openapi --check --out services/api/openapi.json   # exits 3 if stale
uv run odyssey sdk check-drift                                        # exits 3 if resources/*.py is stale
pnpm --filter @odyssey/sdk codegen:check                              # exits 3 if generated .ts is stale
```

Exit code `3` is this repo's contract-violation code (ADR 0003) — CI
greps for it specifically, the same code `data validate`/`eval
check-overlap` use for their own lineage gates. A committed
`openapi.json` or generated SDK file that doesn't match what its
generator would produce right now fails CI, full stop — there is no
"close enough."

## The narrowness both SDK generators share

`sdk/python/src/odyssey_sdk/codegen.py` and
`sdk/javascript/src/codegen.ts` implement the *same* generation rules,
independently, in two languages — verified identical by design, not by a
shared codegen engine:

- **`GET`-only.** No mutation endpoint has ever needed a generated client
  yet (`services/api` exposes none). A `POST`/`PUT`/`DELETE` operation in
  `openapi.json` would need the generator extended deliberately, not
  guessed at.
- **At most one path parameter.** Every real route today is either
  `/resource` (list) or `/resource/{id}` (get). A route with two path
  params has no established shape to generate against.
- **One object or array-of-object response per operation.** Anything
  else (a bare scalar, a union response) is outside what either
  generator will silently emit code for.

Outside this shape, both generators **raise** (`UnsupportedOperationError`
in Python, a thrown error in TS) rather than guess at a shape — a
generator that silently produces plausible-looking wrong code is worse
than one that stops and says so.

## Versioning policy

There isn't one yet, on purpose: nothing in this repo has had a versioned
release (`sdk/python` and `sdk/javascript` both sit at `0.1.0`,
`CHANGELOG.md` accumulates everything under `[Unreleased]`). A real
policy (semver contract for the DTOs, deprecation window for a removed
field) is worth writing once there's a first tagged release to version
against — writing one now would be speculative. Until then, the contract
is: `openapi.json` is committed and CI-gated, so any breaking change to
it is visible in the diff of that one file.

## Where the DTOs stop and the journey schema starts

`odyssey_schemas`' DTOs (`JourneySummaryOut`, `JourneyDetailOut`,
`JourneyMetricsOut`, ...) are **not** `JourneyEvent`/`Journey` re-exported
— see [`journey-schema.md`](journey-schema.md#where-this-schema-is-consumed).
They're independently defined, deliberately narrower views: every field
answers to one real source of truth elsewhere in the monorepo (a
dataclass in `odyssey.primitives`, or a `registry.yaml` entry), and the
DTO package adds no new data and no business logic of its own.
