"""Local append-only event capture, drained to a sink out of band.

The shape is deliberate: **recording never touches the network.** A voice agent
appends an event and returns; a separate drain ships batches on an interval, on a
CLI command, or on an explicit ``push()``. That keeps the inference hot path free
of remote latency and makes recording work with no server reachable at all.

**The local shard is the retry queue.** There is no retry framework, no backoff
scheduler, no in-memory buffer to lose: a shard stays on disk until the sink
acknowledges it, then the watermark advances past those events. Two upstream
policies are explicitly rejected here — Soup's ``monitoring/hf_push.py:160-161``
logs a warning and drops the checkpoint, and its ``utils/trackers.py`` does a
synchronous 1-second-timeout POST per event with silent failure and no local
persistence. Both lose data. This does not.

Layout::

    <root>/journeys/<journey_id>/000.jsonl   active shard (rotates at size cap)
    <root>/journeys/<journey_id>/001.jsonl
    <root>/watermarks.json                   {journey_id: last acked seq}
"""

from __future__ import annotations

import dataclasses
import json
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Optional,
    Protocol,
    TextIO,
    runtime_checkable,
)

from odyssey.jsonl import encode_event, header_line, read_events
from odyssey.primitives import JourneyEvent

REDACTED = "[REDACTED]"

# Same starting set superdialog's observer redacts on, so a value masked on the
# inference side is masked on the training side too.
DEFAULT_REDACT_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "authorization",
        "auth",
        "cvv",
        "pin",
        "otp",
        "ssn",
        "card_number",
        "account_number",
        "credit_card",
    }
)

_DEFAULT_SHARD_BYTES = 100 * 1024 * 1024  # 100 MB, matching the donor's cap

# Cap on cached shard handles. One open file descriptor per actively-recording
# journey buys a ~15x faster record() (no per-event mkdir/glob/stat), but an
# unbounded cache would exhaust the process fd limit under many concurrent
# journeys. Least-recently-written handles are closed past this point; the data
# is already on disk, so eviction costs only the next write's reopen.
_DEFAULT_MAX_OPEN_SHARDS = 256

# An interval outside this range is a configuration mistake, not a preference:
# below 1s the drain thrashes, above an hour it is not a drain, it is a cron job.
MIN_DRAIN_INTERVAL = 1.0
MAX_DRAIN_INTERVAL = 3600.0


class SpoolPathError(ValueError):
    """A path escaped the configured spool root."""


@runtime_checkable
class Sink(Protocol):
    """Where a drain sends events. Raise to signal failure — never return false."""

    def send(self, journey_id: str, events: List[JourneyEvent]) -> None: ...


@dataclass(frozen=True)
class SpoolConfig:
    root: Path
    max_shard_bytes: int = _DEFAULT_SHARD_BYTES
    redact_keys: frozenset[str] = DEFAULT_REDACT_KEYS
    # Off by default: fsync per event costs milliseconds on the hot path, and the
    # OS buffer already survives process death. Turn it on when the threat model
    # is machine loss rather than process loss.
    fsync: bool = False
    max_open_shards: int = _DEFAULT_MAX_OPEN_SHARDS

    def __post_init__(self) -> None:
        if self.max_shard_bytes <= 0:
            raise ValueError("max_shard_bytes must be positive")
        if self.max_open_shards <= 0:
            raise ValueError("max_open_shards must be positive")


@dataclass(frozen=True)
class DrainResult:
    pushed: int = 0
    skipped: int = 0
    failed: int = 0
    errors: List[str] = field(default_factory=list)
    gaps: Dict[str, List[int]] = field(default_factory=dict)
    journeys: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failed == 0 and not self.errors


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def _redact_mapping(d: Any, keys: frozenset[str]) -> Any:
    """Mask values whose key is a configured secret. Empty values pass through.

    A redaction marker therefore always means a real value existed — the
    distinction matters when auditing what actually leaked.
    """
    if isinstance(d, dict):
        out: Dict[str, Any] = {}
        for k, v in d.items():
            if _is_secret(str(k), keys) and v not in (None, "", [], {}):
                out[k] = REDACTED
            else:
                out[k] = _redact_mapping(v, keys)
        return out
    if isinstance(d, list):
        return [_redact_mapping(v, keys) for v in d]
    return d


