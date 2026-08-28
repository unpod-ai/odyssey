# apps/web

Read-only dashboard over `services/api` (item 8.6): journeys, datasets,
models, eval runs, exports. Next.js 16 (App Router), TypeScript, npm.

`docs/STRUCTURE.md` describes this app's pages as
`{journeys,datasets,experiments,models,reports}` — adapted here to
`{journeys,datasets,models,runs,exports}`, the resources `services/api`
(items 8.1-8.3) actually exposes today. "experiments"/"reports" have no
backing endpoint yet.

## Not using `@odyssey/sdk`

`docs/STRUCTURE.md` says this app should consume `@odyssey/sdk`
(`sdk/javascript`, item 8.5), not its own generated client. That package
isn't built yet — this pass only covers `sdk/python` (8.4) + `apps/web`
(8.6), per explicit scope for this session. `src/lib/api/{types,client}.ts`
is a deliberate, temporary stand-in: hand-written TS types mirroring
`odyssey_schemas`, and a thin `fetch` wrapper. Replace both with
`@odyssey/sdk` imports the same commit item 8.5 lands — they should
shrink to nothing, not grow.

## Run it

```bash
cd apps/web
npm install
ODYSSEY_API_BASE_URL=http://127.0.0.1:8000 npm run dev
```

Every page is a React Server Component — data is fetched server-side per
request (`cache: "no-store"`), no client-side data-fetching library. A
failed fetch renders an inline error instead of crashing the page.

| Env var | Default |
|---|---|
| `ODYSSEY_API_BASE_URL` | `http://127.0.0.1:8000` |

## Tests

```bash
npm run test    # vitest — src/lib/api/client.ts against a mocked fetch
npm run build   # also type-checks (`tsc`) and prerenders every route
npm run lint    # eslint (flat config, eslint-config-next)
```

**No browser/e2e test runner is wired up** (`tests/e2e/` stays empty,
same "not done here" treatment as other deferred pieces in this repo) —
this was verified instead by starting a real `services/api` instance,
running `npm run dev` against it, and `curl`-ing every route
(`/`, `/journeys`, `/journeys/{id}`, `/datasets`, including the 404 path
for a missing journey) to confirm the server-rendered HTML actually
contains the live API's data, not by opening a browser. If Playwright/e2e
is wanted later, `tests/e2e/` is where it belongs.

## Architecture

```
src/app/(dashboard)/{journeys,datasets,models,runs,exports}/page.tsx   list pages
src/app/(dashboard)/journeys/[journeyId]/page.tsx                      detail page
src/components/       Nav, DataTable (shared across every list page)
src/lib/api/          types.ts + client.ts — see "Not using @odyssey/sdk" above
tests/unit/           vitest, no browser
tests/e2e/            empty — see "Tests" above
```
