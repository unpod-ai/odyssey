"""odyssey-training CLI plugin — mounts `model register` (item 6.1) onto the
odyssey CLI as its own top-level group, per `docs/STRUCTURE.md`'s command
surface (`train` vs `model` are separate groups even though both are
currently backed by this same package).
"""

from __future__ import annotations

from typing import Any


def register(app: Any) -> None:
    # pyrefly: ignore[missing-import]  — belongs to cli/, the only member
    # that actually depends on it; see odyssey_dataprep.cli's own comment.
    import typer  # noqa: PLC0415 - opt-in only when register() is called

    from odyssey_training.models_registry import register_model

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

    @app.callback()
    def _group() -> None:
        """model registry commands."""

    app.command("register")(register_cmd)
