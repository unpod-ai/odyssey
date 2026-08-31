# apps/web

Read-only dashboard over `services/api` (item 8.6): journeys, datasets,
models, eval runs, exports. Next.js 16 (App Router), TypeScript, pnpm.

`docs/STRUCTURE.md` describes this app's pages as
`{journeys,datasets,experiments,models,reports}` — adapted here to
`{journeys,datasets,models,runs,exports}`, the resources `services/api`
(items 8.1-8.3) actually exposes today. "experiments"/"reports" have no
backing endpoint yet.

## Consumes `@odyssey/sdk`

Per `docs/STRUCTURE.md`, this app consumes `@odyssey/sdk`
(`sdk/javascript`, item 8.5) rather than its own generated client.
`src/lib/api/index.ts` is the only place that knows the base URL —
`apiClient()` builds an `OdysseySDK` from `ODYSSEY_API_BASE_URL`; every
page imports `apiClient` and `@odyssey/sdk`'s own types directly.

## Run it

```bash
pnpm install                                      # from the repo root — one pnpm workspace
ODYSSEY_API_BASE_URL=http://127.0.0.1:8000 pnpm --filter @odyssey/web dev
```

Every page is a React Server Component — data is fetched server-side per
request via `@odyssey/sdk`, no client-side data-fetching library. A
failed fetch renders an inline error instead of crashing the page.

| Env var | Default |
|---|---|
| `ODYSSEY_API_BASE_URL` | `http://127.0.0.1:8000` |

## Tests

```bash
pnpm --filter @odyssey/web test    # vitest — src/lib/api's apiClient() wrapper
pnpm --filter @odyssey/web build   # also type-checks (`tsc`) and prerenders every route
pnpm --filter @odyssey/web lint    # eslint (flat config, eslint-config-next)
```

**No browser/e2e test runner is wired up** (`tests/e2e/` stays empty,
same "not done here" treatment as other deferred pieces in this repo) —
this was verified instead by starting a real `services/api` instance,
running `pnpm dev` against it, and `curl`-ing every route
(`/`, `/journeys`, `/journeys/{id}`, `/datasets`, `/models`, `/runs`,
`/exports`, including the 404 path for a missing journey) to confirm the
server-rendered HTML actually contains the live API's data, not by
opening a browser. If Playwright/e2e is wanted later, `tests/e2e/` is
where it belongs.

## Architecture

```
src/app/(dashboard)/{journeys,datasets,models,runs,exports}/page.tsx   list pages
src/app/(dashboard)/journeys/[journeyId]/page.tsx                      detail page
src/components/       Nav, DataTable (shared across every list page)
src/lib/api/          index.ts — apiClient(), the one @odyssey/sdk entry point
tests/unit/           vitest, no browser
tests/e2e/            empty — see "Tests" above
```
