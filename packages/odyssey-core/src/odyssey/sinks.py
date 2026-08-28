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
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import List, Optional

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
    it not to make. This is not a queueing system: cross-journey batching
    (one POST for many journeys) is explicitly out of scope here, noted as
    still-open in ``docs/WORKING.md`` — `drain()`'s per-journey
    watermark/retry semantics would need a real redesign to batch safely
    across a partial failure.
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

        url = (
            f"{self.endpoint}/journeys/"
            f"{urllib.parse.quote(journey_id, safe='')}/events"
        )
        request = urllib.request.Request(
            url, data=payload, method="POST", headers=headers
        )
        if self.api_key:
            request.add_header("Authorization", f"Bearer {self.api_key}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                self._retry_after_until = time.monotonic() + _parse_retry_after(
                    exc.headers.get("Retry-After")
                )
            raise HttpSinkError(
                f"{journey_id}: HTTP {exc.code} from {self.endpoint}"
            ) from exc
        except urllib.error.URLError as exc:
            raise HttpSinkError(
                f"{journey_id}: could not reach {self.endpoint}: {exc.reason}"
            ) from exc
        if status >= 300:
            # Unreachable via urlopen for a plain 2xx/4xx/5xx server — HTTPError
            # already covers >=400, and urlopen follows redirects itself — but a
            # defensive check costs nothing against a server that returns an
            # unusual 3xx without a Location header.
            raise HttpSinkError(f"{journey_id}: unexpected HTTP status {status}")


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
