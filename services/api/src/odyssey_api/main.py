"""The FastAPI app (item 8.2). `create_app()` is the one factory both
`uvicorn` (via `cli.py`'s `odyssey api serve`) and the test suite import —
no module-level app instance, so tests can build one per-settings without
env var juggling.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI

from odyssey_api.deps import require_api_key
from odyssey_api.routers import datasets, exports, health, journeys, models, runs


def create_app() -> FastAPI:
    app = FastAPI(
        title="odyssey-api",
        description="Read API for journeys/datasets/models/eval-runs. "
        "Ingest stays services/collector's job — see services/api/README.md.",
        version="0.1.0",
    )
    app.include_router(health.router)
    app.include_router(journeys.router, dependencies=[Depends(require_api_key)])
    app.include_router(datasets.router, dependencies=[Depends(require_api_key)])
    app.include_router(models.router, dependencies=[Depends(require_api_key)])
    app.include_router(runs.router, dependencies=[Depends(require_api_key)])
    app.include_router(exports.router, dependencies=[Depends(require_api_key)])
    return app


app = create_app()
