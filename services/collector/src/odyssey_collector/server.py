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
- ``products`` (``--products-file``/``ODYSSEY_COLLECTOR_PRODUCTS_FILE``, a
  JSON file shaped ``{"products": [{"slug": ..., "name": ..., "api_key":
  ...}, ...]}``) — a small registered-tenant roster, each with a unique
  ``api_key`` and a unique ``slug``. Storage becomes
  ``<data_dir>/<slug>/<date>/<journey_id>.jsonl``, so isolation is
  structural (one caller's key can never resolve into another product's
  directory), not just an access check layered on shared storage. ``name``
  exists purely for operator legibility — logs, `GET /products`, reading
  the products file — the ``slug`` is what actually names the directory
  and every invocation. This is a stopgap, not real multi-tenant
  infrastructure: the roster is a flat file loaded once at startup (edit
  it and restart the process to add/revoke a product), not a database.

Passing both ``api_key`` and ``products`` raises at construction — picking
a mode is explicit, not a silent precedence rule. In product-scoped mode,
``GET /products`` (any registered key) lists ``{slug, name}`` for the
whole roster — never keys — as a debugging/operator aid.

Retention (``prune.py``, items 1.12/2.14) is unchanged and unaware of
products: it deletes date-named directories directly under whatever
``--data-dir`` it is pointed at. In product-scoped mode that means running
it once per product directory (``--data-dir <data_dir>/<slug>``), not once
against the root.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import secrets
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, Tuple
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
ENV_PRODUCTS_FILE = "ODYSSEY_COLLECTOR_PRODUCTS_FILE"
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


@dataclass(frozen=True)
class Product:
    """One registered tenant: a human-readable ``name`` plus the ``slug``
    that actually names its storage partition and its ``api_key`` -- the
    top-level, unique-key-per-tenant auth boundary. (Not to be confused
    with the unrelated ``project`` tag packages/odyssey-core's capture
    side can attach to a journey -- see odyssey/project.py -- which is
    purely descriptive and never an auth concept.)

    ``slug`` rather than an opaque id, because it is what shows up in
    ``<data_dir>/<slug>/...`` and in every log line and CLI invocation —
    something an operator reading the filesystem or a `--data-dir` flag can
    recognise, not a UUID they have to cross-reference elsewhere.
    """

    slug: str
    name: str
    api_key: str


def _load_products_file(path: Path | str) -> Tuple[Product, ...]:
    """Parse a JSON ``{"products": [{"slug", "name", "api_key"}, ...]}`` file.

    Fails loudly and immediately — at startup, not on the first mismatched
    request — if the file is missing or malformed, or if two products share a
    slug or a key. A silently-empty or ambiguous roster would look identical
    to "no keys configured" or "which product does this key belong to?" and
    quietly misroute or reopen the server.
    """
    raw = json.loads(Path(path).read_text())
    entries = raw.get("products") if isinstance(raw, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError(
            f"{path}: products file must be a JSON object shaped "
            '{"products": [{"slug": ..., "name": ..., "api_key": ...}, ...]}'
        )

    products = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict) or not all(
            isinstance(entry.get(k), str) and entry.get(k)
            for k in ("slug", "name", "api_key")
        ):
            raise ValueError(
                f"{path}: products[{i}] must have non-empty string "
                "'slug', 'name', and 'api_key' fields"
            )
        products.append(
            Product(slug=entry["slug"], name=entry["name"], api_key=entry["api_key"])
        )

    slugs = [p.slug for p in products]
    if len(set(slugs)) != len(slugs):
        raise ValueError(f"{path}: duplicate product slug in {sorted(slugs)}")
    keys = [p.api_key for p in products]
    if len(set(keys)) != len(keys):
        raise ValueError(f"{path}: the same api_key is registered to two products")

    return tuple(products)


def _init_products_file(path: Path | str, slug: str, name: str) -> Product:
    """Bootstrap a starter ``--products-file`` roster (``odyssey-collector
    --init-products-file``, never a side effect of a normal ``serve``
    startup).

    ``_load_products_file`` deliberately refuses to start the server on a
    missing/empty/malformed products file (see its own docstring) — an
    auto-created *empty* roster would be indistinguishable from "no keys
    configured" and every future request would just 401 with no
    explanation. This is the safe version of "create it automatically": a
    human runs it once, on purpose, and it writes exactly one product with
    a cryptographically random ``api_key`` — a real secret, not a
    fabricated placeholder — printed once so the operator can save it
    before it scrolls off. Refuses to overwrite an existing file, the same
    way a real secret-issuing tool would.
    """
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"{path} already exists — not overwriting a real roster")
    product = Product(slug=slug, name=name, api_key=secrets.token_urlsafe(32))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"products": [{"slug": slug, "name": name, "api_key": product.api_key}]},
            indent=2,
        )
        + "\n"
    )
    return product


class BatchRejected(ValueError):
    """A posted batch parsed but wasn't a clean, well-formed odyssey stream."""


