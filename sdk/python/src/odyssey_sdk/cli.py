"""odyssey-sdk CLI plugin — mounts `sdk codegen`/`sdk check-drift`, per
`docs/STRUCTURE.md`'s command surface (`sdk codegen · check-drift
(scripts/codegen)`).

Only regenerates/checks `resources/*.py` against the already-committed
`services/api/openapi.json` — refreshing that file itself is
`odyssey api openapi`'s job (it needs to import the live FastAPI app,
which this package deliberately does not depend on at runtime). Combine
both in `scripts/codegen.sh` / CI for full drift coverage.
"""

from __future__ import annotations

from typing import Any


def register(app: Any) -> None:
    # pyrefly: ignore[missing-import]  — belongs to cli/, the only member
    # that actually depends on it; see odyssey_dataprep.cli's own comment.
    import typer  # noqa: PLC0415 - opt-in only when register() is called

    def codegen() -> None:
        """Regenerate `resources/*.py` from `services/api/openapi.json`."""
        from odyssey_sdk.codegen import generate

        for resource, path in generate().items():
            print(f"wrote {path} ({resource})")

    def check_drift() -> None:
        """Exit 3 (ADR 0003's contract-violation code) if `resources/*.py`
        no longer matches what the generator would produce right now."""
        from odyssey_sdk.codegen import check_drift as _check_drift

        drifted = _check_drift()
        if drifted:
            print(f"drifted: {', '.join(drifted)} — run `odyssey sdk codegen`")
            raise typer.Exit(code=3)
        print("no drift")

    @app.callback()
    def _group() -> None:
        """the generated Python client: regenerate it, check it's fresh."""

    app.command("codegen")(codegen)
    app.command("check-drift")(check_drift)
