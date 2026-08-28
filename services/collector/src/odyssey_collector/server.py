"""odyssey-collector — the ingest endpoint ``odyssey.HttpSink`` talks to.

Not ``services/api`` (the read side, not built yet) and not a second
implementation of the wire format: this receives exactly what ``HttpSink``
posts and writes it through the same ``odyssey.jsonl`` functions ``FileSink``
already uses, so there is one codec, not two.

Deliberately ``http.server`` (stdlib) rather than FastAPI. The job here is
I/O — accept a JSONL POST, check a bearer token, persist bytes — not routing,
validation or DTOs, which is what ``services/api`` will actually need FastAPI
for once ``odyssey-schemas`` exists (see ``docs/WORKING.md`` items 8.2/1.8:
"the same server"). A framework commitment made here would likely be thrown
away or awkwardly merged when that lands; the wire contract below is what has
to stay stable in the meantime.

Storage today is a local directory, partitioned by date a batch was received
(UTC by default, ``ODYSSEY_COLLECTOR_TIMEZONE``/``--timezone`` to change it):
``<data_dir>/<YYYY-MM-DD>/<journey_id>.jsonl`` — files a shard on disk would
hold, just date-bucketed so neither a directory nor a single long-lived
journey_id's file grows without bound, and old dates are trivial to archive
or delete wholesale. A journey whose events straddle midnight splits across
two date directories, each a complete, independently-readable file in its own
right (own header, own contiguous seq range for what landed that day) — an
acceptable cost against the alternative of unbounded growth, and rare in
practice since a journey is normally one call or one session.
"spool -> object store" (``docs/STRUCTURE.md``'s stated destination for this
service) is a deliberately deferred upgrade: swap :meth:`_Handler._store`,
keep the endpoint contract.

Wire contract (matches ``odyssey.sinks.HttpSink`` exactly)::

    POST /journeys/<url-encoded journey_id>/events
    Content-Type: application/x-ndjson; charset=utf-8
    Content-Encoding: gzip                   # HttpSink's default (item 1.7); optional
    Authorization: Bearer <api_key>          # only when the server requires one
    <header line><event line>...             # exactly what a shard on disk holds

    200 {"journey_id": ..., "events_received": N}
    400 malformed batch — not valid odyssey JSONL, or a bad gzip body
    401 missing/incorrect Authorization, when the server requires one
    500 storage failure

No rate-limiting/backpressure lives here — ``HttpSink`` honours a 429's
``Retry-After`` if this server (or something in front of it) ever sends one,
but nothing here emits 429 itself; that is a deliberate scope cut (item 1.7),
not an oversight — this stdlib server has no queue-depth signal to base one
on.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional, Sequence
from urllib.parse import unquote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from odyssey.jsonl import (
    MalformedHeaderError,
    SchemaVersionError,
    read_events,
    write_events,
)

ENV_HOST = "ODYSSEY_COLLECTOR_HOST"
ENV_PORT = "ODYSSEY_COLLECTOR_PORT"
ENV_DATA_DIR = "ODYSSEY_COLLECTOR_DATA_DIR"
ENV_API_KEY = "ODYSSEY_COLLECTOR_API_KEY"
ENV_TIMEZONE = "ODYSSEY_COLLECTOR_TIMEZONE"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_DATA_DIR = "./collector-data"
DEFAULT_TIMEZONE = "UTC"


def _make_date_fn(tz_name: Optional[str]) -> Callable[[], str]:
    """Resolve a timezone once and return a cheap closure over it.

    Which day a batch's date-partition belongs to — UTC by default, since a
    shared server can receive traffic from writers in any timezone and the
    partition has to mean the same thing regardless of which one sent it.
    Mirrors odyssey.spool._make_date_fn; not imported from it, since it's
    private to a different package for an unrelated concern.
    """
    name = (
        tz_name
        if tz_name is not None
        else os.environ.get(ENV_TIMEZONE, DEFAULT_TIMEZONE)
    )
    try:
        tz = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo(DEFAULT_TIMEZONE)

    def date_fn() -> str:
        return datetime.now(tz).date().isoformat()

    return date_fn


def _safe_stem(journey_id: str) -> str:
    """``journey_id`` as a filename-safe stem, path traversal stripped.

    journey_ids are caller-chosen and nothing stops one holding a separator:
    written naively, ``a/b`` silently creates a subdirectory and
    ``../../etc/passwd`` escapes ``data_dir`` entirely. Same defense as
    ``odyssey.export._filename``, reimplemented rather than imported — this
    service treats odyssey-core's export module as someone else's exporter,
    not a place to reach into privates for an unrelated concern.
    """
    segments = [
        seg
        for seg in journey_id.strip().replace("\\", "/").split("/")
        if seg and seg not in (".", "..")
    ]
    stem = "_".join(segments).lstrip(".")[:200]
    return stem or "journey"


def _existing_event_ids(dest: Path) -> set[str]:
    """``event_id``s already committed to ``dest``, empty if it doesn't exist yet."""
    if not dest.exists():
        return set()
    result = read_events(dest)
    return {e.event_id for e in result.events}


class BatchRejected(ValueError):
    """A posted batch parsed but wasn't a clean, well-formed odyssey stream."""


