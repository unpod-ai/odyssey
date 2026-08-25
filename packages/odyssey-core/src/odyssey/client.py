"""The process-wide client: one :func:`init` call, everything else is ambient.

This module owns lifecycle — the spool, the seq allocator, the background
drainer, the exit hook, and the counters that make a silent capture layer
visible. It is the only place in the library that holds mutable global state, and
that is deliberate: a single integration point means callers do not thread a
client object through their code.

**Never crash the host.** An observability layer that takes down the application
it observes is worse than no observability layer. Failures here are counted and
surfaced through :func:`odyssey.health`, not raised — unless ``ODYSSEY_DEBUG=1``,
which re-raises so a developer can see the problem during development.
"""

from __future__ import annotations

import atexit
import signal
import threading
import traceback
import warnings
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Sequence, runtime_checkable
from uuid import uuid4

from odyssey.config import Config, resolve
from odyssey.context import SeqAllocator
from odyssey.primitives import TerminationReason
from odyssey.sinks import FileSink
from odyssey.spool import DrainResult, IntervalDrainer, Sink, Spool, SpoolConfig

# How many recent failures to keep with their tracebacks. Enough to diagnose a
# repeating fault, small enough to never be a memory concern.
_ERROR_RING = 5

# What a journey that was never closed is stamped with at process exit. `STALE`
# is the schema's word for "recording stopped without an ending", and it is what
# `capture.journey()` already uses for an abandoned scope.
_ABANDONED = (
    "recording ended at process exit without the session closing its journey"
)


@runtime_checkable
class JourneyCloser(Protocol):
    """Something holding an open journey that shutdown must be able to end.

    Integrations register themselves so a journey whose provider never fired its
    own close event still gets a terminal event. Without one, ``fold()`` cannot
    tell "still running" from "lost the tail" and refuses to export the journey —
    permanently, since nothing will ever arrive to complete it.
    """

    def close(
        self, *, reason: TerminationReason = ..., error: Optional[str] = ...
    ) -> None: ...


@dataclass
class Stats:
    """Counters that turn silent failure into visible failure."""

    events_recorded: int = 0
    events_dropped: int = 0
    journeys_started: int = 0
    capture_errors: int = 0
    recent_errors: List[str] = field(default_factory=list)

    def note_error(self, label: str, exc: BaseException) -> None:
        self.capture_errors += 1
        detail = f"{label}: {type(exc).__name__}: {exc}"
        tb = traceback.format_exc(limit=4).strip().splitlines()
        if tb:
            detail = f"{detail}\n    {tb[-1].strip()}"
        self.recent_errors.append(detail)
        del self.recent_errors[:-_ERROR_RING]


