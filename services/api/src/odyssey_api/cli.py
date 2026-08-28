"""odyssey-api CLI plugin — mounts `api serve`/`api openapi`/`api routes`
onto the odyssey CLI, per `docs/STRUCTURE.md`'s command surface. `sdk`/`db`
groups named alongside it there are not mounted here — they belong to
`sdk/python` (item 8.4, not built) and alembic migrations (not built, see
README's "Not done here"), neither of which exists yet.
"""

from __future__ import annotations

import json
from typing import Any


def register(app: Any) -> None:
    # pyrefly: ignore[missing-import]  — belongs to cli/, the only member
    # that actually depends on it; see odyssey_dataprep.cli's own comment.
    import typer  # noqa: PLC0415 - opt-in only when register() is called

    def serve(
        host: str = typer.Option(
            None, "--host", help="default: $ODYSSEY_API_HOST or 127.0.0.1"
        ),
        port: int = typer.Option(
            None, "--port", help="default: $ODYSSEY_API_PORT or 8000"
        ),
        reload: bool = typer.Option(
            False, "--reload", help="uvicorn autoreload (dev only)"
        ),
    ) -> None:
        """Run the API with uvicorn (item 8.2)."""
        import uvicorn

        from odyssey_api.settings import get_settings

        settings = get_settings()
        uvicorn.run(
            "odyssey_api.main:app",
            host=host or settings.host,
            port=port or settings.port,
            reload=reload,
        )

    def openapi(
        out: str = typer.Option(
            "services/api/openapi.json", "--out", help="where to write the schema"
        ),
    ) -> None:
        """Write `openapi.json` from the live app (item 8.3) — the single
        contract `scripts/codegen` (item 8.7, not built) would regenerate
        SDKs from."""
        from odyssey_api.main import create_app

        schema = create_app().openapi()
        with open(out, "w", encoding="utf-8") as f:
            json.dump(schema, f, indent=2, sort_keys=True)
            f.write("\n")
        print(f"wrote {out}")

    def routes() -> None:
        """List every mounted route — a quick sanity check without starting
        a server."""
        from odyssey_api.main import create_app

        app_ = create_app()
        # Newer starlette (>=1.x) wraps each `include_router()` call as an
        # opaque `_IncludedRouter` on `app.routes` instead of flattening its
        # routes in place — `.original_router.routes` is where the real
        # `APIRoute`s live. Walk both shapes so this keeps working across
        # the starlette version this repo happens to be pinned to.
        seen = []
        for route in app_.routes:
            original = getattr(route, "original_router", None)
            candidates = original.routes if original is not None else [route]
            seen.extend(candidates)
        for route in seen:
            methods = ",".join(sorted(getattr(route, "methods", None) or []))
            path = getattr(route, "path", "")
            if methods:
                print(f"{methods:10s} {path}")

    @app.callback()
    def _group() -> None:
        """the read API: serve it, regenerate its OpenAPI schema, list routes."""

    app.command("serve")(serve)
    app.command("openapi")(openapi)
    app.command("routes")(routes)
