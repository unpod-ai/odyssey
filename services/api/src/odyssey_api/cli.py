"""odyssey-api CLI plugin — mounts `api serve`/`api openapi`/`api routes`
onto the odyssey CLI, per `docs/STRUCTURE.md`'s command surface. The `db`
group named alongside it there is not mounted here — it belongs to
alembic migrations (not built, see README's "Not done here"). The `sdk`
group is mounted by `sdk/python`'s own CLI plugin (item 8.4) — see its
`odyssey_sdk.cli` module.
"""

from __future__ import annotations

import json
from typing import Any, Optional


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
        api_key: Optional[str] = typer.Option(
            None,
            "--api-key",
            help="require this bearer token on every route except /health; "
            "default: open. Same as setting $ODYSSEY_API_AUTH_KEY",
        ),
    ) -> None:
        """Run the API with uvicorn (item 8.2)."""
        import os

        import uvicorn

        from odyssey_api.settings import get_settings

        if api_key is not None:
            os.environ["ODYSSEY_API_AUTH_KEY"] = api_key

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
        check: bool = typer.Option(
            False,
            "--check",
            help="don't write — exit 3 (ADR 0003's contract-violation code) if `out` is stale",
        ),
    ) -> None:
        """Write `openapi.json` from the live app (item 8.3) — the single
        contract `scripts/codegen.sh` (item 8.7) regenerates SDKs from."""
        from pathlib import Path

        from odyssey_api.main import create_app

        schema = create_app().openapi()
        rendered = json.dumps(schema, indent=2, sort_keys=True) + "\n"

        if check:
            current = (
                Path(out).read_text(encoding="utf-8") if Path(out).exists() else ""
            )
            if current != rendered:
                print(f"{out} is stale — run `odyssey api openapi --out {out}`")
                raise typer.Exit(code=3)
            print(f"{out} is fresh")
            return

        with open(out, "w", encoding="utf-8") as f:
            f.write(rendered)
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

    def reindex() -> None:
        """Force one full index pass (journeys + metrics + exports +
        reconciliation) right now, outside the background worker's
        interval -- useful right after a deploy or in scripts/tests."""
        from odyssey_store.db import connect

        from odyssey_api.index.exports_indexer import index_exports
        from odyssey_api.index.journeys_indexer import index_journeys
        from odyssey_api.index.metrics_indexer import index_metrics
        from odyssey_api.index.reconcile import reconcile
        from odyssey_api.settings import get_settings

        settings = get_settings()
        conn = connect(settings.db_uri)
        try:
            j = index_journeys(conn, settings.journeys_dir)
            m = index_metrics(conn, settings.journeys_dir)
            e = index_exports(conn, settings.exports_dir)
            removed = reconcile(conn)
        finally:
            conn.close()
        print(
            f"journeys indexed: {j}, metrics indexed: {m}, exports indexed: {e}, reconciled away: {removed}"
        )

    @app.callback()
    def _group() -> None:
        """the read API: serve it, regenerate its OpenAPI schema, list routes."""

    app.command("serve")(serve)
    app.command("openapi")(openapi)
    app.command("routes")(routes)
    app.command("reindex")(reindex)