class Client:
    """Holds everything a recording process needs. Created by :func:`init`."""

    def __init__(self, config: Config, sink: Optional[Sink] = None) -> None:
        self.config = config
        # Short, human-quotable, and unique per process. Stamped on every event
        # so a two-writer collision is provable rather than suspected.
        self.writer_id = uuid4().hex[:12]
        self.stats = Stats()
        self._stats_lock = threading.Lock()
        self._closed = False

        self.spool = Spool(
            SpoolConfig(
                root=config.spool_dir,
                redact_keys=config.redact_keys,
                fsync=config.fsync,
                max_open_shards=config.max_open_shards,
            )
        )
        self.allocator = SeqAllocator(self.spool.highest_seq)
        self.sink: Sink = sink if sink is not None else FileSink(config.out_dir)

        self.drainer: Optional[IntervalDrainer] = None
        if config.drain_interval is not None:
            self.drainer = IntervalDrainer(self.spool, self.sink, config.drain_interval)
            self.drainer.start()

        # Recorders holding an open journey. Weak on purpose: the session keeps
        # its recorder alive through the bound handlers it registered, so
        # liveness here means "still recording". A recorder the app dropped is
        # collected, and its journey was already unreachable.
        self._open_journeys: "weakref.WeakSet[JourneyCloser]" = weakref.WeakSet()

        self._prev_sigterm: Any = None
        if config.handle_sigterm:
            self._install_sigterm()

    # -- open journeys ----------------------------------------------------

    def register_journey(self, closer: JourneyCloser) -> None:
        """Track a recorder so shutdown can end its journey if nothing else did."""
        self._open_journeys.add(closer)

    def unregister_journey(self, closer: JourneyCloser) -> None:
        """Stop tracking a recorder that closed or detached on its own."""
        self._open_journeys.discard(closer)

    def close_open_journeys(self) -> int:
        """Terminate every journey still open. Returns how many were closed.

        Runs before the final flush so the terminal events are in the spool when
        it drains. Each closer is idempotent, so a session that already fired its
        own close event is a no-op and keeps its real termination reason — this
        only reaches journeys nothing else ended.
        """
        closed = 0
        for closer in list(self._open_journeys):
            try:
                closer.close(reason="STALE", error=_ABANDONED)
                closed += 1
            except Exception as exc:  # noqa: BLE001 - shutdown must never raise
                self.note_error("close_open_journeys", exc)
        self._open_journeys.clear()
        return closed

    # -- counters ---------------------------------------------------------

    def count_recorded(self, n: int = 1) -> None:
        with self._stats_lock:
            self.stats.events_recorded += n

    def count_dropped(self, n: int = 1) -> None:
        with self._stats_lock:
            self.stats.events_dropped += n

    def count_journey(self) -> None:
        with self._stats_lock:
            self.stats.journeys_started += 1

    def note_error(self, label: str, exc: BaseException) -> None:
        """Record a swallowed failure. Re-raises when debug is on."""
        with self._stats_lock:
            self.stats.note_error(label, exc)
        if self.config.debug:
            raise exc

    # -- lifecycle --------------------------------------------------------

    def _install_sigterm(self) -> None:
        """Drain on SIGTERM, then defer to whatever handler was already there.

        ``atexit`` covers a normal exit and SIGINT (which unwinds as
        ``KeyboardInterrupt``), but **not** SIGTERM — the default action
        terminates the process immediately, so a container being stopped would
        lose everything still in the spool. Hijacking a signal from inside a
        library is rude, which is why this is opt-in and chains rather than
        replaces.

        Flush only — deliberately **not** ``close_open_journeys()``. The handler
        this chains to is usually the host's own graceful shutdown (LiveKit's
        installs one), which ends its sessions properly and emits a real
        termination reason. Stamping ``STALE`` here would win the idempotent race
        against it and mislabel a clean shutdown as an abandoned one.
        """
        try:
            previous = signal.getsignal(signal.SIGTERM)

            def handler(signum: int, frame: Any) -> None:
                try:
                    self.flush()
                finally:
                    if callable(previous):
                        previous(signum, frame)
                    elif previous == signal.SIG_DFL:
                        signal.signal(signal.SIGTERM, signal.SIG_DFL)
                        signal.raise_signal(signal.SIGTERM)

            self._prev_sigterm = previous
            signal.signal(signal.SIGTERM, handler)
        except (ValueError, OSError) as exc:
            # signal() only works on the main thread; a worker importing this is
            # not a reason to fail startup.
            self.note_error("install_sigterm", exc)

    def flush(self) -> DrainResult:
        """Drain everything now. Safe to call repeatedly."""
        if self.drainer is not None:
            self.drainer.stop()
            self.drainer = None
        try:
            return self.spool.push(self.sink)
        except Exception as exc:  # noqa: BLE001 - flush must never raise
            self.note_error("flush", exc)
            return DrainResult(errors=[f"flush: {type(exc).__name__}: {exc}"])

    def shutdown(self) -> DrainResult:
        """End open journeys, flush, then release every cached shard handle.

        Closing comes first so the terminal events are on disk before the drain
        reads them. A journey that leaves this process without one is not merely
        untidy: ``fold()`` reports it incomplete and refuses to export it, and
        nothing will ever arrive to complete it.

        SIGTERM deliberately does not do this — see :meth:`_install_sigterm`.
        """
        self.close_open_journeys()
        result = self.flush()
        self.spool.close()
        self._closed = True
        return result

    def health(self) -> Dict[str, Any]:
        with self._stats_lock:
            stats = dict(
                events_recorded=self.stats.events_recorded,
                events_dropped=self.stats.events_dropped,
                journeys_started=self.stats.journeys_started,
                capture_errors=self.stats.capture_errors,
                recent_errors=list(self.stats.recent_errors),
            )
        journeys = self.allocator.tracked()
        return {
            "enabled": self.config.enabled,
            "writer_id": self.writer_id,
            "spool_dir": str(self.config.spool_dir),
            "out_dir": str(self.config.out_dir),
            "drain_interval": self.config.drain_interval,
            "drainer_running": self.drainer is not None,
            "debug": self.config.debug,
            "closed": self._closed,
            "open_shards": self.spool.open_shard_count(),
            # Journeys with no terminal event yet. A number that only grows is a
            # leak of unexportable journeys, otherwise invisible until someone
            # folds them and finds every one refused.
            "open_journeys": len(self._open_journeys),
            "journeys_in_process": journeys,
            "next_seq": {j: self.allocator.peek(j) for j in journeys},
            "undrained": {j: len(self.spool.undrained(j)) for j in journeys},
            "last_drain": (
                None
                if self.drainer is None or self.drainer.last_result is None
                else {
                    "pushed": self.drainer.last_result.pushed,
                    "failed": self.drainer.last_result.failed,
                    "ok": self.drainer.last_result.ok,
                }
            ),
            "stats": stats,
        }


