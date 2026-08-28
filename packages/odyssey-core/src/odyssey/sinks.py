"""Drain destinations. A sink is one method: ``send(journey_id, events)``.

Sinks live here rather than in ``cli.py`` so the library never imports the
command line. ``odyssey.cli.FileSink`` stays importable — it re-exports this one.

Raise to signal failure; never return false. ``drain()`` treats an exception as
retryable and leaves both the shard and the watermark untouched, so the next
drain re-sends the same events. A returned boolean would be ignored.
"""

from __future__ import annotations

import email.utils
import gzip
import http.client
import os
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from odyssey.jsonl import encode_event, header_line, write_events
from odyssey.primitives import JourneyEvent, JourneyHeader


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

    def send(
        self,
        journey_id: str,
        events: List[JourneyEvent],
        header: Optional[JourneyHeader] = None,
    ) -> None:
        """Append this batch, writing the header only when creating the file.

        A resumed drain sends the tail into a file that already carries the
        header from the first drain, so ``write_events`` skipping it on append is
        what keeps a multi-drain output a single valid document rather than a
        header interleaved with events.
        """
        write_events(
            self.out_dir / f"{journey_id}.jsonl", events, append=True, header=header
        )


# Env fallback, self-contained on the sink rather than routed through
# `config.py`/`Client` — this needs no new abstraction (WORKING.md item 1.5):
# a caller builds `HttpSink()` and hands it to `init(sink=...)` exactly like any
# other sink. `explicit beats environment` still holds, just locally.
ENV_ENDPOINT = "ODYSSEY_ENDPOINT"
ENV_API_KEY = "ODYSSEY_API_KEY"
DEFAULT_TIMEOUT = 10.0


class HttpSinkError(RuntimeError):
    """A batch could not be delivered. Always retryable — see the module docstring."""


