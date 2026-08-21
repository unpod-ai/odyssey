"""Drain destinations. A sink is one method: ``send(journey_id, events)``.

Sinks live here rather than in ``cli.py`` so the library never imports the
command line. ``odyssey.cli.FileSink`` stays importable — it re-exports this one.

Raise to signal failure; never return false. ``drain()`` treats an exception as
retryable and leaves both the shard and the watermark untouched, so the next
drain re-sends the same events. A returned boolean would be ignored.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from odyssey.jsonl import write_events
from odyssey.primitives import JourneyEvent


class FileSink:
    """Writes drained events to ``<out>/<journey_id>.jsonl``.

    Append-mode on purpose: a resumed drain sends only the tail, so appending is
    what keeps the output complete across multiple drains.

    This is the real, usable destination until the network sink ships with the
    backend — a directory of per-journey JSONL is exactly the interchange format
    a trainer consumes.
    """

    def __init__(self, out_dir: Path | str) -> None:
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def send(self, journey_id: str, events: List[JourneyEvent]) -> None:
        write_events(self.out_dir / f"{journey_id}.jsonl", events, append=True)
