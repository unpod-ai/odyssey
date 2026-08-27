"""odyssey CLI — the command-line drain trigger, plus spool inspection.

Three triggers share one ``drain()``: ``Spool.push()`` (SDK), ``IntervalDrainer``
(time), and ``odyssey push`` (here). This module adds no drain logic of its own.

The network sink ships with the backend, which is out of scope for this change.
Until then ``FileSink`` is the real, usable destination: it drains the spool into
a directory of per-journey JSONL, which is exactly the interchange format a
trainer consumes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from odyssey.sinks import FileSink
from odyssey.spool import Spool, SpoolConfig, drain

__all__ = ["FileSink", "build_parser", "main", "register"]


def _cmd_push(args: argparse.Namespace) -> int:
    spool = Spool(SpoolConfig(root=Path(args.spool)))
    result = drain(spool, FileSink(Path(args.out)), journey_id=args.journey)

    print(f"pushed  {result.pushed}")
    print(f"skipped {result.skipped}")
    print(f"failed  {result.failed}")
    for jid, missing in sorted(result.gaps.items()):
        print(f"gap     {jid}: missing seq {missing}", file=sys.stderr)
    for err in result.errors:
        print(f"error   {err}", file=sys.stderr)
    # Non-zero on failure so a cron-driven drain is visible to its supervisor.
    return 0 if result.ok else 1


def _cmd_export(args: argparse.Namespace) -> int:
    """Turn drained events into the artifact a trainer or the platform consumes.

    `push` produces the wire format — append-only events, no cumulative state,
    because shipping N steps costs O(N**2) where shipping N events costs O(N).
    That is transport, not a deliverable. This folds those events back into one
    `{conversation_id}.json` per conversation: task, steps, reward, metrics.

    Incomplete journeys are still written, flagged under `_odyssey`, and listed
    on stderr — the caller decides, but never by accident.

    `--last-step` writes the final step alone. Each step carries the whole
    conversation up to its turn, so the last one already holds every message and
    every tool call; the rest are prefixes of it and cost O(N**2) bytes.
    """
    from odyssey.export import export_dir, export_spool

    # Straight from the spool by default. That is where a developer's events
    # already are, and requiring a `push` first to see the artifact for a call
    # just recorded is a step with no purpose — reading the spool moves no
    # watermark, so a later drain still ships everything.
    if args.events:
        result = export_dir(
            Path(args.events),
            Path(args.out),
            journey_id=args.journey,
            last_step_only=args.last_step,
        )
    else:
        result = export_spool(
            Path(args.spool),
            Path(args.out),
            journey_id=args.journey,
            last_step_only=args.last_step,
        )
    print(f"exported {result.count}")
    for cid, reason in sorted(result.incomplete.items()):
        print(f"flagged  {cid}: {reason}", file=sys.stderr)
    for err in result.errors:
        print(f"error    {err}", file=sys.stderr)
    return 0 if result.ok else 1


def _cmd_sft(args: argparse.Namespace) -> int:
    """Write the SFT training file: one JSON line per trainable turn.

    Same read side as `export` — spool by default, `--events` for an
    already-drained directory — but the output is a single `.jsonl` file, not
    a directory of Trajectory JSON: this is a training shard, and every SFT
    trainer that reads JSONL wants one file to point at.
    """
    from odyssey.sft import export_sft_dir, export_sft_spool

    if args.events:
        result = export_sft_dir(
            Path(args.events), Path(args.out), journey_id=args.journey
        )
    else:
        result = export_sft_spool(
            Path(args.spool), Path(args.out), journey_id=args.journey
        )
    print(f"wrote {result.written} example(s)")
    for jid, reason in sorted(result.skipped_incomplete.items()):
        print(f"skipped {jid}: {reason}", file=sys.stderr)
    for err in result.errors:
        print(f"error   {err}", file=sys.stderr)
    return 0 if result.ok else 1


def _cmd_dpo(args: argparse.Namespace) -> int:
    """Write DPO preference pairs: one `(prompt, chosen, rejected)` line per pair.

    A pair exists wherever a regenerated/edited answer was later accepted —
    see `odyssey.dpo` for exactly how a pair is found. A journey with no such
    decision point contributes nothing, which is not an error.
    """
    from odyssey.dpo import export_dpo_dir, export_dpo_spool

    if args.events:
        result = export_dpo_dir(
            Path(args.events), Path(args.out), journey_id=args.journey
        )
    else:
        result = export_dpo_spool(
            Path(args.spool), Path(args.out), journey_id=args.journey
        )
    print(f"wrote {result.written} pair(s)")
    for jid, reason in sorted(result.skipped_incomplete.items()):
        print(f"skipped {jid}: {reason}", file=sys.stderr)
    for err in result.errors:
        print(f"error   {err}", file=sys.stderr)
    return 0 if result.ok else 1


def _cmd_status(args: argparse.Namespace) -> int:
    spool = Spool(SpoolConfig(root=Path(args.spool)))
    ids = spool.journey_ids()
    if not ids:
        print("spool is empty")
        return 0
    print(f"{'journey':<32} {'total':>7} {'undrained':>10} {'watermark':>10}")
    for jid in ids:
        mark = spool.watermark(jid)
        print(
            f"{jid:<32} {len(spool.read(jid)):>7} "
            f"{len(spool.undrained(jid)):>10} "
            f"{'-' if mark is None else mark:>10}"
        )
    return 0


def _cmd_health(args: argparse.Namespace) -> int:
    """Answer "is it actually recording?" — for a spool, and for this process.

    Read-only: nothing drains, no watermark moves, so it is safe against a spool
    a live process is writing to. Exits 3 on a writer conflict, which is the
    lineage-violation code CI greps for (ADR 0003).
    """
    from odyssey.diagnostics import format_report, report, scan

    live = report()
    journeys = scan(Path(args.spool), journey_id=args.journey)
    if args.json:
        print(
            json.dumps(
                {"process": live, "journeys": [j.as_dict() for j in journeys]},
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(format_report(live, journeys))
    return 3 if any(len(j.writers) > 1 for j in journeys) else 0


def _cmd_show(args: argparse.Namespace) -> int:
    """Print a collected journey as a conversation, plus its training view.

    The question `health` cannot answer: not "is it recording?" but "show me what
    you recorded, and which of it a model would learn from."
    """
    from odyssey.diagnostics import render_journey

    spool = Spool(SpoolConfig(root=Path(args.spool)))
    targets = [args.journey] if args.journey else spool.journey_ids()
    if not targets:
        print("spool is empty")
        return 0
    for i, jid in enumerate(targets):
        if i:
            print()
        print(render_journey(Path(args.spool), jid))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="odyssey", description=__doc__.splitlines()[0])
    p.add_argument(
        "--spool",
        default=".odyssey",
        help="spool root (default: .odyssey). Not required to be under the cwd.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    push = sub.add_parser("push", help="drain the spool now")
    push.add_argument("--out", required=True, help="output directory for JSONL")
    push.add_argument("--journey", default=None, help="drain only this journey_id")
    push.set_defaults(func=_cmd_push)

    export = sub.add_parser(
        "export", help="fold drained events into Trajectory JSON artifacts"
    )
    export.add_argument(
        "--events",
        default=None,
        help="directory of drained *.jsonl (push --out); default: read --spool",
    )
    export.add_argument("--out", required=True, help="output directory for *.json")
    export.add_argument("--journey", default=None, help="export only this journey_id")
    export.add_argument(
        "--last-step",
        action="store_true",
        help="write only the final step (it already holds the whole conversation)",
    )
    export.set_defaults(func=_cmd_export)

    sft = sub.add_parser(
        "sft", help="write an SFT training file (one line per trainable turn)"
    )
    sft.add_argument(
        "--events",
        default=None,
        help="directory of drained *.jsonl (push --out); default: read --spool",
    )
    sft.add_argument(
        "--out", required=True, help="output .jsonl file (not a directory)"
    )
    sft.add_argument("--journey", default=None, help="export only this journey_id")
    sft.set_defaults(func=_cmd_sft)

    dpo = sub.add_parser(
        "dpo", help="write DPO preference pairs (prompt/chosen/rejected)"
    )
    dpo.add_argument(
        "--events",
        default=None,
        help="directory of drained *.jsonl (push --out); default: read --spool",
    )
    dpo.add_argument(
        "--out", required=True, help="output .jsonl file (not a directory)"
    )
    dpo.add_argument("--journey", default=None, help="export only this journey_id")
    dpo.set_defaults(func=_cmd_dpo)

    status = sub.add_parser("status", help="show per-journey spool state")
    status.set_defaults(func=_cmd_status)

    show = sub.add_parser(
        "show", help="print a collected journey and what is trainable in it"
    )
    show.add_argument(
        "journey", nargs="?", default=None, help="journey_id (default: all)"
    )
    show.set_defaults(func=_cmd_show)

    health = sub.add_parser(
        "health", help="is it recording? per-journey foldability and failures"
    )
    health.add_argument("--journey", default=None, help="inspect only this journey_id")
    health.add_argument("--json", action="store_true", help="machine-readable output")
    health.set_defaults(func=_cmd_health)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


def register(app: Any) -> None:
    """Mount this module's commands as ``odyssey.commands`` plugin entries.

    The ADR 0003 plugin contract: ``cli/`` discovers ``spool = "odyssey.cli:
    register"`` via entry-point metadata and calls this with a fresh typer
    sub-app, importing this module (and, transitively, typer) only when the
    ``spool`` command group is actually invoked — never for a cold
    ``odyssey --help``.

    Every function below is pure plumbing: it builds the argv :func:`main`
    (this module's own argparse entrypoint, already tested) already
    understands and delegates to it. That keeps exactly one implementation
    of what each command does — this is a friendlier front end, not a
    second backend — and it is why ``typer`` is imported here, inside the
    function, rather than at module scope: importing it would break
    ``odyssey-core``'s ``dependencies = []`` for every caller, not just
    ``cli/``, which is the only one that actually has it installed.
    """
    # pyrefly: ignore[missing-import]  — belongs to cli/, the only member
    # that actually depends on it; see the docstring for why it's local.
    import typer  # noqa: PLC0415 - opt-in only when register() is called

    def _run(argv: list) -> None:
        raise typer.Exit(code=main(argv))

    def push(
        out: str = typer.Option(..., help="output directory for JSONL"),
        journey: Optional[str] = typer.Option(None, help="drain only this journey_id"),
        spool: str = typer.Option(".odyssey", help="spool root"),
    ) -> None:
        """Drain the spool now."""
        argv = ["--spool", spool, "push", "--out", out]
        if journey:
            argv += ["--journey", journey]
        _run(argv)

    def export(
        out: str = typer.Option(..., help="output directory for *.json"),
        events: Optional[str] = typer.Option(
            None, help="directory of drained *.jsonl; default: read --spool"
        ),
        journey: Optional[str] = typer.Option(None, help="export only this journey_id"),
        last_step: bool = typer.Option(
            False, "--last-step", help="write only the final step"
        ),
        spool: str = typer.Option(".odyssey", help="spool root"),
    ) -> None:
        """Fold drained events into Trajectory JSON artifacts."""
        argv = ["--spool", spool, "export", "--out", out]
        if events:
            argv += ["--events", events]
        if journey:
            argv += ["--journey", journey]
        if last_step:
            argv.append("--last-step")
        _run(argv)

    def sft(
        out: str = typer.Option(..., help="output .jsonl file (not a directory)"),
        events: Optional[str] = typer.Option(
            None, help="directory of drained *.jsonl; default: read --spool"
        ),
        journey: Optional[str] = typer.Option(None, help="export only this journey_id"),
        spool: str = typer.Option(".odyssey", help="spool root"),
    ) -> None:
        """Write an SFT training file (one line per trainable turn)."""
        argv = ["--spool", spool, "sft", "--out", out]
        if events:
            argv += ["--events", events]
        if journey:
            argv += ["--journey", journey]
        _run(argv)

    def dpo(
        out: str = typer.Option(..., help="output .jsonl file (not a directory)"),
        events: Optional[str] = typer.Option(
            None, help="directory of drained *.jsonl; default: read --spool"
        ),
        journey: Optional[str] = typer.Option(None, help="export only this journey_id"),
        spool: str = typer.Option(".odyssey", help="spool root"),
    ) -> None:
        """Write DPO preference pairs (prompt/chosen/rejected)."""
        argv = ["--spool", spool, "dpo", "--out", out]
        if events:
            argv += ["--events", events]
        if journey:
            argv += ["--journey", journey]
        _run(argv)

    def status(spool: str = typer.Option(".odyssey", help="spool root")) -> None:
        """Show per-journey spool state."""
        _run(["--spool", spool, "status"])

    def show(
        journey: Optional[str] = typer.Argument(None, help="journey_id (default: all)"),
        spool: str = typer.Option(".odyssey", help="spool root"),
    ) -> None:
        """Print a collected journey and what is trainable in it."""
        argv = ["--spool", spool, "show"]
        if journey:
            argv.append(journey)
        _run(argv)

    def health(
        journey: Optional[str] = typer.Option(
            None, help="inspect only this journey_id"
        ),
        as_json: bool = typer.Option(False, "--json", help="machine-readable output"),
        spool: str = typer.Option(".odyssey", help="spool root"),
    ) -> None:
        """Is it recording? Per-journey foldability and failures."""
        argv = ["--spool", spool, "health"]
        if journey:
            argv += ["--journey", journey]
        if as_json:
            argv.append("--json")
        _run(argv)

    for fn in (push, export, sft, dpo, status, show, health):
        app.command()(fn)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
