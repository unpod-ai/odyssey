# @odyssey/sdk

Generated TypeScript client for `services/api` (item 8.5) — the JS twin of
`sdk/python` (item 8.4). `client.journeys`, `.datasets`, `.models`,
`.runs`, `.exports` are per-resource clients generated from
`services/api/openapi.json`; `client.health()` is the one hand-written
exception (nothing to list or fetch by id).

## Architecture

```
src/client.ts            hand-written: OdysseySDK, Transport (fetch, no HTTP framework dep)
src/errors.ts            hand-written: OdysseyAPIError / OdysseyAPINotFoundError
src/codegen.ts           hand-written: the generator itself
src/types.generated.ts   generated — do not hand-edit, see codegen.ts
src/resources/*.ts       generated — do not hand-edit, see codegen.ts
scripts/codegen.ts       CLI entry point (`pnpm codegen` / `pnpm codegen:check`)
```

Mirrors `sdk/python/src/odyssey_sdk/codegen.py` 1:1: both SDKs are
generated from the same `services/api/openapi.json`, with the same
narrowness — `GET`-only, at most one path parameter, a single object or
array-of-object response per operation. The generator raises rather than
silently guessing on anything outside that shape.

## Use it

```ts
import { OdysseySDK } from "@odyssey/sdk";

const client = new OdysseySDK("http://127.0.0.1:8000");
await client.health();
await client.journeys.list();
await client.journeys.get("j_123");
await client.datasets.list();
await client.models.get("my-model");
await client.runs.list();
await client.exports.list();
```

A 404 rejects with `OdysseyAPINotFoundError` (a subclass of
`OdysseyAPIError`, thrown for every other non-2xx response).

Full runnable walkthrough: [`sdk/examples/javascript/basic-usage.mjs`](../examples/javascript/basic-usage.mjs).
Build first because the example imports `dist/`:

```bash
pnpm --filter @odyssey/sdk build
node sdk/examples/javascript/basic-usage.mjs [base_url]
```

[`sdk/examples/README.md`](../examples/README.md) has the full setup steps.

## Regenerating `types.generated.ts` / `resources/*.ts`

```bash
pnpm --filter @odyssey/sdk codegen        # regenerate from services/api/openapi.json
pnpm --filter @odyssey/sdk codegen:check  # exit 3 if generated files are stale
```

Regenerating `services/api/openapi.json` itself is `odyssey api
openapi`'s job (item 8.3) — `scripts/codegen.sh` runs the Python and JS
codegen in sequence.

## Not done here

Only `GET` endpoints are supported by the generator today, matching
`services/api`'s actual surface (item 8.2) — see `codegen.ts`'s module
docstring for the exact narrowness this implies.

## Tests

```bash
pnpm --filter @odyssey/sdk test
```

Exercises this client against a real `services/api` instance started via
`uvicorn` as a child process, not a mocked `fetch`.
