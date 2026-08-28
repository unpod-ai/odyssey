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

    from odyssey_training.checkpoints import upload_checkpoint
    from odyssey_training.experiments import write_experiment_manifest
    from odyssey_training.soup_adapter import (
        translate_dpo_shard,
        write_dpo_config,
        write_grpo_config,
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

    def grpo_config(
        base: str = typer.Option(..., "--base", help="HF model id"),
        prompts: str = typer.Option(
            ...,
            "--prompts",
            help="a chatml *.jsonl of prompts (odyssey has no GRPO exporter)",
        ),
        reward_fn: str = typer.Option(
            "accuracy",
            "--reward-fn",
            help="soup-cli built-in ('accuracy'/'format'/'verifiable'), a custom .py path, or a comma-separated ensemble",
        ),
        out: str = typer.Option(..., "--out", help="soup.yaml path to write"),
        output: str = typer.Option(
            "./output", "--output", help="soup train's own output dir"
        ),
        backend: str = typer.Option(
            "transformers", "--backend", help="transformers / unsloth / mlx"
        ),
    ) -> None:
        """Write a soup.yaml for GRPO (item 5.7) — see `write_grpo_config`'s
        docstring for why this needs a caller-supplied prompts shard."""
        path = write_grpo_config(
            base=base,
            prompts_shard=prompts,
            reward_fn=reward_fn,
            out_path=out,
            output=output,
            backend=backend,
        )
        print(f"wrote {path}")

    def record_experiment(
        exp_id: str = typer.Option(..., "--exp-id"),
        config: str = typer.Option(
            ..., "--config", help="the soup.yaml this run was launched with"
        ),
        corpus_version: str = typer.Option(
            ..., "--corpus-version", help="from `odyssey data corpus-version`"
        ),
        metrics: str = typer.Option(
            None, "--metrics", help="a metrics pointer, e.g. an MLflow/W&B run URL"
        ),
        checkpoint_uri: str = typer.Option(
            None,
            "--checkpoint-uri",
            help="from `odyssey train upload-checkpoint`'s own output",
        ),
        checkpoint_sha256: str = typer.Option(
            None,
            "--checkpoint-sha256",
            help="from `odyssey train upload-checkpoint`'s own output",
        ),
        experiments_root: str = typer.Option(
            "training/experiments", "--experiments-root"
        ),
        overwrite: bool = typer.Option(
            False, "--overwrite", help="replace an existing exp_id's manifest"
        ),
    ) -> None:
        """Write experiments/<exp_id>.yaml (item 5.8): config sha + corpus
        version + metrics ref (+ checkpoint pointer, item 5.9)."""
        path = write_experiment_manifest(
            exp_id,
            config_path=config,
            corpus_version=corpus_version,
            experiments_root=experiments_root,
            metrics_ref=metrics,
            checkpoint_uri=checkpoint_uri,
            checkpoint_sha256=checkpoint_sha256,
            overwrite=overwrite,
        )
        print(f"wrote {path}")

    def upload_checkpoint_cmd(
        checkpoint_dir: str = typer.Option(
            ..., "--dir", help="a `soup train` run's --output dir"
        ),
        bucket: str = typer.Option(..., "--bucket"),
        prefix: str = typer.Option(..., "--prefix", help="e.g. checkpoints/<exp_id>"),
        endpoint_url: str = typer.Option(
            None, "--endpoint-url", help="S3-compatible endpoint (MinIO, R2, ...)"
        ),
    ) -> None:
        """Upload a checkpoint dir's bytes to the object store (item 5.9).

        Prints the `uri`/`manifest_sha256` pair `odyssey train
        record-experiment --checkpoint-uri/--checkpoint-sha256` records —
        run this first, then feed its output into that command."""
        result = upload_checkpoint(
            checkpoint_dir, bucket, prefix, endpoint_url=endpoint_url
        )
        print(f"uploaded {len(result.files)} file(s) -> {result.uri}", file=sys.stderr)
        print(f"checkpoint_uri={result.uri}")
        print(f"checkpoint_sha256={result.manifest_sha256}")

    @app.callback()
    def _group() -> None:
        """training stages: soup-cli config generation."""

    app.command("sft-config")(sft_config)
    app.command("dpo-config")(dpo_config)
    app.command("grpo-config")(grpo_config)
    app.command("record-experiment")(record_experiment)
    app.command("upload-checkpoint")(upload_checkpoint_cmd)
