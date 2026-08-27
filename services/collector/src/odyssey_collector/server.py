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

Storage today is a local directory of ``<journey_id>.jsonl`` — the exact
shape ``FileSink`` writes. "spool -> object store" (``docs/STRUCTURE.md``'s
stated destination for this service) is a deliberately deferred upgrade: swap
:meth:`_Handler._store`, keep the endpoint contract.

Wire contract (matches ``odyssey.sinks.HttpSink`` exactly)::

    POST /journeys/<url-encoded journey_id>/events
    Content-Type: application/x-ndjson; charset=utf-8
    Authorization: Bearer <api_key>          # only when the server requires one
    <header line><event line>...             # exactly what a shard on disk holds

    200 {"journey_id": ..., "events_received": N}
    400 malformed batch — not valid odyssey JSONL
    401 missing/incorrect Authorization, when the server requires one
    500 storage failure
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional, Sequence
from urllib.parse import unquote

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

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_DATA_DIR = "./collector-data"


class BatchRejected(ValueError):
    """A posted batch parsed but wasn't a clean, well-formed odyssey stream."""


@dataclass(frozen=True)
class CollectorConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    data_dir: Path = Path(DEFAULT_DATA_DIR)
    # None means open: no Authorization header is required. Set to require one.
    api_key: Optional[str] = None


def resolve_config(
    *,
    host: Optional[str] = None,
    port: Optional[int] = None,
    data_dir: Optional[Path | str] = None,
    api_key: Optional[str] = None,
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

        dest = self.server.config.data_dir / f"{journey_id}.jsonl"
        with self.server.write_lock:
            write_events(dest, result.events, append=True, header=result.header)
        return len(result.events)

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
    args = parser.parse_args(argv)

    config = resolve_config(
        host=args.host, port=args.port, data_dir=args.data_dir, api_key=args.api_key
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