def _is_secret(key: str, keys: frozenset[str]) -> bool:
    low = key.lower()
    if low in keys:
        return True
    return bool(keys & set(low.replace("-", "_").split("_")))


def redact_event(event: JourneyEvent, keys: frozenset[str]) -> JourneyEvent:
    """Mask secrets in the structured corners of an event.

    Deliberately does NOT touch ``message.content``: that is the training data,
    and blanket-redacting prose would quietly destroy the corpus. Structured
    fields — metadata, tool arguments, tool responses — are where credentials
    actually end up.
    """
    if not keys:
        return event
    ev = event
    if ev.metadata:
        ev = dataclasses.replace(ev, metadata=_redact_mapping(ev.metadata, keys))
    msg = ev.message
    if msg is None:
        return ev

    changed: Dict[str, Any] = {}
    if msg.metadata:
        changed["metadata"] = _redact_mapping(msg.metadata, keys)
    if msg.tool_calls:
        changed["tool_calls"] = [
            dataclasses.replace(tc, arguments=_redact_mapping(tc.arguments, keys))
            for tc in msg.tool_calls
        ]
    if msg.tool_response is not None:
        tr = msg.tool_response
        changed["tool_response"] = dataclasses.replace(
            tr,
            arguments=_redact_mapping(tr.arguments, keys),
            response=_redact_mapping(tr.response, keys),
            metadata=_redact_mapping(tr.metadata, keys) if tr.metadata else tr.metadata,
        )
    if not changed:
        return ev
    return dataclasses.replace(ev, message=dataclasses.replace(msg, **changed))


# ---------------------------------------------------------------------------
# Path containment
# ---------------------------------------------------------------------------


def safe_child(root: Path, *parts: str) -> Path:
    """Resolve a path under ``root``, rejecting traversal and symlink escapes.

    Note what this is NOT: Soup's ``is_under_cwd``, enforced at 286 call sites,
    which pins every artifact to the process working directory and would reject
    odyssey's spool outright. The containment root here is configured, not the
    cwd. The symlink/junction rejection is kept, because that half is real.
    """
    root_r = root.resolve()
    candidate = root_r.joinpath(*parts)
    resolved = candidate.resolve()
    if resolved != root_r and root_r not in resolved.parents:
        raise SpoolPathError(f"{candidate} escapes spool root {root_r}")
    # Reject a symlink or junction anywhere in the segments we own.
    probe = root_r
    for part in parts:
        probe = probe / part
        if probe.is_symlink():
            raise SpoolPathError(f"{probe} is a symlink; refusing to write through it")
    return candidate


# ---------------------------------------------------------------------------
# Spool
# ---------------------------------------------------------------------------


@dataclass
class _ShardState:
    """A journey's active shard, held open between writes.

    ``size`` is tracked rather than ``stat()``-ed. Re-statting per event was 94%
    of the old record() cost, alongside the mkdir/resolve/glob it also repeated.
    """

    path: Path
    handle: TextIO
    size: int