class HttpSink:
    """Ships drained events to a network endpoint over plain stdlib HTTP.

    ``odyssey-core`` declares ``dependencies = []``; this uses ``urllib`` rather
    than ``requests``/``httpx`` so installing the SDK never pulls in an HTTP
    client nobody asked for.

    One POST per journey batch, to ``{endpoint}/journeys/{journey_id}/events``.
    The body is the same JSONL bytes a shard on disk would hold — the header
    line, then one event per line, produced by the same :func:`header_line` /
    :func:`encode_event` :class:`FileSink` uses — so a collector need not
    decode anything odyssey does not already write to disk.

    Raises :class:`HttpSinkError` on any non-2xx response or transport
    failure. ``drain()`` treats that as retryable: the shard and watermark are
    left untouched, and the next drain resends the same batch. No retry or
    backoff lives here — the spool already is the retry queue (see
    ``spool.py``).

    **Compression** (item 1.7): the body is gzipped by default
    (``compress=True``), stdlib ``gzip`` only — no new dependency. A
    collector must decompress on ``Content-Encoding: gzip`` to stay in sync;
    ``services/collector`` does.

    **Backpressure** (item 1.7): a 429 response's own ``Retry-After`` header
    (seconds or an HTTP-date) is honoured — this sink refuses to attempt
    another request before that time, raising :class:`HttpSinkError`
    immediately rather than making a network call the server already asked
    it not to make. This is not a queueing system.

    **Cross-journey overhead** (item 1.7): still one POST per journey, not
    one POST for many — merging payloads was ruled out because it would
    need a real redesign of ``drain()``'s per-journey watermark/retry
    semantics to stay correct across a partial failure (see
    ``docs/WORKING.md`` item 1.7). Instead, the connection itself is reused
    across ``send()`` calls (``http.client.HTTPConnection`` with HTTP/1.1
    keep-alive, ``services/collector`` opts in on its side) — draining N
    journeys in one process pays for one TCP/TLS handshake, not N, without
    touching the watermark-per-request correctness guarantee at all: every
    journey's POST and response stay independent, so a failure on journey 3
    still leaves journeys 1/2's already-advanced watermarks untouched and
    journey 3 retried on the next drain, exactly as before. Not safe to call
    ``send()`` concurrently from two threads at once (the connection is
    shared, mutable state) — guarded by an internal lock, so a manual
    ``push()`` racing the background ``IntervalDrainer`` serializes rather
    than corrupting the connection, matching `drain()`'s own already-serial
    per-journey loop.
    """

    def __init__(
        self,
        endpoint: Optional[str] = None,
        *,
        api_key: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        compress: bool = True,
    ) -> None:
        resolved = endpoint if endpoint is not None else os.environ.get(ENV_ENDPOINT)
        if not resolved:
            raise ValueError(
                f"HttpSink needs an endpoint: pass endpoint=... or set {ENV_ENDPOINT}"
            )
        self.endpoint = resolved.rstrip("/")
        self.api_key = api_key if api_key is not None else os.environ.get(ENV_API_KEY)
        self.timeout = timeout
        self.compress = compress
        # Set by a 429's Retry-After; checked before the next send() so a
        # server that asked to be left alone is not immediately re-hit.
        self._retry_after_until: float = 0.0

        parsed = urlsplit(self.endpoint)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(
                f"HttpSink endpoint must be an http(s) URL: {self.endpoint!r}"
            )
        self._scheme = parsed.scheme
        self._host = parsed.hostname
        self._port = parsed.port
        self._base_path = parsed.path.rstrip("/")
        # The reused connection (item 1.7) and the lock guarding it — see the
        # class docstring's "Cross-journey overhead" section.
        self._conn: Optional[http.client.HTTPConnection] = None
        self._lock = threading.Lock()

    def _connect(self) -> http.client.HTTPConnection:
        cls = (
            http.client.HTTPSConnection
            if self._scheme == "https"
            else http.client.HTTPConnection
        )
        return cls(self._host, self._port, timeout=self.timeout)

    def close(self) -> None:
        """Release the connection reused across ``send()`` calls. Idempotent;
        safe to call even if nothing was ever sent."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def send(
        self,
        journey_id: str,
        events: List[JourneyEvent],
        header: Optional[JourneyHeader] = None,
    ) -> None:
        if time.monotonic() < self._retry_after_until:
            wait = self._retry_after_until - time.monotonic()
            raise HttpSinkError(
                f"{journey_id}: backing off {wait:.0f}s more per the server's "
                f"last Retry-After"
            )

        body = header_line(header=header) + "\n"
        body += "".join(encode_event(e) + "\n" for e in events)
        payload = body.encode("utf-8")
        headers = {"Content-Type": "application/x-ndjson; charset=utf-8"}
        if self.compress:
            payload = gzip.compress(payload)
            headers["Content-Encoding"] = "gzip"
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        path = (
            f"{self._base_path}/journeys/"
            f"{urllib.parse.quote(journey_id, safe='')}/events"
        )

        with self._lock:
            status, retry_after, error = self._request(path, payload, headers)

        if error is not None:
            raise HttpSinkError(
                f"{journey_id}: could not reach {self.endpoint}: {error}"
            )
        if status == 429:
            self._retry_after_until = time.monotonic() + _parse_retry_after(retry_after)
        if status is None or status >= 300:
            raise HttpSinkError(f"{journey_id}: HTTP {status} from {self.endpoint}")

    def _request(
        self, path: str, payload: bytes, headers: Dict[str, str]
    ) -> Tuple[Optional[int], Optional[str], Optional[str]]:
        """One POST, reusing ``self._conn`` when a prior request left it open.

        A kept-alive connection the server (or an idle intermediary) closed
        in the meantime raises on the *first* use after that, not at close
        time — so a dropped connection is retried once, transparently, with
        a fresh one, rather than surfacing as a spurious drain failure.
        """
        for attempt in range(2):
            if self._conn is None:
                self._conn = self._connect()
            try:
                self._conn.request("POST", path, body=payload, headers=headers)
                response = self._conn.getresponse()
                response.read()  # must drain the body to reuse the connection
                return response.status, response.getheader("Retry-After"), None
            except (http.client.HTTPException, OSError) as exc:
                if self._conn is not None:
                    self._conn.close()
                self._conn = None
                if attempt == 1:
                    return None, None, str(exc)
        return None, None, "unreachable"  # pragma: no cover - loop always returns


# A missing/malformed Retry-After still has to back off *something* -- a
# fixed floor rather than hammering the server with a 0-second retry, which
# is what "unknown" would otherwise mean.
_DEFAULT_RETRY_AFTER_SECONDS = 5.0


def _parse_retry_after(raw: Optional[str]) -> float:
    """RFC 9110 ``Retry-After``: either delay-seconds or an HTTP-date."""
    if not raw:
        return _DEFAULT_RETRY_AFTER_SECONDS
    raw = raw.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return _DEFAULT_RETRY_AFTER_SECONDS
    if when is None:
        return _DEFAULT_RETRY_AFTER_SECONDS
    delta = when.timestamp() - time.time()
    return max(0.0, delta)
