# odyssey-schemas

Pydantic DTOs for `services/api` (item 8.1). This package is a pure wire
contract: every field is a deliberately narrowed view of data that already
has one real source of truth elsewhere in the monorepo — a dataclass in
`odyssey.primitives`, or a `registry.yaml` entry written by
`odyssey_dataprep.datasets` / `odyssey_training.models_registry` /
`odyssey_eval.eval_datasets`. It adds no new data and no business logic.

`services/api` imports these models for its request/response shapes;
`services/api/openapi.json` (item 8.3) is generated from them.

## Why a separate package, not just classes inside `services/api`

Per `docs/STRUCTURE.md`, this is the shape a generated OpenAPI client
(`sdk/python`, item 8.4 — not built yet) and, eventually, `apps/web`'s
TypeScript codegen both need to point at independently of the FastAPI
service itself — a client library must not depend on a deployable's
`fastapi`/`uvicorn` dependencies just to get the DTOs.

## Run it

```bash
cd packages/odyssey-schemas
uv sync --extra dev
uv run pytest tests
```
