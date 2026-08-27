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


def register(app: Any) -> None:
    # pyrefly: ignore[missing-import]  — belongs to cli/, the only member
    # that actually depends on it; see the module docstring.
    import typer  # noqa: PLC0415 - opt-in only when register() is called

    from odyssey_dataprep.normalization import (
        NormalizeResult,
        normalize_byod_dir,
        normalize_odyssey_dir,
        normalize_odyssey_spool,
    )

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

    # A no-op callback keeps `data` a named command group even with a single
    # subcommand today — without it typer collapses a one-command app so
    # `odyssey data --out ...` would work instead of `odyssey data normalize
    # --out ...`, which would silently reshape once collect/clean/etc. land.
    @app.callback()
    def _group() -> None:
        """data_preparation stages."""

    app.command()(normalize)