@dataclass(frozen=True)
class CollectorConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    data_dir: Path = Path(DEFAULT_DATA_DIR)
    # None means open: no Authorization header is required. Set to require one.
    api_key: Optional[str] = None
    # Explicit wins over ODYSSEY_COLLECTOR_TIMEZONE, which wins over UTC.
    timezone: Optional[str] = None
    # Injectable for tests that need a fixed date rather than the real one.
    # Passing this directly bypasses `timezone` entirely.
    date_fn: Callable[[], str] = field(default_factory=lambda: _make_date_fn(None))

    def __post_init__(self) -> None:
        if self.timezone is not None:
            object.__setattr__(self, "date_fn", _make_date_fn(self.timezone))


def resolve_config(
    *,
    host: Optional[str] = None,
    port: Optional[int] = None,
    data_dir: Optional[Path | str] = None,
    api_key: Optional[str] = None,
    timezone: Optional[str] = None,
) -> CollectorConfig:
    """Explicit arguments win over ``ODYSSEY_COLLECTOR_*`` env vars — the same
    precedence ``odyssey.config.resolve()`` uses on the recording side."""
    return CollectorConfig(
        host=host if host is not None else os.environ.get(ENV_HOST, DEFAULT_HOST),
        port=int(port if port is not None else os.environ.get(ENV_PORT, DEFAULT_PORT)),
        data_dir=Path(
            data_dir
            if data_dir is not None
            else os.environ.get(ENV_DATA_DIR, DEFAULT_DATA_DIR)
        ),
        api_key=api_key if api_key is not None else os.environ.get(ENV_API_KEY),
        timezone=timezone,
    )


class _Server(ThreadingHTTPServer):
    """Carries the resolved config and a single write lock down to the handler.

    One lock for every journey rather than a per-journey_id registry: this is
    the minimal version, and the write itself is a fast local append — not
    reason enough to build a lock registry before a workload demands it.
    """

    def __init__(self, config: CollectorConfig) -> None:
        super().__init__((config.host, config.port), _Handler)
        self.config = config
        self.write_lock = threading.Lock()
        config.data_dir.mkdir(parents=True, exist_ok=True)


class _Handler(BaseHTTPRequestHandler):
    server: _Server  # narrows the inherited Any for readability

    def log_message(self, *args: object) -> None:  # keep stdout quiet
        pass

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        if self.path == "/health":
            self._respond(200, {"status": "ok"})
            return
        self._respond(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib method name
        parts = self.path.strip("/").split("/")
        if len(parts) != 3 or parts[0] != "journeys" or parts[2] != "events":
            self._respond(404, {"error": "expected POST /journeys/<journey_id>/events"})
            return
        journey_id = unquote(parts[1])
        if not journey_id:
            self._respond(400, {"error": "journey_id must not be empty"})
            return

        if not self._authorized():
            self._respond(401, {"error": "missing or invalid Authorization"})
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        if self.headers.get("Content-Encoding", "").strip().lower() == "gzip":
            try:
                body = gzip.decompress(body)
            except OSError as exc:
                self._respond(400, {"error": f"bad gzip body: {exc}"})
                return

        try:
            count = self._store(journey_id, body)
        except (MalformedHeaderError, SchemaVersionError, BatchRejected) as exc:
            self._respond(400, {"error": str(exc)})
            return
        except OSError as exc:
            self._respond(500, {"error": f"storage failed: {exc}"})
            return

        self._respond(200, {"journey_id": journey_id, "events_received": count})

    def _authorized(self) -> bool:
        required = self.server.config.api_key
        if not required:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {required}"

    def _store(self, journey_id: str, body: bytes) -> int:
        """Parse the posted batch through the real codec, then append it.

        Round-tripping through :func:`read_events` rather than trusting the
        bytes as-is is what makes this a validating ingest point instead of a
        dumb pipe: a malformed batch is rejected with a 400 here, not written
        and only discovered broken the next time someone folds it.

        Deduplicates by ``event_id`` against what is already on disk for this
        journey/date (item 1.9): ``HttpSink``'s retry-on-failure means the same
        batch can be posted twice when a response is lost after the server
        already committed it. Without this, a retried batch would double-write
        every event in it — `fold()` would still dedupe it correctly on read,
        but the raw layer would carry redundant bytes indefinitely.
        """
        with tempfile.TemporaryDirectory() as tmp:
            received = Path(tmp) / "received.jsonl"
            received.write_bytes(body)
            result = read_events(received)

        if not result.clean:
            reason = (
                f"{result.rejected_count} rejected line(s)"
                if result.rejections
                else "truncated final line"
            )
            raise BatchRejected(f"malformed batch: {reason}")

        date_dir = self.server.config.data_dir / self.server.config.date_fn()
        dest = date_dir / f"{_safe_stem(journey_id)}.jsonl"
        with self.server.write_lock:
            date_dir.mkdir(parents=True, exist_ok=True)
            existing_ids = _existing_event_ids(dest)
            new_events = [e for e in result.events if e.event_id not in existing_ids]
            if new_events:
                write_events(dest, new_events, append=True, header=result.header)
        return len(new_events)

    def _respond(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(config: CollectorConfig) -> _Server:
    """Build and bind the server. The caller drives it with ``serve_forever()``."""
    return _Server(config)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="odyssey-collector", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument(
        "--api-key", default=None, help="require this bearer token; default: open"
    )
    parser.add_argument(
        "--timezone",
        default=None,
        help="IANA name for date-partition boundaries; default: UTC",
    )
    args = parser.parse_args(argv)

    config = resolve_config(
        host=args.host,
        port=args.port,
        data_dir=args.data_dir,
        api_key=args.api_key,
        timezone=args.timezone,
    )
    server = serve(config)
    print(
        f"odyssey-collector listening on http://{config.host}:{config.port} "
        f"-> {config.data_dir}"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