class Spool:
    """Append-only local event capture with a per-journey watermark."""

    def __init__(self, config: SpoolConfig) -> None:
        self._cfg = config
        self._lock = threading.Lock()
        self._root = Path(config.root)
        (self._root / "journeys").mkdir(parents=True, exist_ok=True)
        # Insertion-ordered so popitem(last=False) evicts least-recently-written.
        self._open: "OrderedDict[str, _ShardState]" = OrderedDict()

    # -- paths ------------------------------------------------------------

    @property
    def root(self) -> Path:
        return self._root

    def _journey_dir(self, journey_id: str) -> Path:
        if not journey_id or "/" in journey_id or journey_id in (".", ".."):
            raise SpoolPathError(f"unusable journey_id {journey_id!r}")
        d = safe_child(self._root, "journeys", journey_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def shards(self, journey_id: str) -> List[Path]:
        d = self._root / "journeys" / journey_id
        return sorted(d.glob("*.jsonl")) if d.exists() else []

    def journey_ids(self) -> List[str]:
        base = self._root / "journeys"
        return (
            sorted(p.name for p in base.iterdir() if p.is_dir())
            if base.exists()
            else []
        )

    def _active_shard(self, journey_id: str) -> Path:
        d = self._journey_dir(journey_id)
        existing = sorted(d.glob("*.jsonl"))
        if not existing:
            return d / "000.jsonl"
        last = existing[-1]
        if last.stat().st_size >= self._cfg.max_shard_bytes:
            return d / f"{int(last.stem) + 1:03d}.jsonl"
        return last

    # -- cached shard handles ---------------------------------------------
    #
    # Everything below assumes the caller already holds self._lock.

    def _append(self, st: _ShardState, text: str) -> None:
        st.handle.write(text)
        # Flush every event: the OS buffer is what makes a killed process lose
        # nothing, and that guarantee predates this cache.
        st.handle.flush()
        if self._cfg.fsync:
            os.fsync(st.handle.fileno())
        st.size += len(text.encode("utf-8"))

    def _open_shard(self, journey_id: str, path: Path) -> _ShardState:
        size = path.stat().st_size if path.exists() else 0
        st = _ShardState(path=path, handle=path.open("a", encoding="utf-8"), size=size)
        self._open[journey_id] = st
        if size == 0:
            self._append(st, header_line() + "\n")
        return st

    @staticmethod
    def _close_state(st: _ShardState) -> None:
        try:
            st.handle.close()
        except OSError:
            pass

    def _rotate(self, journey_id: str, st: _ShardState) -> _ShardState:
        self._close_state(st)
        nxt = st.path.parent / f"{int(st.path.stem) + 1:03d}.jsonl"
        return self._open_shard(journey_id, nxt)

    def _evict(self) -> None:
        while len(self._open) > self._cfg.max_open_shards:
            _jid, victim = self._open.popitem(last=False)
            self._close_state(victim)

    def _shard_state(self, journey_id: str) -> _ShardState:
        st = self._open.get(journey_id)
        if st is not None:
            self._open.move_to_end(journey_id)
            if st.size >= self._cfg.max_shard_bytes:
                st = self._rotate(journey_id, st)
            return st
        # Cold path, once per journey: this is where journey_id validation and
        # path containment happen. A cached entry cannot exist without having
        # passed them.
        st = self._open_shard(journey_id, self._active_shard(journey_id))
        self._evict()
        return st

    # -- write ------------------------------------------------------------

    def record(self, event: JourneyEvent) -> None:
        """Append one event. Local only, O(1), safe across threads."""
        redacted = redact_event(event, self._cfg.redact_keys)
        line = encode_event(redacted) + "\n"
        with self._lock:
            self._append(self._shard_state(event.journey_id), line)

    def close(self, journey_id: Optional[str] = None) -> None:
        """Release cached shard handles. Every event is already on disk.

        Call it when a journey ends, or for the whole spool at shutdown. Writing
        again after a close simply reopens — closing is never destructive.
        """
        with self._lock:
            if journey_id is None:
                for st in self._open.values():
                    self._close_state(st)
                self._open.clear()
                return
            st = self._open.pop(journey_id, None)
            if st is not None:
                self._close_state(st)

    def open_shard_count(self) -> int:
        with self._lock:
            return len(self._open)

    def record_all(self, events: Iterable[JourneyEvent]) -> int:
        n = 0
        for e in events:
            self.record(e)
            n += 1
        return n

    # -- read -------------------------------------------------------------

    def read(self, journey_id: str) -> List[JourneyEvent]:
        """Every event on disk for a journey, across all shards, seq-ordered."""
        events: List[JourneyEvent] = []
        for shard in self.shards(journey_id):
            events.extend(read_events(shard).events)
        return sorted(events, key=lambda e: e.seq)

    def highest_seq(self, journey_id: str) -> Optional[int]:
        """Highest ``seq`` already persisted, or ``None``. Seeds the allocator.

        Newest shard first: this spool writes monotonically, so the last shard
        carries the maximum and a long journey costs one shard read rather than
        a full scan. Falls back to older shards only if that one yields nothing.
        """
        for shard in reversed(self.shards(journey_id)):
            try:
                seqs = [e.seq for e in read_events(shard).events]
            except (OSError, ValueError):
                # A half-written or unreadable shard must not break seeding.
                continue
            if seqs:
                return max(seqs)
        return None

    # -- watermarks -------------------------------------------------------

    @property
    def _watermark_path(self) -> Path:
        return self._root / "watermarks.json"

    def _watermarks(self) -> Dict[str, int]:
        p = self._watermark_path
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            return {}
        return (
            {str(k): int(v) for k, v in data.items()} if isinstance(data, dict) else {}
        )

    def watermark(self, journey_id: str) -> Optional[int]:
        """Highest ``seq`` the sink has acknowledged, or None."""
        return self._watermarks().get(journey_id)

    def _set_watermark(self, journey_id: str, seq: int) -> None:
        with self._lock:
            marks = self._watermarks()
            if seq > marks.get(journey_id, -1):
                marks[journey_id] = seq
                tmp = self._watermark_path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(marks, sort_keys=True), encoding="utf-8")
                tmp.replace(self._watermark_path)

    def undrained(self, journey_id: str) -> List[JourneyEvent]:
        """Events past the acknowledged watermark — what a drain would send."""
        mark = self.watermark(journey_id)
        events = self.read(journey_id)
        return events if mark is None else [e for e in events if e.seq > mark]

    # -- drain ------------------------------------------------------------

    def push(self, sink: Sink, *, journey_id: Optional[str] = None) -> DrainResult:
        """Drain now. The explicit trigger; identical path to interval and CLI."""
        return drain(self, sink, journey_id=journey_id)


