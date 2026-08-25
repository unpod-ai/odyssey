"""Ambient journey context — the layer that makes recording invisible.

Without this module, every call site has to know its ``journey_id`` and track a
monotonic ``seq`` by hand. That is instrumentation sprinkled across the app,
which is the thing a single integration point exists to avoid.

Three pieces:

- :class:`SeqAllocator` — hands out ``seq`` per journey, seeded from disk so a
  process restart continues the sequence instead of colliding with it.
- :class:`JourneyContext` — what is "currently being recorded", held in a
  :class:`~contextvars.ContextVar` so nothing has to be passed down a call stack.
- ``writer_id`` — who wrote an event. Stamped into ``JourneyEvent.metadata``
  rather than a new schema field, so the wire format stays at v1.0 while a
  two-writer conflict is still detectable at fold time.

**Single writer per journey is a contract.** The allocator is per-process: two
processes writing one journey would both seed from the same on-disk maximum and
issue the same ``seq``. That is why ``writer_id`` exists — the fold detects the
collision and refuses the journey instead of silently training on a corrupted
conversation. See ``diagnostics.writer_conflict``.

This module performs no I/O of its own. Seeding is delegated through a callback,
which keeps the whole module testable without a filesystem.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional

from odyssey.primitives import WRITER_META_KEY, JourneyHeader

__all__ = [
    "WRITER_META_KEY",
    "JourneyContext",
    "JourneyHeader",
    "SeqAllocator",
    "bind",
    "current",
    "reset_current",
    "set_current",
]


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class SeqAllocator:
    """Monotonic ``seq`` per ``journey_id``, seeded from disk on first touch.

    ``seed_fn(journey_id)`` returns the highest ``seq`` already persisted for
    that journey, or ``None`` when nothing is. It is called at most once per
    journey per process.

    The lock is held across that seed call. Deliberate: releasing it would let
    two threads seed the same journey concurrently and hand out the same number.
    The cost is one directory read on a journey's first event, never again.
    """

    def __init__(self, seed_fn: Callable[[str], Optional[int]]) -> None:
        self._seed_fn = seed_fn
        self._next: Dict[str, int] = {}
        self._lock = threading.Lock()

    def next(self, journey_id: str) -> int:
        """The next ``seq`` for this journey. Thread-safe, never returns twice."""
        with self._lock:
            nxt = self._next.get(journey_id)
            if nxt is None:
                # A seed failure must not break recording: starting at 0 on a
                # fresh journey is right, and on a resumed one the fold's gap
                # and duplicate handling absorbs the overlap.
                try:
                    highest = self._seed_fn(journey_id)
                except Exception:
                    highest = None
                nxt = 0 if highest is None else highest + 1
            self._next[journey_id] = nxt + 1
            return nxt

    def peek(self, journey_id: str) -> Optional[int]:
        """The next ``seq`` without consuming it, or ``None`` if never touched."""
        with self._lock:
            return self._next.get(journey_id)

    def forget(self, journey_id: str) -> None:
        """Drop cached state for a finished journey, so it re-seeds if reopened."""
        with self._lock:
            self._next.pop(journey_id, None)

    def tracked(self) -> List[str]:
        with self._lock:
            return sorted(self._next)


@dataclass
class JourneyContext:
    """The journey currently being recorded on this task or thread.

    Mutable on purpose: ``prefix_len`` and ``terminated`` change as the journey
    progresses, and every holder of this context must see the same values.
    """

    journey_id: str
    allocator: SeqAllocator
    # Caller-supplied tags (user_id, session_id, ...). Snapshotted into the shard
    # header at construction; only what changes after that rides on an event.
    metadata: Dict[str, Any] = field(default_factory=dict)

    # Journey identity. These cannot change once recording starts, which is why
    # they belong in the header rather than on every event: a file that repeats
    # its own name 29 times still cannot say what it is when you read line 1.
    #
    # `data_source` in particular is what `fold()` used to demand from whoever
    # happened to call the reader, so two callers could fold one file into two
    # differently-labelled journeys and neither was wrong.
    data_source: Optional[str] = None
    trace_id: Optional[str] = None
    started_at: str = field(default_factory=_utc_now_iso)
    # SDK bookkeeping — integration state, seen-system-prompt, and so on.
    # Deliberately separate from ``metadata``: that one is emitted, this one is
    # not, and mixing them would smear internal counters across the corpus.
    state: Dict[str, Any] = field(default_factory=dict)

    # How many request messages have already been recorded for this journey.
    # Provider APIs resend the whole conversation on every call; without this,
    # auto-capture would re-record history on turn 2. The read side solves the
    # same problem in build_cumulative_steps; this is the write-side half.
    prefix_len: int = 0

    # Set once a terminal event has been emitted, so exiting a nested block or
    # an explicit close() cannot emit a second one.
    terminated: bool = False

    # The seq of the most recent message event. A signal targets "the answer we
    # just gave" far more often than an explicit number, so this is the default
    # target for signal() and keeps thumbs-up out of the caller's bookkeeping.
    last_message_seq: Optional[int] = None

    # Nesting depth. Only the outermost block emits the terminal event.
    depth: int = 0

    # Snapshot of `metadata` as of the first event, and the header built from it.
    # Both are filled on first use and never recomputed: the header is written
    # once per shard, so a tag added afterwards has nowhere to land in it.
    _header: Optional[JourneyHeader] = field(default=None, repr=False)

    def next_seq(self) -> int:
        return self.allocator.next(self.journey_id)

    def header(self) -> JourneyHeader:
        """The shard header for this journey. Built once, then reused.

        Memoized rather than rebuilt per event both because it is written at most
        once per shard and because the memo *is* the snapshot: it freezes the
        journey-level tags at the moment of the first event, which is what makes
        :meth:`event_metadata` able to tell a later tag apart from an original.
        """
        if self._header is None:
            self._header = JourneyHeader(
                journey_id=self.journey_id,
                data_source=self.data_source,
                trace_id=self.trace_id,
                started_at=self.started_at,
                journey_metadata=dict(self.metadata) or None,
            )
        return self._header

    def event_metadata(self) -> Dict[str, Any]:
        """Caller tags this event must carry because the header does not.

        Empty for the common case: everything passed to ``journey()`` or
        ``attach()`` is already in the header, so repeating it on all N events
        buys nothing and costs the majority of every line.

        A tag added or changed mid-journey — ``journey()`` nesting merges into
        ``metadata``, and a handoff can retag — is a genuine delta, and the
        header for this shard was written before it existed. Those ride on the
        event, where a reader applying header-then-event in order gets the value
        that was true at that seq.
        """
        snapshot = self.header().journey_metadata or {}
        return {k: v for k, v in self.metadata.items() if snapshot.get(k) != v}


_current: ContextVar[Optional[JourneyContext]] = ContextVar(
    "odyssey_journey", default=None
)


def current() -> Optional[JourneyContext]:
    """The active :class:`JourneyContext`, or ``None`` outside a journey."""
    return _current.get()


def set_current(ctx: Optional[JourneyContext]) -> Token:
    return _current.set(ctx)


def reset_current(token: Token) -> None:
    _current.reset(token)


@contextmanager
def bind(ctx: Optional[JourneyContext]) -> Iterator[Optional[JourneyContext]]:
    """Re-establish a journey context on another thread.

    ``ContextVar`` propagates into ``asyncio`` tasks automatically, but **not**
    into a new ``threading.Thread`` or a ``run_in_executor`` callback — those
    start from defaults. Hand the context across explicitly::

        ctx = odyssey.context.current()
        def worker():
            with odyssey.context.bind(ctx):
                ...   # records into the same journey
    """
    token = set_current(ctx)
    try:
        yield ctx
    finally:
        reset_current(token)
