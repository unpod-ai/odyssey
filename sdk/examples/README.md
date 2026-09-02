# sdk/examples

Runnable samples for both SDKs — real requests against a real
`services/api` instance, not mocked. `docs/STRUCTURE.md` calls this
"docs that can't rot": each example is exercised, not just read.

## 1. Start something to point them at

```bash
# a. an empty API is enough to see health/404 behavior:
cd services/api && uv sync --extra dev
uv run uvicorn odyssey_api.main:app --host 127.0.0.1 --port 8000

# b. to see real journeys/datasets/models/runs/exports too, also run the
#    collector and send it some data first — see the root README's
#    "Run the whole stack" section.
```

## 2. Run the examples (from the repo root)

```bash
uv run python sdk/examples/python/basic_usage.py [base_url]
```

```bash
pnpm --filter @odyssey/sdk build      # examples import its dist/ output
node sdk/examples/javascript/basic-usage.mjs [base_url]
```

Both default to `http://127.0.0.1:8000` when no URL is passed, and walk
through the same sequence: `health()`, `journeys.list()`/`.get()`, a 404
on a missing journey (`OdysseyAPINotFoundError`), then
`datasets`/`models`/`runs`/`exports`.

## Testing a live `services/collector` (write side)

`basic_usage.py`/`basic-usage.mjs` above are read-side (`services/api`)
walkthroughs against a server you start yourself. To send a real request
to an already-running collector — a QA box, staging, whatever — instead:

```bash
ODYSSEY_ENDPOINT=https://your-collector-host \
ODYSSEY_API_KEY=<product's api_key, if the server requires one> \
uv run python sdk/examples/python/qa_collector_smoke_test.py

# or pass them positionally:
uv run python sdk/examples/python/qa_collector_smoke_test.py \
    https://your-collector-host <api_key>
```

Posts one dummy, uniquely-timestamped journey via `odyssey.HttpSink` (the
same client `odyssey.init()` uses in production) and prints the result —
useful for confirming a deployment's endpoint/auth/data-dir are all
working, and (with `ODYSSEY_COLLECTOR_DEBUG=1` set on that box) for
watching the resulting request show up in `journalctl -u odyssey-collector -f`.

## Which SDK is which

- `sdk/python` (`odyssey-sdk`) — stdlib `urllib` transport, no extra deps
  beyond `odyssey-schemas`.
- `sdk/javascript` (`@odyssey/sdk`) — `fetch`-based, ESM+CJS.

Both are generated from the same `services/api/openapi.json` with the
same narrowness (`GET`-only, ≤1 path param) — see each SDK's own
`README.md` for regenerating them after an API change.