@dataclass(frozen=True)
class CollectorConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    data_dir: Path = Path(DEFAULT_DATA_DIR)
    # None means open: no Authorization header is required. Set to require one.
    # Mutually exclusive with products — see the module docstring's
    # "Product scoping" section.
    api_key: Optional[str] = None
    # The registered tenant roster. When set, storage is partitioned per
    # product (<data_dir>/<slug>/<date>/...) and only a key belonging to a
    # registered product is accepted. Mutually exclusive with api_key.
    products: Optional[Tuple[Product, ...]] = None
    # Explicit wins over ODYSSEY_COLLECTOR_TIMEZONE, which wins over UTC.
    timezone: Optional[str] = None
    # Injectable for tests that need a fixed date rather than the real one.
    # Passing this directly bypasses `timezone` entirely.
    date_fn: Callable[[], str] = field(default_factory=lambda: _make_date_fn(None))

    def __post_init__(self) -> None:
        if self.timezone is not None:
            object.__setattr__(self, "date_fn", _make_date_fn(self.timezone))
        if self.api_key is not None and self.products is not None:
            raise ValueError(
                "CollectorConfig: pass either api_key (single shared key, "
                "unscoped) or products (multi-tenant, product-scoped "
                "storage), not both — picking an auth mode is explicit here, "
                "not a silent precedence rule"
            )

    def product_for_key(self, api_key: str) -> Optional[Product]:
        if not self.products or not api_key:
            return None
        for product in self.products:
            if product.api_key == api_key:
                return product
        return None


def resolve_config(
    *,
    host: Optional[str] = None,
    port: Optional[int] = None,
    data_dir: Optional[Path | str] = None,
    api_key: Optional[str] = None,
    products_file: Optional[Path | str] = None,
    timezone: Optional[str] = None,
) -> CollectorConfig:
    """Explicit arguments win over ``ODYSSEY_COLLECTOR_*`` env vars — the same
    precedence ``odyssey.config.resolve()`` uses on the recording side."""
    resolved_products_file = (
        products_file
        if products_file is not None
        else os.environ.get(ENV_PRODUCTS_FILE)
    )
    return CollectorConfig(
        host=host if host is not None else os.environ.get(ENV_HOST, DEFAULT_HOST),
        port=int(port if port is not None else os.environ.get(ENV_PORT, DEFAULT_PORT)),
        data_dir=Path(
            data_dir
            if data_dir is not None
            else os.environ.get(ENV_DATA_DIR, DEFAULT_DATA_DIR)
        ),
        api_key=api_key if api_key is not None else os.environ.get(ENV_API_KEY),
        products=(
            _load_products_file(resolved_products_file)
            if resolved_products_file
            else None
        ),
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

    # HTTP/1.1, not the stdlib default HTTP/1.0: without this every response
    # closes the connection, defeating HttpSink's connection reuse (item
    # 1.7's actual fix for cross-journey overhead -- see sinks.py). Safe
    # because every response already sends Content-Length, HTTP/1.1
    # keep-alive's one hard requirement.
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:  # keep stdout quiet
        pass

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
        if config.products is None:
            self._respond(404, {"error": "not found"})
            return
        authorized, _ = self._authenticate()
        if not authorized:
            self._respond(401, {"error": "missing or invalid Authorization"})
            return
        self._respond(
            200,
            {"products": [{"slug": p.slug, "name": p.name} for p in config.products]},
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
            metrics_dir.mkdir(parents=True, exist_ok=True)
            dest = metrics_dir / f"{self.server.config.date_fn()}.jsonl"
            with dest.open("a", encoding="utf-8") as f:
                f.write(json.dumps(snapshot) + "\n")

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
        if config.products is not None:
            token = (
                presented[len("Bearer ") :] if presented.startswith("Bearer ") else ""
            )
            product = config.product_for_key(token)
            return (product is not None, product.slug if product else None)
        if not config.api_key:
            return (True, None)
        return (presented == f"Bearer {config.api_key}", None)

    def _store(self, journey_id: str, body: bytes, product_slug: Optional[str]) -> int:
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

        base = self.server.config.data_dir
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
        "Mutually exclusive with --products-file",
    )
    parser.add_argument(
        "--products-file",
        default=None,
        help='JSON {"products": [{"slug", "name", "api_key"}, ...]} file '
        "-- each product writes into its own <data_dir>/<slug>/ "
        "partition. Mutually exclusive with --api-key",
    )
    parser.add_argument(
        "--timezone",
        default=None,
        help="IANA name for date-partition boundaries; default: UTC",
    )
    parser.add_argument(
        "--init-products-file",
        default=None,
        metavar="PATH",
        help="bootstrap a starter --products-file roster at PATH (one "
        "product, a fresh random api_key) and exit -- does not start the "
        "server. Refuses to overwrite an existing file. See "
        "--product-slug/--product-name",
    )
    parser.add_argument(
        "--product-slug",
        default="default",
        help="with --init-products-file (default: default)",
    )
    parser.add_argument(
        "--product-name",
        default="Default",
        help="with --init-products-file (default: Default)",
    )
    args = parser.parse_args(argv)

    if args.init_products_file is not None:
        try:
            product = _init_products_file(
                args.init_products_file, args.product_slug, args.product_name
            )
        except FileExistsError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"wrote {args.init_products_file}")
        print(f"product: slug={product.slug!r} name={product.name!r}")
        print(f"api_key={product.api_key}")
        print(
            f"save this key now -- it will not be printed again "
            f"(it's also in {args.init_products_file} in plaintext)",
            file=sys.stderr,
        )
        return 0

    config = resolve_config(
        host=args.host,
        port=args.port,
        data_dir=args.data_dir,
        api_key=args.api_key,
        products_file=args.products_file,
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
