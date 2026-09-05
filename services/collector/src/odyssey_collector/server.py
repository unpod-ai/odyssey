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

**Cross-journey batching** (item 1.7, ``HttpSink.send_batch()``)::

    POST /batch/events
    Content-Type: application/json; charset=utf-8
    Content-Encoding: gzip                   # optional, covers the whole envelope
    Authorization: Bearer <api_key>          # one key covers every journey in the batch

    {"journeys": {"<journey_id>": "<header line>\n<event line>...", ...}}

    200 {"results": {"<journey_id>": {"ok": true, "events_received": N}
                      | {"ok": false, "error": "..."}, ...}}
    400 malformed envelope — not a JSON object of {journey_id: blob} strings
    401 missing/incorrect Authorization

Always ``200`` once the envelope itself parses: each journey inside is
validated and stored independently through the same path a lone
``/journeys/<id>/events`` POST uses, so one journey's malformed blob or
storage failure reports as that journey's own ``{"ok": false, ...}`` entry
rather than failing the whole request — the same "every journey's own
outcome" guarantee draining one journey at a time already had.

No rate-limiting/backpressure lives here — ``HttpSink`` honours a 429's
``Retry-After`` if this server (or something in front of it) ever sends one,
but nothing here emits 429 itself; that is a deliberate scope cut (item 1.7),
not an oversight — this stdlib server has no queue-depth signal to base one
on.

**Product scoping.** Two mutually exclusive auth modes:

- ``api_key`` (``--api-key``/``ODYSSEY_COLLECTOR_API_KEY``) — one shared
  key, unscoped: any caller with the key writes into the flat
  ``<data_dir>/<date>/`` layout below. The simple single-tenant mode.
