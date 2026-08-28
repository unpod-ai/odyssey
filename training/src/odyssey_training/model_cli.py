"""odyssey-training CLI plugin — mounts `model register`/`card`/`promote`/
`export` (items 6.1/6.2/6.4) onto the odyssey CLI as its own top-level
group, per `docs/STRUCTURE.md`'s command surface (`train` vs `model` are
separate groups even though both are currently backed by this same
package).
"""

from __future__ import annotations

from typing import Any


def register(app: Any) -> None:
    # pyrefly: ignore[missing-import]  — belongs to cli/, the only member
    # that actually depends on it; see odyssey_dataprep.cli's own comment.
    import typer  # noqa: PLC0415 - opt-in only when register() is called

    from odyssey_training.models_registry import (
        export_model,
        promote_model,
        register_model,
        resolve_model,
        write_model_card,
    )

    def register_cmd(
        name: str = typer.Option(..., "--name", help="registered model name"),
        sha256: str = typer.Option(
            ...,
            "--sha256",
            help="from `odyssey train upload-checkpoint`'s own manifest_sha256",
        ),
        uri: str = typer.Option(
            ..., "--uri", help="from `odyssey train upload-checkpoint`'s own uri"
        ),
        base_model: str = typer.Option(
            ..., "--base-model", help="e.g. meta-llama/Llama-3.1-8B-Instruct"
        ),
        corpus_version: str = typer.Option(
            ..., "--corpus-version", help="from `odyssey data corpus-version`"
        ),
        version: int = typer.Option(
            None, "--version", help="defaults to the next unused version"
        ),
        registry: str = typer.Option(
            "models/registry.yaml", "--registry", help="models/registry.yaml path"
        ),
    ) -> None:
        """Register a version of --name in models/registry.yaml (item 6.1):
        name -> version -> sha256 -> URI -> base model -> corpus version."""
        entry = register_model(
            registry,
            name,
            sha256=sha256,
            uri=uri,
            base_model=base_model,
            corpus_version=corpus_version,
            version=version,
        )
        print(f"registered {name} v{entry['version']} in {registry}")

    def card_cmd(
        name: str = typer.Option(..., "--name"),
        version: int = typer.Option(..., "--version"),
        license: str = typer.Option(..., "--license"),
        intended_use: str = typer.Option(..., "--intended-use"),
        limitations: str = typer.Option(..., "--limitations"),
        eval_summary: str = typer.Option(
            None, "--eval-summary", help="omit if not evaluated yet"
        ),
        registry: str = typer.Option("models/registry.yaml", "--registry"),
        cards: str = typer.Option("models/cards", "--cards", help="cards root"),
    ) -> None:
        """Write models/cards/<name>-v<version>.md (item 6.2)."""
        entry = resolve_model(registry, name, version=version)
        path = write_model_card(
            entry,
            name,
            cards,
            license=license,
            intended_use=intended_use,
            limitations=limitations,
            eval_summary=eval_summary,
        )
        print(f"wrote {path}")

    def promote_cmd(
        name: str = typer.Option(..., "--name"),
        version: int = typer.Option(..., "--version"),
        alias: str = typer.Option("production", "--alias"),
        registry: str = typer.Option("models/registry.yaml", "--registry"),
    ) -> None:
        """Point --alias at an already-registered --name/--version (item 6.4)."""
        result = promote_model(registry, name, version, alias=alias)
        print(f"{name}:{result['alias']} -> v{result['version']}")

    def export_cmd(
        name: str = typer.Option(..., "--name"),
        out: str = typer.Option(..., "--out", help="local directory to download into"),
        version: int = typer.Option(
            None, "--version", help="exactly one of --version/--alias"
        ),
        alias: str = typer.Option(
            None, "--alias", help="exactly one of --version/--alias"
        ),
        endpoint_url: str = typer.Option(
            None, "--endpoint-url", help="S3-compatible endpoint (MinIO, R2, ...)"
        ),
        registry: str = typer.Option("models/registry.yaml", "--registry"),
    ) -> None:
        """Download a registered model's checkpoint bytes to --out (item
        6.4) — verified against its registered sha256. Does not convert to
        a serving format (GGUF/ONNX/safetensors); see `export_model`'s own
        docstring for why that's a deliberate scope cut."""
        result = export_model(
            registry,
            name,
            out,
            version=version,
            alias=alias,
            endpoint_url=endpoint_url,
        )
        print(f"exported {len(result.files)} file(s) -> {out}")

    @app.callback()
    def _group() -> None:
        """model registry commands."""

    app.command("register")(register_cmd)
    app.command("card")(card_cmd)
    app.command("promote")(promote_cmd)
    app.command("export")(export_cmd)