# ---------------------------------------------------------------------------
# The process-wide singleton
# ---------------------------------------------------------------------------

_client: Optional[Client] = None
_client_lock = threading.Lock()
_warned_uninitialised = False


def init(
    *,
    spool_dir: Optional[Path | str] = None,
    out_dir: Optional[Path | str] = None,
    sink: Optional[Sink] = None,
    drain_interval: Optional[float] = 30.0,
    instrument: Sequence[str] = (),
    enabled: Optional[bool] = None,
    flush_on_exit: bool = True,
    handle_sigterm: bool = False,
    debug: Optional[bool] = None,
    max_open_shards: Optional[int] = None,
    redact_keys: Optional[frozenset] = None,
    fsync: bool = False,
    force: bool = False,
) -> Client:
    """Start recording. Call once, as early as the process allows.

    Every argument has an ``ODYSSEY_*`` environment equivalent; explicit
    arguments win. ``drain_interval=None`` disables the background drain, in
    which case nothing leaves the spool until :func:`flush` or the CLI runs.

    ``instrument`` opt-in patches provider SDKs in place — ``["anthropic"]``
    makes every existing ``anthropic`` client record without touching app code.
    The explicit drop-in (``from odyssey.integrations.anthropic import
    Anthropic``) is the default path because a patched call stack is harder to
    debug; reach for patching when you cannot edit the call sites.

    Calling twice is a no-op that warns and returns the existing client, because
    a second one would start a second drainer. Pass ``force=True`` to replace.
    """
    global _client
    with _client_lock:
        if _client is not None and not force:
            warnings.warn(
                "odyssey.init() called more than once; returning the existing "
                "client. Pass force=True to replace it.",
                RuntimeWarning,
                stacklevel=2,
            )
            return _client
        if _client is not None:
            _client.shutdown()

        config = resolve(
            spool_dir=spool_dir,
            out_dir=out_dir,
            drain_interval=drain_interval,
            enabled=enabled,
            flush_on_exit=flush_on_exit,
            handle_sigterm=handle_sigterm,
            debug=debug,
            max_open_shards=max_open_shards,
            redact_keys=redact_keys,
            fsync=fsync,
        )
        client = Client(config, sink=sink)
        _client = client

    if config.flush_on_exit:
        atexit.register(_atexit_flush, client)
    for name in instrument:
        _instrument(name, client)
    return client


def _atexit_flush(client: Client) -> None:
    # Bound to the client it was registered with, so a force-replaced client
    # still gets flushed rather than the hook silently targeting the new one.
    try:
        client.shutdown()
    except Exception:  # noqa: BLE001 - an exit hook must never raise
        pass


def _instrument(name: str, client: Client) -> None:
    key = name.strip().lower()
    try:
        if key == "anthropic":
            from odyssey.integrations.anthropic import instrument

            instrument()
        else:
            raise ValueError(f"unknown instrumentation target {name!r}")
    except Exception as exc:  # noqa: BLE001 - a missing provider is not fatal
        client.note_error(f"instrument:{key}", exc)


def get_client() -> Optional[Client]:
    """The active client, or ``None`` when :func:`init` was never called."""
    return _client


def require_client() -> Optional[Client]:
    """The active client, warning exactly once if there is none.

    Returning ``None`` rather than raising is the whole not-initialised policy:
    an app that forgot to call :func:`init` keeps working, minus recording.
    """
    global _warned_uninitialised
    client = _client
    if client is None and not _warned_uninitialised:
        _warned_uninitialised = True
        warnings.warn(
            "odyssey is recording nothing: odyssey.init() was never called. "
            "Add it once at process start.",
            RuntimeWarning,
            stacklevel=3,
        )
    return client


def flush() -> Optional[DrainResult]:
    """Drain the spool now. No-op when uninitialised."""
    client = _client
    return None if client is None else client.flush()


def shutdown() -> Optional[DrainResult]:
    """End open journeys, flush, close handles, and clear the singleton."""
    global _client, _warned_uninitialised
    client = _client
    if client is None:
        with _client_lock:
            _warned_uninitialised = False
        return None
    # Before the singleton is cleared, not after. A terminal event travels the
    # same ambient path as every other event, so a recorder closed once
    # `require_client()` returns None emits nothing at all — the journey would be
    # left exactly as unexportable as if shutdown had never tried.
    client.close_open_journeys()
    with _client_lock:
        if _client is client:
            _client = None
        _warned_uninitialised = False
    return client.shutdown()


def health() -> Dict[str, Any]:
    """A snapshot of what the capture layer is doing, including its failures."""
    client = _client
    if client is None:
        return {"initialised": False, "enabled": False}
    report = {"initialised": True}
    report.update(client.health())
    return report
