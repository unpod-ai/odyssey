"""odyssey-dataprep CLI plugin — mounts `data normalize` onto the odyssey CLI.

The ADR 0003 plugin contract: ``cli/`` discovers ``data = "odyssey_dataprep.
cli:register"`` via entry-point metadata and calls this with a fresh typer
sub-app, importing this module (and, transitively, typer) only when the
``data`` command group is actually invoked.

Unlike ``odyssey.cli.register`` this member has no pre-existing argparse
entrypoint to delegate to — ``normalization`` is a plain importable module,
so this command calls its functions directly and does its own thin
print/exit-code handling. Still "zero logic in the CLI": the actual work
(parsing, folding, writing) is entirely in ``odyssey_dataprep.normalization``.
"""

from __future__ import annotations

import sys
from typing import Any, Optional


def _parse_pii_rules(raw: Optional[str]) -> Optional[list]:
    """``--pii-rules email,phone`` -> ``["EMAIL", "PHONE"]``, or ``None`` when
    the flag was omitted -- the opt-in default for both `clean` and
    `validate` (item 2.15)."""
    if not raw:
        return None
    return [r.strip().upper() for r in raw.split(",") if r.strip()]


def register(app: Any) -> None:
    # pyrefly: ignore[missing-import]  — belongs to cli/, the only member
    # that actually depends on it; see the module docstring.
    import typer  # noqa: PLC0415 - opt-in only when register() is called
    from odyssey.primitives import PiiPolicy

    from odyssey_dataprep.annotation import apply_reviews as _apply_reviews
    from odyssey_dataprep.annotation import build_queue
    from odyssey_dataprep.augmentation import (
        generate_synthetic_negative,
        paraphrase_journey,
        perturb_tool_calls,
    )
    from odyssey_dataprep.cleaning import clean_dir
    from odyssey_dataprep.collection import (
        collect_from_collector,
        collect_from_object_store,
        collect_from_spool,
    )
    from odyssey_dataprep.datasets import (
        build_manifest,
        next_version,
        update_registry,
        write_card,
        write_manifest,
    )
    from odyssey_dataprep.normalization import (
        NormalizeResult,
        normalize_byod_dir,
        normalize_odyssey_dir,
        normalize_odyssey_spool,
    )
    from odyssey_dataprep.recipes import load_recipe
    from odyssey_dataprep.recipes import recipe_hash as _recipe_hash
    from odyssey_dataprep.splitting import split_dir
    from odyssey_dataprep.validation import validate_dir
    from odyssey_dataprep.versioning import compute_curated_watermark, corpus_version

    def _report(result: NormalizeResult) -> None:
        print(f"normalized {result.count}")
        for cid, reason in sorted(result.incomplete.items()):
            print(f"flagged {cid}: {reason}", file=sys.stderr)
        for err in result.errors:
            print(f"error   {err}", file=sys.stderr)
        raise typer.Exit(code=0 if result.ok else 1)

    def normalize(
        out: str = typer.Option(..., help="output directory for canonical *.json"),
        raw: Optional[str] = typer.Option(
            None,
            help=(
                "directory of raw provider-format *.json (BYOD); "
                "omit for odyssey-shaped input"
            ),
        ),
        format: Optional[str] = typer.Option(
            None,
            help=(
                "required with --raw: openai_chat / anthropic_messages / "
                "vercel_ai_sdk"
            ),
        ),
        data_source: Optional[str] = typer.Option(None, help="required with --raw"),
        events: Optional[str] = typer.Option(
            None,
            help=(
                "directory of drained odyssey *.jsonl (odyssey-shaped only); "
                "default: read --spool"
            ),
        ),
        journey: Optional[str] = typer.Option(
            None, help="normalize only this journey_id (odyssey-shaped only)"
        ),
        spool: str = typer.Option(".odyssey", help="spool root (odyssey-shaped only)"),
    ) -> None:
        """Normalize raw traces into canonical Journey artifacts."""
        if raw:
            if not format or not data_source:
                print(
                    "--format and --data-source are required with --raw",
                    file=sys.stderr,
                )
                raise typer.Exit(code=2)
            _report(
                normalize_byod_dir(raw, out, format=format, data_source=data_source)
            )
        elif events:
            _report(normalize_odyssey_dir(events, out, journey_id=journey))
        else:
            _report(normalize_odyssey_spool(spool, out, journey_id=journey))

    def recipe_hash(
        recipe: str = typer.Argument(..., help="path to a recipes/*.yaml file"),
    ) -> None:
        """Print a recipe's hash (item 4.4) — 'processed which way'."""
        print(_recipe_hash(load_recipe(recipe)))

    def corpus_version_cmd(
        recipe: str = typer.Option(
            ..., "--recipe", help="path to a recipes/*.yaml file"
        ),
        curated: str = typer.Option(
            ..., "--curated", help="directory of normalized *.json journeys"
        ),
        seq: int = typer.Option(
            ..., "--seq", help="this curation run's sequence number"
        ),
    ) -> None:
        """Print the corpus version (item 4.5): sha(recipe_hash + curated_watermark)."""
        watermark = compute_curated_watermark(curated, seq=seq)
        print(corpus_version(_recipe_hash(load_recipe(recipe)), watermark))

    def build_corpus(
        name: str = typer.Option(..., "--name", help="corpus name"),
        recipe: str = typer.Option(
            ..., "--recipe", help="path to a recipes/*.yaml file"
        ),
        curated: str = typer.Option(
            ..., "--curated", help="directory of normalized *.json journeys"
        ),
        shard: list[str] = typer.Option(
            ...,
            "--shard",
            help="a corpus shard file (e.g. odyssey sft/dpo output); repeatable",
        ),
        manifests: str = typer.Option(
            "datasets/manifests", "--manifests", help="manifests root"
        ),
        registry: str = typer.Option(
            "datasets/registry.yaml", "--registry", help="registry.yaml path"
        ),
    ) -> None:
        """Build and register one corpus version (items 4.6/4.7): a manifest
        (shards + sha256 + row counts + recipe_hash) plus a registry.yaml entry."""
        recipe_h = _recipe_hash(load_recipe(recipe))
        seq = next_version(name, manifests)
        watermark = compute_curated_watermark(curated, seq=seq)
        manifest = build_manifest(
            name,
            manifests,
            corpus_version=corpus_version(recipe_h, watermark),
            recipe_hash=recipe_h,
            curated_watermark=watermark,
            shard_paths=shard,
        )
        manifest_path = write_manifest(manifest, manifests)
        update_registry(registry, name, manifest_path)
        print(f"wrote {manifest_path}")
        print(manifest["corpus_version"])

    def card(
        name: str = typer.Option(..., "--name", help="corpus name"),
        license: str = typer.Option(..., "--license"),
        pii_posture: str = typer.Option(..., "--pii-posture"),
        intended_use: str = typer.Option(..., "--intended-use"),
        version: Optional[int] = typer.Option(
            None, "--version", help="manifest version; default: latest"
        ),
        splits: Optional[str] = typer.Option(None, "--splits"),
        manifests: str = typer.Option(
            "datasets/manifests", "--manifests", help="manifests root"
        ),
        cards: str = typer.Option("datasets/cards", "--cards", help="cards root"),
    ) -> None:
        """Write a corpus card (item 4.8): provenance, license, PII posture,
        splits, intended use."""
        import json
        from pathlib import Path

        v = version if version is not None else next_version(name, manifests) - 1
        if v < 1:
            print(f"no manifest found for {name!r} under {manifests}", file=sys.stderr)
            raise typer.Exit(code=1)
        manifest_path = Path(manifests) / name / f"v{v}.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        path = write_card(
            manifest,
            cards,
            license=license,
            pii_posture=pii_posture,
            intended_use=intended_use,
            splits=splits,
        )
        print(f"wrote {path}")

    def collect(
        out: str = typer.Option(..., "--out", help="flat raw-layer output directory"),
        spool: Optional[str] = typer.Option(None, "--spool", help="a spool root"),
        collector: Optional[str] = typer.Option(
            None, "--collector", help="a services/collector data_dir"
        ),
        bucket: Optional[str] = typer.Option(
            None, "--bucket", help="an S3-compatible bucket (item 1.10)"
        ),
        prefix: str = typer.Option(
            "", "--prefix", help="key prefix to list within --bucket"
        ),
        endpoint_url: Optional[str] = typer.Option(
            None,
            "--endpoint-url",
            help="S3-compatible endpoint URL (e.g. for MinIO); omit for AWS S3",
        ),
    ) -> None:
        """Pull raw traces into a flat raw layer (item 3.1): one *.jsonl per
        journey, reassembled from wherever it's rotated/date-partitioned."""
        sources = [spool, collector, bucket]
        if sum(bool(s) for s in sources) != 1:
            print(
                "exactly one of --spool, --collector, or --bucket is required",
                file=sys.stderr,
            )
            raise typer.Exit(code=2)
        if spool:
            result = collect_from_spool(spool, out)
        elif collector:
            result = collect_from_collector(collector, out)
        else:
            result = collect_from_object_store(
                bucket, prefix, out, endpoint_url=endpoint_url
            )
        print(f"collected {result.count}")
        for err in result.errors:
            print(f"error   {err}", file=sys.stderr)
        raise typer.Exit(code=0 if result.ok else 1)

    def clean(
        journeys: str = typer.Option(..., "--journeys", help="normalized *.json dir"),
        out: str = typer.Option(..., "--out", help="output directory"),
        pii_rules: Optional[str] = typer.Option(
            None,
            "--pii-rules",
            help="comma-separated content-level PII rules to scrub, e.g. "
            "email,phone,credit_card,ssn (item 2.15) -- opt-in, off by default",
        ),
    ) -> None:
        """Dedupe, drop dead turns, repair encoding (item 3.2)."""
        rules = _parse_pii_rules(pii_rules)
        policy = PiiPolicy(name="cli", rules=rules) if rules else None
        result = clean_dir(journeys, out, pii_policy=policy)
        print(
            f"cleaned {result.count} "
            f"(dropped {result.duplicates_dropped} duplicate(s), "
            f"{result.dead_turns_dropped} dead turn(s), "
            f"repaired {result.encoding_repairs} string(s), "
            f"scrubbed {result.pii_scrubs} PII match(es))"
        )

    def queue(
        journeys: str = typer.Option(..., "--journeys", help="normalized *.json dir"),
        out: str = typer.Option(..., "--out", help="queue *.jsonl path"),
    ) -> None:
        """Write a human-review queue (item 3.4)."""
        n = build_queue(journeys, out)
        print(f"queued {n}")

    def apply_reviews(
        journeys: str = typer.Option(..., "--journeys", help="normalized *.json dir"),
        decisions: str = typer.Option(
            ..., "--decisions", help="reviewer decisions *.jsonl"
        ),
        out: str = typer.Option(..., "--out", help="output directory"),
    ) -> None:
        """Apply reviewer decisions -- reward + approval -- onto journeys
        (item 3.4)."""
        result = _apply_reviews(journeys, decisions, out)
        print(f"applied {result.count}")
        for jid in result.skipped:
            print(f"no journey found for decision: {jid}", file=sys.stderr)

    def augment(
        journeys: str = typer.Option(..., "--journeys", help="normalized *.json dir"),
        out: str = typer.Option(
            ..., "--out", help="output directory for synthetic journeys"
        ),
        paraphrase: int = typer.Option(
            0,
            "--paraphrase",
            help="LLM-generated paraphrases per journey (item 3.5) -- opt-in, "
            "0 by default; requires an OpenAI-compatible client (OPENAI_API_KEY "
            "env, or odyssey-dataprep[llm] installed)",
        ),
        synthetic_negatives: bool = typer.Option(
            False,
            "--synthetic-negatives",
            help="one LLM-generated wrong-answer DPO pair per journey (item "
            "3.5) -- opt-in, off by default; same client requirement as "
            "--paraphrase",
        ),
        llm_model: str = typer.Option(
            "gpt-4.1-mini",
            "--llm-model",
            help="model for --paraphrase/--synthetic-negatives",
        ),
    ) -> None:
        """Synthetic negatives via tool-call perturbation (item 3.5, always
        on) plus optional LLM-backed paraphrase / synthetic-negative
        generation (opt-in, both off by default -- an LLM call per journey
        is a real cost this stage does not spend unless asked to)."""
        import json
        from pathlib import Path

        client = None
        if paraphrase or synthetic_negatives:
            # pyrefly: ignore[missing-import]  — optional extra, odyssey-dataprep[llm].
            import openai  # noqa: PLC0415 - opt-in only when actually requested

            client = openai.OpenAI()

        src = Path(journeys)
        dest_dir = Path(out)
        dest_dir.mkdir(parents=True, exist_ok=True)
        n = 0
        for path in sorted(src.glob("*.json")):
            journey = json.loads(path.read_text(encoding="utf-8"))
            synthetic_journeys = list(perturb_tool_calls(journey))
            if paraphrase:
                synthetic_journeys += paraphrase_journey(
                    journey, client=client, model=llm_model, n=paraphrase
                )
            if synthetic_negatives:
                negative = generate_synthetic_negative(
                    journey, client=client, model=llm_model
                )
                if negative is not None:
                    synthetic_journeys.append(negative)
            for synthetic in synthetic_journeys:
                cid = synthetic["task"]["conversation_id"]
                dest = dest_dir / f"{cid}.json"
                dest.write_text(json.dumps(synthetic, indent=2, sort_keys=True) + "\n")
                n += 1
        print(f"generated {n}")

    def validate(
        journeys: str = typer.Option(..., "--journeys", help="normalized *.json dir"),
        pii_rules: Optional[str] = typer.Option(
            None,
            "--pii-rules",
            help="comma-separated content-level PII rules to additionally scan "
            "for, e.g. email,phone,credit_card,ssn (item 2.15) -- opt-in, off "
            "by default; the key-based check always runs",
        ),
    ) -> None:
        """Schema + PII-redaction checks (item 3.6). Exits 3 on breach — the
        lineage-violation code CI greps for (ADR 0003)."""
        result = validate_dir(journeys, content_pii_rules=_parse_pii_rules(pii_rules))
        for err in result.errors:
            print(err, file=sys.stderr)
        print(f"{'ok' if result.ok else 'FAILED'}: {len(result.errors)} error(s)")
        raise typer.Exit(code=0 if result.ok else 3)

    def split(
        journeys: str = typer.Option(..., "--journeys", help="cleaned *.json dir"),
        out: str = typer.Option(..., "--out", help="output root (train/val/test)"),
    ) -> None:
        """Split by group key, never by row (item 3.7)."""
        written = split_dir(journeys, out)
        for name, paths in sorted(written.items()):
            print(f"{name}: {len(paths)}")

    # A no-op callback keeps `data` a named command group even with a single
    # subcommand today — without it typer collapses a one-command app so
    # `odyssey data --out ...` would work instead of `odyssey data normalize
    # --out ...`, which would silently reshape once collect/clean/etc. land.
    @app.callback()
    def _group() -> None:
        """data_preparation stages."""

    app.command()(normalize)
    app.command("recipe-hash")(recipe_hash)
    app.command("corpus-version")(corpus_version_cmd)
    app.command("build-corpus")(build_corpus)
    app.command("card")(card)
    app.command("collect")(collect)
    app.command("clean")(clean)
    app.command("queue")(queue)
    app.command("apply-reviews")(apply_reviews)
    app.command("augment")(augment)
    app.command("validate")(validate)
    app.command("split")(split)
