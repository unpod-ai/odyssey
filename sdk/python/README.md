# odyssey-sdk

Generated Python client for `services/api` (item 8.4) — `client.journeys`,
`.datasets`, `.models`, `.runs`, `.exports` are per-resource clients
generated from `services/api/openapi.json`; `client.health()` is the one
hand-written exception (nothing to list or fetch by id).

## Not the capture layer

`odyssey-core` (distributed as `odyssey`) is what people usually mean when
they say "the odyssey SDK" — `odyssey.init()`, `HttpSink`, the recording
API. This package is a different thing: a generated HTTP client for
`services/api`'s *read* endpoints. It has no dependency on `odyssey-core`
and never touches the spool or the JSONL wire format. `docs/WORKING.md`
flagged this as a naming collision to settle before building this package;
the resolution is this README, not a rename — `docs/STRUCTURE.md` already
committed to the distribution name `odyssey-sdk` / package `odyssey_sdk`,
and nothing else in this repo actually collides with it (`odyssey-core`'s
own distribution name is `odyssey`).

## Architecture

```
client.py      hand-written: OdysseySDK, Transport (stdlib urllib, no HTTP framework dep)
errors.py      hand-written: OdysseyAPIError / OdysseyAPINotFoundError
models.py      hand-written: re-exports odyssey_schemas DTOs under this package's namespace
codegen.py     hand-written: the generator itself
resources/*.py generated — do not hand-edit, see codegen.py
```

## Use it

```python
from odyssey_sdk import OdysseySDK

client = OdysseySDK("http://127.0.0.1:8000")
client.health()
client.journeys.list()
client.journeys.get("j_123")
client.datasets.list()
client.models.get("my-model")
client.runs.list()
client.exports.list()
```

A 404 raises `OdysseyAPINotFoundError` (a subclass of `OdysseyAPIError`,
raised for every other non-2xx response).

## Regenerating `resources/*.py`

```bash
uv run odyssey sdk codegen        # regenerate from services/api/openapi.json
uv run odyssey sdk check-drift    # exit 3 if resources/*.py is stale (ADR 0003's contract-violation code)
```

Regenerating `services/api/openapi.json` itself is `odyssey api openapi`'s
job (item 8.3) — `scripts/codegen.sh` runs both in sequence.

## Not done here

Only `GET` endpoints are supported by the generator today, matching
`services/api`'s actual surface (item 8.2) — see `codegen.py`'s
module docstring for the exact narrowness this implies. The JS twin of
this package is `sdk/javascript` (`@odyssey/sdk`, item 8.5) — a separate
package, generated the same way from the same `openapi.json`.

## Tests

```bash
uv run pytest tests
```

Exercises this client against a real `services/api` instance started via
`uvicorn` in a background thread (dev-only dependency — see
`pyproject.toml`'s `dev` extra), not a mocked transport.
