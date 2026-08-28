"""odyssey-eval CLI plugin — mounts `eval run/compare/build-set/card/check-overlap`
onto the odyssey CLI (`docs/STRUCTURE.md`'s "Command surface": ``eval run ·
compare · report``). See `harness.py`'s module docstring for what a run
actually scores and why there's no live-model path here.
"""

from __future__ import annotations

import sys
from typing import Any, Optional


def register(app: Any) -> None:
    # pyrefly: ignore[missing-import]  — belongs to cli/, the only member
    # that actually depends on it; see odyssey_dataprep.cli's own comment.
    import typer  # noqa: PLC0415 - opt-in only when register() is called

    from odyssey_eval.eval_datasets import (
        build_manifest,
        next_version,
        update_registry,
        write_card,
        write_manifest,
    )
    from odyssey_eval.harness import run_and_report, write_compare_report
    from odyssey_eval.overlap import check_no_overlap

    def run(
        benchmark: str = typer.Option(
            ..., "--benchmark", help="a evaluation/benchmarks/*.yaml suite"
        ),
        completions: str = typer.Option(
            ..., "--completions", help="a {'id','response'} JSONL"
        ),
        metrics: str = typer.Option(
            "evaluation/metrics", "--metrics", help="metrics/ root"
        ),
        reports: str = typer.Option(
            "evaluation/reports", "--reports", help="reports/ output dir"
        ),
    ) -> None:
        """Score a completions file against a benchmark, write a report
        (item 7.1)."""
        result = run_and_report(benchmark, completions, metrics, reports)
        run_result = result["run"]
        print(f"{run_result.benchmark_name}: mean_score={run_result.mean_score:.4f}")
        for kind, path in result["paths"].items():
            print(f"wrote {kind}: {path}")

    def compare(
        a: str = typer.Option(..., "--a", help="a previously written report *.json"),
        b: str = typer.Option(..., "--b", help="a previously written report *.json"),
        reports: str = typer.Option(
            "evaluation/reports", "--reports", help="reports/ output dir"
        ),
    ) -> None:
        """Diff two prior `eval run` reports (item 7.1)."""
        path = write_compare_report(a, b, reports)
        print(f"wrote {path}")

    def build_set(
        name: str = typer.Option(..., "--name", help="eval set name"),
        shard: list[str] = typer.Option(
            ...,
            "--shard",
            help="an eval-set file (journeys dir contents, prompts jsonl, ...); repeatable",
        ),
        manifests: str = typer.Option(
            "evaluation/datasets/manifests", "--manifests", help="manifests root"
        ),
        registry: str = typer.Option(
            "evaluation/datasets/registry.yaml", "--registry", help="registry.yaml path"
        ),
    ) -> None:
        """Build and register one frozen eval set version (item 7.2)."""
        manifest = build_manifest(name, manifests, shard_paths=shard)
        manifest_path = write_manifest(manifest, manifests)
        update_registry(registry, name, manifest_path)
        print(f"wrote {manifest_path}")

    def card(
        name: str = typer.Option(..., "--name", help="eval set name"),
        license: str = typer.Option(..., "--license"),
        intended_use: str = typer.Option(..., "--intended-use"),
        provenance: str = typer.Option(..., "--provenance"),
        version: Optional[int] = typer.Option(
            None, "--version", help="manifest version; default: latest"
        ),
        manifests: str = typer.Option(
            "evaluation/datasets/manifests", "--manifests", help="manifests root"
        ),
        cards: str = typer.Option(
            "evaluation/datasets/cards", "--cards", help="cards/ output dir"
        ),
    ) -> None:
        """Write an eval set's card (item 7.2)."""
        import json
        from pathlib import Path

        v = version or (next_version(name, manifests) - 1)
        if v < 1:
            print(f"no manifest found for {name!r}", file=sys.stderr)
            raise typer.Exit(code=1)
        manifest = json.loads(
            (Path(manifests) / name / f"v{v}.json").read_text(encoding="utf-8")
        )
        path = write_card(
            manifest,
            cards,
            license=license,
            intended_use=intended_use,
            provenance=provenance,
        )
        print(f"wrote {path}")

    def check_overlap(
        eval_journeys: str = typer.Option(
            ..., "--eval-journeys", help="a frozen eval set's journeys dir"
        ),
        train_journeys: str = typer.Option(
            ..., "--train-journeys", help="a training split's journeys dir"
        ),
    ) -> None:
        """No-overlap gate (item 7.4). Exits 3 on breach — the
        lineage-violation code CI greps for (ADR 0003)."""
        errors = check_no_overlap(eval_journeys, train_journeys)
        for err in errors:
            print(err, file=sys.stderr)
        print(f"{'ok' if not errors else 'FAILED'}: {len(errors)} overlap(s)")
        raise typer.Exit(code=0 if not errors else 3)

    @app.callback()
    def _group() -> None:
        """evaluation harness: run · compare · frozen eval sets."""

    app.command()(run)
    app.command()(compare)
    app.command("build-set")(build_set)
    app.command("card")(card)
    app.command("check-overlap")(check_overlap)