- ``db_uri`` (``ODYSSEY_DB_URI``) — a shared SQLite file (see
  ``packages/odyssey-store``) holding a ``products`` table, each row a
  registered tenant with a unique ``api_key_hash`` and a unique ``slug``.
  Storage becomes ``<data_dir>/<slug>/<date>/<journey_id>.jsonl``, so
  isolation is structural (one caller's key can never resolve into
  another product's directory), not just an access check layered on
  shared storage. ``name`` exists purely for operator legibility — logs,
  `GET /products` — the ``slug`` is what actually names the directory and
  every invocation. Lookups go through an in-memory ``AuthCache``
  (``odyssey_collector.auth_cache``) refreshed on a background thread, so
  ingest never pays a DB round-trip per request; a cache miss falls
  through to a direct query so a just-created product authenticates
  immediately. The roster itself is managed out-of-band (see
  ``odyssey_collector.products_db``), not by this server.

Passing both ``api_key`` and ``db_uri`` raises at construction — picking
a mode is explicit, not a silent precedence rule. In product-scoped mode,
``GET /products`` (any registered key) lists ``{slug, name}`` for the
whole roster — never keys — as a debugging/operator aid.

Retention (``prune.py``, items 1.12/2.14) is unchanged and unaware of
products: it deletes date-named directories directly under whatever
``--data-dir`` it is pointed at. In product-scoped mode that means running
it once per product directory (``--data-dir <data_dir>/<slug>``), not once
against the root.

No per-request access log by default (``_Handler.log_message`` is a
no-op) -- ``--debug``/``ODYSSEY_COLLECTOR_DEBUG`` opts into one line per
request (method, path, status) via the ``odyssey_collector.requests``
logger.
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence, Tuple
from urllib.parse import unquote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from odyssey.jsonl import (
    MalformedHeaderError,
    SchemaVersionError,
    parse_events,
    read_events,
    write_events,
)
from odyssey_store.db import connect

from odyssey_collector.auth_cache import AuthCache, Product

ENV_HOST = "ODYSSEY_COLLECTOR_HOST"
ENV_PORT = "ODYSSEY_COLLECTOR_PORT"
ENV_DATA_DIR = "ODYSSEY_COLLECTOR_DATA_DIR"
ENV_API_KEY = "ODYSSEY_COLLECTOR_API_KEY"
ENV_DB_URI = "ODYSSEY_DB_URI"
ENV_AUTH_CACHE_TTL = "ODYSSEY_COLLECTOR_AUTH_CACHE_TTL_SECONDS"
ENV_TIMEZONE = "ODYSSEY_COLLECTOR_TIMEZONE"
ENV_DEBUG = "ODYSSEY_COLLECTOR_DEBUG"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_DATA_DIR = "./collector-data"
DEFAULT_TIMEZONE = "UTC"
DEFAULT_DB_URI = "sqlite:///./odyssey.sqlite3"
DEFAULT_AUTH_CACHE_TTL_SECONDS = 60.0

# Per-request access logging, off by default (see _Handler.log_message) --
# a caller opts in with --debug/ODYSSEY_COLLECTOR_DEBUG rather than editing
# code. A dedicated logger name (not the root logger) so enabling this can
# never surface unrelated third-party log noise.
request_logger = logging.getLogger("odyssey_collector.requests")


def _truthy(value: Optional[str]) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


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
    # Mutually exclusive with db_uri — see the module docstring's
    # "Product scoping" section.
    api_key: Optional[str] = None
    # Product-scoped mode: the shared SQLite file where `products` lives
    # (see packages/odyssey-store). Mutually exclusive with api_key.
    db_uri: Optional[str] = None
    auth_cache_ttl_seconds: float = DEFAULT_AUTH_CACHE_TTL_SECONDS
    # Explicit wins over ODYSSEY_COLLECTOR_TIMEZONE, which wins over UTC.
    timezone: Optional[str] = None
    # Per-request access logging via `request_logger` -- off by default,
    # same "keep stdout quiet" behavior this always had. See
    # ENV_DEBUG/--debug.
    debug: bool = False
    # Injectable for tests that need a fixed date rather than the real one.
    # Passing this directly bypasses `timezone` entirely.
    date_fn: Callable[[], str] = field(default_factory=lambda: _make_date_fn(None))

    def __post_init__(self) -> None:
        if self.timezone is not None:
            object.__setattr__(self, "date_fn", _make_date_fn(self.timezone))
        if self.api_key is not None and self.db_uri is not None:
            raise ValueError(
                "CollectorConfig: pass either api_key (single shared key, "
                "unscoped) or db_uri (multi-tenant, product-scoped "
                "storage), not both — picking an auth mode is explicit here, "
                "not a silent precedence rule"
            )
        auth_cache = AuthCache(self.db_uri, self.auth_cache_ttl_seconds) if self.db_uri else None
        object.__setattr__(self, "_auth_cache", auth_cache)

    def product_for_key(self, api_key: str) -> Optional[Product]:
        if self._auth_cache is None or not api_key:
            return None
        return self._auth_cache.lookup(api_key)

    def list_products(self) -> List[Product]:
        if self.db_uri is None:
            return []
        conn = connect(self.db_uri)
        try:
            rows = conn.execute(
                "SELECT slug, name, api_key_hash FROM products WHERE revoked = 0 ORDER BY slug"
            ).fetchall()
        finally:
            conn.close()
        return [Product(r["slug"], r["name"], r["api_key_hash"]) for r in rows]


def resolve_config(
    *,
    host: Optional[str] = None,
    port: Optional[int] = None,
    data_dir: Optional[Path | str] = None,
    api_key: Optional[str] = None,
    db_uri: Optional[str] = None,
    auth_cache_ttl_seconds: Optional[float] = None,
    timezone: Optional[str] = None,
    debug: Optional[bool] = None,
) -> CollectorConfig:
    """Explicit arguments win over ``ODYSSEY_COLLECTOR_*`` env vars — the same
    precedence ``odyssey.config.resolve()`` uses on the recording side."""
    resolved_db_uri = db_uri if db_uri is not None else os.environ.get(ENV_DB_URI)
    return CollectorConfig(
        host=host if host is not None else os.environ.get(ENV_HOST, DEFAULT_HOST),
        port=int(port if port is not None else os.environ.get(ENV_PORT, DEFAULT_PORT)),
        data_dir=Path(
            data_dir
            if data_dir is not None
            else os.environ.get(ENV_DATA_DIR, DEFAULT_DATA_DIR)
        ),
        api_key=api_key if api_key is not None else os.environ.get(ENV_API_KEY),
        db_uri=resolved_db_uri,
        auth_cache_ttl_seconds=(
            auth_cache_ttl_seconds
            if auth_cache_ttl_seconds is not None
            else float(os.environ.get(ENV_AUTH_CACHE_TTL, DEFAULT_AUTH_CACHE_TTL_SECONDS))
        ),
        timezone=timezone,
        debug=debug if debug is not None else _truthy(os.environ.get(ENV_DEBUG)),
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

    # HTTP/1.1, not the stdlib default HTTP/1.0: without this every response
    # closes the connection, defeating HttpSink's connection reuse (item
    # 1.7's actual fix for cross-journey overhead -- see sinks.py). Safe
    # because every response already sends Content-Length, HTTP/1.1
    # keep-alive's one hard requirement.
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Quiet by default -- opt in with --debug/ODYSSEY_COLLECTOR_DEBUG
        # rather than editing code. Mirrors BaseHTTPRequestHandler's own
        # default format (client address, then the request line + status),
        # routed through `request_logger` instead of straight to stderr so
        # it can be turned on/off and captured like any other log.
        if self.server.config.debug:
            request_logger.info("%s - %s", self.address_string(), format % args)

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        if self.path == "/health":
            self._respond(200, {"status": "ok"})
            return
        if self.path == "/products":
            self._get_products()
            return
        self._respond(404, {"error": "not found"})

    def _get_products(self) -> None:
        """The registered roster, in product-scoped mode only — 404 rather
        than an empty list when the server isn't running that mode, so a
        caller can tell "no products" apart from "not a thing this server
        does". Any registered key may list the roster (names + slugs, never
        keys) — this is an operator/debugging aid, not a privacy boundary
        between products, which is what storage partitioning already is.
        """
        config = self.server.config
        if config.db_uri is None:
            self._respond(404, {"error": "not found"})
            return
        authorized, _ = self._authenticate()
        if not authorized:
            self._respond(401, {"error": "missing or invalid Authorization"})
            return
        self._respond(
            200,
            {"products": [{"slug": p.slug, "name": p.name} for p in config.list_products()]},
        )

    def do_POST(self) -> None:  # noqa: N802 - stdlib method name
        if self.path.strip("/") == "batch/events":
            self._do_batch_post()
            return
        if self.path.strip("/") == "metrics":
            self._do_metrics_post()
            return

        parts = self.path.strip("/").split("/")
        if len(parts) != 3 or parts[0] != "journeys" or parts[2] != "events":
            self._respond(
                404,
                {
                    "error": "expected POST /journeys/<journey_id>/events "
                    "or POST /batch/events"
                },
            )
            return
        journey_id = unquote(parts[1])
        if not journey_id:
            self._respond(400, {"error": "journey_id must not be empty"})
            return

        authorized, product_slug = self._authenticate()
        if not authorized:
            self._respond(401, {"error": "missing or invalid Authorization"})
            return

        body, error = self._read_body()
        if error is not None:
            self._respond(400, {"error": error})
            return

        try:
            count = self._store(journey_id, body, product_slug)
        except (MalformedHeaderError, SchemaVersionError, BatchRejected) as exc:
            self._respond(400, {"error": str(exc)})
            return
        except OSError as exc:
            self._respond(500, {"error": f"storage failed: {exc}"})
            return

        self._respond(200, {"journey_id": journey_id, "events_received": count})

    def _do_batch_post(self) -> None:
        """``POST /batch/events`` (item 1.7) — several journeys in one
        request. Every journey is attempted independently through the exact
        same :meth:`_store` a lone ``/journeys/<id>/events`` POST uses, so
        one journey's malformed batch or storage failure never blocks the
        others in the same request — the envelope is a transport
        optimisation, not a new all-or-nothing write.
        """
        authorized, product_slug = self._authenticate()
        if not authorized:
            self._respond(401, {"error": "missing or invalid Authorization"})
            return

        body, error = self._read_body()
        if error is not None:
            self._respond(400, {"error": error})
            return

        try:
            envelope = json.loads(body)
            journeys = envelope["journeys"]
            if not isinstance(journeys, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in journeys.items()
            ):
                raise TypeError("'journeys' must be an object of {journey_id: blob}")
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            self._respond(400, {"error": f"malformed batch envelope: {exc}"})
            return

        results: dict[str, Any] = {}
        for journey_id, blob in journeys.items():
            if not journey_id:
                results[journey_id] = {
                    "ok": False,
                    "error": "journey_id must not be empty",
                }
                continue
            try:
                count = self._store(journey_id, blob.encode("utf-8"), product_slug)
            except (MalformedHeaderError, SchemaVersionError, BatchRejected) as exc:
                results[journey_id] = {"ok": False, "error": str(exc)}
                continue
            except OSError as exc:
                results[journey_id] = {"ok": False, "error": f"storage failed: {exc}"}
                continue
            results[journey_id] = {"ok": True, "events_received": count}

        self._respond(200, {"results": results})

    def _do_metrics_post(self) -> None:
        """``POST /metrics`` — an opt-in, off-by-default host telemetry
        snapshot from a capturing process (see packages/odyssey-core's
        odyssey/metrics.py). Independent of journey capture: its own
        auth check (same rules), its own storage subdirectory, never
        mixed into a journey shard file. ``public_ip`` is added here,
        server-side, from the real TCP peer address -- the SDK never
        reports its own public IP (see the design spec's "Public IP
        source" decision).
        """
        authorized, product_slug = self._authenticate()
        if not authorized:
            self._respond(401, {"error": "missing or invalid Authorization"})
            return

        body, error = self._read_body()
        if error is not None:
            self._respond(400, {"error": error})
            return

        try:
            snapshot = json.loads(body)
        except json.JSONDecodeError as exc:
            self._respond(400, {"error": f"malformed metrics body: {exc}"})
            return
        if not isinstance(snapshot, dict):
            self._respond(400, {"error": "metrics body must be a JSON object"})
            return

        snapshot["public_ip"] = self.client_address[0]

        base = self.server.config.data_dir
        metrics_dir = (
            (base / _safe_stem(product_slug) / "metrics")
            if product_slug is not None
            else (base / "metrics")
        )
        with self.server.write_lock:
            try:
                metrics_dir.mkdir(parents=True, exist_ok=True)
                dest = metrics_dir / f"{self.server.config.date_fn()}.jsonl"
                with dest.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(snapshot) + "\n")
            except OSError as exc:
                self._respond(500, {"error": f"storage failed: {exc}"})
                return

        self._respond(200, {"ok": True})

    def _read_body(self) -> Tuple[bytes, Optional[str]]:
        """The request body, gzip-decompressed if ``Content-Encoding`` says
        so. Returns ``(body, None)`` or ``(b"", error)``."""
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        if self.headers.get("Content-Encoding", "").strip().lower() == "gzip":
            try:
                body = gzip.decompress(body)
            except OSError as exc:
                return b"", f"bad gzip body: {exc}"
        return body, None

    def _authenticate(self) -> Tuple[bool, Optional[str]]:
        """Returns ``(authorized, product_slug)``. ``product_slug`` is
        ``None`` except in product-scoped mode, where it names the directory
        the caller's key resolved to.
        """
        config = self.server.config
        presented = self.headers.get("Authorization", "")
        if config.db_uri is not None:
            token = presented[len("Bearer "):] if presented.startswith("Bearer ") else ""
            product = config.product_for_key(token)
            return (product is not None, product.slug if product else None)
        if not config.api_key:
            return (True, None)
        return (presented == f"Bearer {config.api_key}", None)

    def _store(self, journey_id: str, body: bytes, product_slug: Optional[str]) -> int:
        """Parse the posted batch through the real codec, then append it.

        Round-tripping through :func:`parse_events` rather than trusting the
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
        base = self.server.config.data_dir
        # Validated in memory, with no scratch file anywhere. This used to
        # write the posted bytes to a temp file and read them back, which made
        # every ingest depend on a writable temp directory. A hardened
        # deployment does not have one: under ``ProtectSystem=strict`` with
        # ``ReadWritePaths`` naming only the data directory, ``/tmp``,
        # ``/var/tmp``, ``/usr/tmp`` and the working directory are all
        # read-only, so ``tempfile`` found no candidate and returned a 500 for
        # every journey POST -- while ``POST /metrics``, which writes straight
        # into ``data_dir``, kept working and made the failure look selective.
        # The batch is already in memory; putting it on disk to read it back
        # bought nothing but that dependency and a write per request.
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BatchRejected(f"malformed batch: not valid UTF-8: {exc}") from exc
        result = parse_events(text, source=f"journey {journey_id}")

        if not result.clean:
            reason = (
                f"{result.rejected_count} rejected line(s)"
                if result.rejections
                else "truncated final line"
            )
            raise BatchRejected(f"malformed batch: {reason}")

        date_dir = (
            (base / _safe_stem(product_slug) / self.server.config.date_fn())
            if product_slug is not None
            else (base / self.server.config.date_fn())
        )
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
        "--api-key",
        default=None,
        help="require this single shared bearer token, unscoped; default: open. "
        "Mutually exclusive with product-scoped mode (ODYSSEY_DB_URI)",
    )
    parser.add_argument(
        "--timezone",
        default=None,
        help="IANA name for date-partition boundaries; default: UTC",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=None,
        help="log every request (method, path, status) to stdout via the "
        "'odyssey_collector.requests' logger; default: off (ODYSSEY_COLLECTOR_DEBUG)",
    )
    parser.add_argument(
        "--db-uri",
        default=None,
        help="shared SQLite database (see packages/odyssey-store) for product-scoped "
        "auth and management flags below, as a sqlite:/// URI -- e.g. "
        "sqlite:///./odyssey.sqlite3 (relative, 3 slashes) or "
        "sqlite:////var/lib/odyssey/odyssey.db (absolute, 4 slashes); "
        "default: $ODYSSEY_DB_URI. Mutually exclusive with --api-key",
    )
    parser.add_argument(
        "--auth-cache-ttl-seconds",
        type=float,
        default=None,
        help="how long an auth-check result is cached in memory before "
        "re-reading --db-uri; default: 60 ($ODYSSEY_COLLECTOR_AUTH_CACHE_TTL_SECONDS)",
    )
    parser.add_argument(
        "--product-slug",
        default="default",
        help="slug for --create-product; default: 'default'",
    )
    parser.add_argument(
        "--product-name",
        default="Default",
        help="name for --create-product; default: 'Default'",
    )
    parser.add_argument(
        "--create-product",
        action="store_true",
        help="create a new product (--product-slug/--product-name) in --db-uri, "
        "print its api_key once, and exit -- does not start the server",
    )
    parser.add_argument(
        "--list-products",
        action="store_true",
        help="list every product in --db-uri (slug/name/revoked/created_at, "
        "never a key) and exit -- does not start the server",
    )
    parser.add_argument(
        "--revoke-product",
        default=None,
        metavar="SLUG",
        help="revoke a product's key in --db-uri and exit -- does not start the server",
    )
    parser.add_argument(
        "--rotate-product",
        default=None,
        metavar="SLUG",
        help="revoke a product's current key and issue a new one in --db-uri, "
        "print it once, and exit -- does not start the server",
    )
    parser.add_argument(
        "--migrate-products-from-json",
        default=None,
        metavar="PATH",
        help="one-time cutover: read an old --products-file-style JSON roster "
        "at PATH, hash each existing api_key as-is, and insert into --db-uri; "
        "exit -- does not start the server",
    )
    args = parser.parse_args(argv)

    admin_actions = [
        args.create_product,
        args.list_products,
        args.revoke_product is not None,
        args.rotate_product is not None,
        args.migrate_products_from_json is not None,
    ]
    if any(admin_actions):
        if not args.db_uri and not os.environ.get(ENV_DB_URI):
            print("--db-uri (or $ODYSSEY_DB_URI) is required for product management flags", file=sys.stderr)
            return 1
        db_uri = args.db_uri or os.environ[ENV_DB_URI]

        from odyssey_collector.products_db import (
            create_product,
            list_products,
            migrate_products_from_json,
            revoke_product,
            rotate_product,
        )

        if args.create_product:
            created = create_product(db_uri, args.product_slug, args.product_name)
            print(f"product: slug={created.slug!r} name={created.name!r}")
            print(f"api_key={created.api_key}")
            print("save this key now -- it will not be printed again", file=sys.stderr)
            return 0

        if args.list_products:
            for p in list_products(db_uri):
                print(f"slug={p['slug']!r} name={p['name']!r} revoked={p['revoked']} created_at={p['created_at']}")
            return 0

        if args.revoke_product is not None:
            try:
                revoke_product(db_uri, args.revoke_product)
            except KeyError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(f"revoked {args.revoke_product!r}")
            return 0

        if args.rotate_product is not None:
            try:
                rotated = rotate_product(db_uri, args.rotate_product)
            except KeyError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(f"product: slug={rotated.slug!r} name={rotated.name!r}")
            print(f"api_key={rotated.api_key}")
            print("save this key now -- it will not be printed again", file=sys.stderr)
            return 0

        if args.migrate_products_from_json is not None:
            count = migrate_products_from_json(db_uri, Path(args.migrate_products_from_json))
            print(f"migrated {count} product(s) into {db_uri}")
            return 0

    try:
        config = resolve_config(
            host=args.host,
            port=args.port,
            data_dir=args.data_dir,
            api_key=args.api_key,
            db_uri=args.db_uri,
            auth_cache_ttl_seconds=args.auth_cache_ttl_seconds,
            timezone=args.timezone,
            debug=args.debug,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if config.debug:
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(name)s %(message)s"
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
