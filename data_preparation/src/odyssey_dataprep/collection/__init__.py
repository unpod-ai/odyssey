"""Collection — item 3.1: pull raw traces from wherever they land into one
flat raw layer, the shape `normalization` (3.3) already expects — one
`<journey_id>.jsonl` per journey, full event stream, no folding.

Two sources exist today; a third (object store, item 1.10) does not, so it
is not wired here:

- **spool** — `Spool` nests one directory per journey and rotates shards
  inside it (`journeys/<journey_id>/<date>.<seq>.jsonl`); `Spool.read()` is
  the only thing that knows how to reassemble one, same reasoning
  `normalization.normalize_odyssey_spool` already gives for going straight
  to the spool instead of draining first.
- **collector** — `services/collector` nests by date
  (`<date>/<safe_stem(journey_id)>.jsonl`), one growing file per journey
  per day. A journey spanning more than one day is split across files that
  `normalization`'s flat-directory reader cannot reassemble on its own —
  this is the gap this stage closes for that source.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

from odyssey.export import _filename
from odyssey.jsonl import read_events, write_events
from odyssey.primitives import JourneyEvent, JourneyHeader

__all__ = ["CollectResult", "collect_from_spool", "collect_from_collector"]


def _shard_filename(journey_id: str) -> str:
    """journey_id as a flat *.jsonl filename — same traversal-safety as
    odyssey.export._filename, reused directly rather than re-derived."""
    return _filename(journey_id)[: -len(".json")] + ".jsonl"


@dataclass(frozen=True)
class CollectResult:
    written: List[Path] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def count(self) -> int:
        return len(self.written)


def collect_from_spool(spool_root: Path | str, raw_dir: Path | str) -> CollectResult:
    """Reassemble every journey in a spool into one flat `*.jsonl` each."""
    from odyssey.spool import Spool, SpoolConfig

    spool = Spool(SpoolConfig(root=spool_root))
    out = Path(raw_dir)
    out.mkdir(parents=True, exist_ok=True)

    written: List[Path] = []
    errors: List[str] = []
    for jid in spool.journey_ids():
        try:
            events = spool.read(jid)
            if not events:
                raise ValueError("no events")
            header = spool.header(jid) or JourneyHeader(journey_id=jid)
            if not header.journey_id:
                header = dataclasses.replace(header, journey_id=jid)
            path = out / _shard_filename(jid)
            write_events(path, events, header=header)
            written.append(path)
        except (OSError, ValueError) as exc:
            errors.append(f"{jid}: {type(exc).__name__}: {exc}")
    return CollectResult(written=written, errors=errors)


def collect_from_collector(
    collector_root: Path | str, raw_dir: Path | str
) -> CollectResult:
    """Reassemble every journey scattered across a collector's date-partitioned
    store into one flat `*.jsonl` each.

    Grouped by each event's own `journey_id`, not by filename — the
    filename is a storage-safe stem (`_safe_stem` in `services/collector`),
    which is not guaranteed reversible for a `journey_id` that needed
    sanitizing. Event order within the merged file does not need to be
    globally sorted here: `fold()` sorts by `seq` and dedupes by
    `event_id` at read time regardless.
    """
    out = Path(raw_dir)
    out.mkdir(parents=True, exist_ok=True)

    by_journey: Dict[str, List[JourneyEvent]] = {}
    headers: Dict[str, JourneyHeader] = {}
    errors: List[str] = []
    for path in sorted(Path(collector_root).rglob("*.jsonl")):
        try:
            result = read_events(path)
        except (OSError, ValueError) as exc:
            errors.append(f"{path}: {type(exc).__name__}: {exc}")
            continue
        for e in result.events:
            by_journey.setdefault(e.journey_id, []).append(e)
            headers.setdefault(e.journey_id, result.header)

    written: List[Path] = []
    for jid, events in sorted(by_journey.items()):
        header = headers[jid]
        if not header.journey_id:
            header = dataclasses.replace(header, journey_id=jid)
        dest = out / _shard_filename(jid)
        write_events(dest, events, header=header)
        written.append(dest)
    return CollectResult(written=written, errors=errors)
