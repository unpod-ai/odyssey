"""odyssey-training CLI plugin — mounts `train sft-config`/`train dpo-config`
onto the odyssey CLI. See `soup_adapter`'s own docstring for what these
commands actually translate and why.
"""

from __future__ import annotations

import sys
from typing import Any


def register(app: Any) -> None:
    # pyrefly: ignore[missing-import]  — belongs to cli/, the only member
    # that actually depends on it; see odyssey_dataprep.cli's own comment.
    import typer  # noqa: PLC0415 - opt-in only when register() is called

    from odyssey_training.soup_adapter import (
        translate_dpo_shard,
        write_dpo_config,
        write_sft_config,
    )

    def sft_config(
        base: str = typer.Option(
            ..., "--base", help="HF model id, e.g. meta-llama/Llama-3.1-8B-Instruct"
        ),
        shard: str = typer.Option(
            ..., "--shard", help="an `odyssey sft` output *.jsonl"
        ),
        out: str = typer.Option(..., "--out", help="soup.yaml path to write"),
        output: str = typer.Option(
            "./output", "--output", help="soup train's own output dir"
        ),
        backend: str = typer.Option(
            "transformers", "--backend", help="transformers / unsloth / mlx"
        ),
    ) -> None:
        """Write a soup.yaml for an `odyssey sft` shard (item 5.6)."""
        path = write_sft_config(
            base=base, train_shard=shard, out_path=out, output=output, backend=backend
        )
        print(f"wrote {path}")

    def dpo_config(
        base: str = typer.Option(..., "--base", help="HF model id"),
        shard: str = typer.Option(
            ..., "--shard", help="an `odyssey dpo` output *.jsonl"
        ),
        out: str = typer.Option(..., "--out", help="soup.yaml path to write"),
        output: str = typer.Option(
            "./output", "--output", help="soup train's own output dir"
        ),
        backend: str = typer.Option(
            "transformers", "--backend", help="transformers / unsloth / mlx"
        ),
    ) -> None:
        """Translate an `odyssey dpo` shard and write its soup.yaml (item 5.6)."""
        translated = f"{shard}.soup.jsonl"
        n = translate_dpo_shard(shard, translated)
        path = write_dpo_config(
            base=base,
            train_shard=translated,
            out_path=out,
            output=output,
            backend=backend,
        )
        print(f"translated {n} pair(s) -> {translated}", file=sys.stderr)
        print(f"wrote {path}")

    @app.callback()
    def _group() -> None:
        """training stages: soup-cli config generation."""

    app.command("sft-config")(sft_config)
    app.command("dpo-config")(dpo_config)