def drain(
    spool: Spool,
    sink: Sink,
    *,
    journey_id: Optional[str] = None,
) -> DrainResult:
    """Send undrained events to ``sink``, advancing watermarks only on success.

    One implementation, three callers: ``Spool.push()``, the CLI, and the interval
    drainer. A failure leaves the shard and the watermark untouched, so the next
    drain retries the same events.
    """
    targets = [journey_id] if journey_id else spool.journey_ids()
    pushed = skipped = failed = 0
    errors: List[str] = []
    gaps: Dict[str, List[int]] = {}
    touched: List[str] = []

    for jid in targets:
        events = spool.undrained(jid)
        if not events:
            continue
        touched.append(jid)

        missing = _missing_seqs(spool.read(jid))
        if missing:
            gaps[jid] = missing

        try:
            sink.send(jid, events)
        except Exception as exc:  # noqa: BLE001 - any sink failure is retryable
            failed += len(events)
            errors.append(f"{jid}: {type(exc).__name__}: {exc}")
            continue
        spool._set_watermark(jid, max(e.seq for e in events))
        pushed += len(events)

    return DrainResult(
        pushed=pushed,
        skipped=skipped,
        failed=failed,
        errors=errors,
        gaps=gaps,
        journeys=touched,
    )


def _missing_seqs(events: List[JourneyEvent]) -> List[int]:
    if not events:
        return []
    present = {e.seq for e in events}
    return sorted(set(range(0, max(present) + 1)) - present)


def validate_interval(seconds: float) -> float:
    """Bounds-check a drain interval. Raises rather than silently clamping."""
    if not MIN_DRAIN_INTERVAL <= seconds <= MAX_DRAIN_INTERVAL:
        raise ValueError(
            f"drain interval {seconds}s is outside "
            f"[{MIN_DRAIN_INTERVAL}, {MAX_DRAIN_INTERVAL}]"
        )
    return seconds


class IntervalDrainer:
    """Background drain on a fixed interval. The third trigger.

    A daemon thread rather than a scheduler: it calls the same ``drain()`` every
    other trigger uses, so there is exactly one code path to reason about.
    """

    def __init__(self, spool: Spool, sink: Sink, interval_seconds: float) -> None:
        self._spool = spool
        self._sink = sink
        self._interval = validate_interval(interval_seconds)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_result: Optional[DrainResult] = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("drainer already started")
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            self.last_result = drain(self._spool, self._sink)
