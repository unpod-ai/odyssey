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
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from odyssey.jsonl import write_events
from odyssey.primitives import JourneyEvent
from odyssey.spool import Spool, SpoolConfig, drain


class FileSink:
    """Writes drained events to ``<out>/<journey_id>.jsonl``.

    Append-mode on purpose: a resumed drain sends only the tail, so appending is
    what keeps the output complete across multiple drains.
    """

    def __init__(self, out_dir: Path) -> None:
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def send(self, journey_id: str, events: List[JourneyEvent]) -> None:
        write_events(self.out_dir / f"{journey_id}.jsonl", events, append=True)


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

    status = sub.add_parser("status", help="show per-journey spool state")
    status.set_defaults(func=_cmd_status)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
