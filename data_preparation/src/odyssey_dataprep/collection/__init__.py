"""Collection — item 3.1: pull raw traces from wherever they land into one
flat raw layer, the shape `normalization` (3.3) already expects — one
`<journey_id>.jsonl` per journey, full event stream, no folding.

Three sources:

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
- **object store** (item 1.10) — an S3-compatible bucket holding the same
  shape of `*.jsonl` keys a collector would have written, listed and
  fetched via `boto3` (an optional extra: `odyssey-dataprep[s3]`, lazily
  imported inside `collect_from_object_store` so a light install never
  pulls it in).

All three converge on the same merge step (`_write_merged`): group by each
event's own `journey_id`, not by filename or object key — a storage-safe
stem is not guaranteed reversible for a `journey_id` that needed
sanitizing.
"""

from __future__ import annotations

import dataclasses
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from odyssey.export import _filename
from odyssey.jsonl import read_events, write_events
from odyssey.primitives import JourneyEvent, JourneyHeader

__all__ = [
    "CollectResult",
    "collect_from_spool",
    "collect_from_collector",
    "collect_from_object_store",
]


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


def _write_merged(
    by_journey: Dict[str, List[JourneyEvent]],
    headers: Dict[str, JourneyHeader],
    raw_dir: Path,
) -> List[Path]:
    """The convergence point for every source: one flat `*.jsonl` per
    `journey_id`, regardless of how many files or object-store keys its
    events were scattered across."""
    written: List[Path] = []
    for jid, events in sorted(by_journey.items()):
        header = headers[jid]
        if not header.journey_id:
            header = dataclasses.replace(header, journey_id=jid)
        dest = raw_dir / _shard_filename(jid)
        write_events(dest, events, header=header)
        written.append(dest)
    return written


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

    written = _write_merged(by_journey, headers, out)
    return CollectResult(written=written, errors=errors)


def collect_from_object_store(
    bucket: str,
    prefix: str,
    raw_dir: Path | str,
    *,
    endpoint_url: Optional[str] = None,
    client: Optional[Any] = None,
) -> CollectResult:
    """Reassemble every journey from an S3-compatible bucket's `*.jsonl` keys
    into one flat `*.jsonl` each (item 1.10).

    `client` is a `boto3` S3 client (or a test double exposing the same two
    methods this needs — `list_objects_v2`/`get_object`), the same
    dependency-injection seam this project already uses to keep an optional
    provider's real SDK out of the unit-test path. Omit it for a real
    ``boto3.client("s3", endpoint_url=endpoint_url)``, imported here, lazily
    — never at module scope — so `boto3` stays an optional extra
    (`odyssey-dataprep[s3]`), not a dependency of this member's light
    install (nor, obviously, of `odyssey-core`).
    """
    if client is None:
        # pyrefly: ignore[missing-import]  — optional extra, odyssey-dataprep[s3].
        import boto3  # noqa: PLC0415 - opt-in only when no client is injected

        client = boto3.client("s3", endpoint_url=endpoint_url)

    out = Path(raw_dir)
    out.mkdir(parents=True, exist_ok=True)

    keys: List[str] = []
    continuation: Optional[str] = None
    while True:
        kwargs: Dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if continuation:
            kwargs["ContinuationToken"] = continuation
        page = client.list_objects_v2(**kwargs)
        keys.extend(
            obj["Key"]
            for obj in page.get("Contents", []) or []
            if obj["Key"].endswith(".jsonl")
        )
        if not page.get("IsTruncated"):
            break
        continuation = page.get("NextContinuationToken")

    by_journey: Dict[str, List[JourneyEvent]] = {}
    headers: Dict[str, JourneyHeader] = {}
    errors: List[str] = []
    for key in sorted(keys):
        try:
            body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp) / "received.jsonl"
                tmp_path.write_bytes(body)
                result = read_events(tmp_path)
        except Exception as exc:  # noqa: BLE001 - one bad key must not abort the sweep
            errors.append(f"{key}: {type(exc).__name__}: {exc}")
            continue
        for e in result.events:
            by_journey.setdefault(e.journey_id, []).append(e)
            headers.setdefault(e.journey_id, result.header)

    written = _write_merged(by_journey, headers, out)
    return CollectResult(written=written, errors=errors)
