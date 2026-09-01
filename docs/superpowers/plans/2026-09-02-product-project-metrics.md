# Product/Project rename + opt-in server metrics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename `services/collector`'s multi-tenant auth concept from `Project` to `Product`; add an unrelated, purely descriptive `project` auto-detection tag on captured journeys; add an opt-in, off-by-default server-metadata/metrics capture channel.

**Architecture:** Two independent SDK modules (`odyssey/project.py`, `odyssey/metrics.py`) wired into `odyssey.init()`/`Client`; `services/collector`'s `Project`→`Product` rename ripples through its CLI flags, env vars, wire shapes, and every test/doc that names it; a new `HttpTransport` base class extracted from `HttpSink` is shared by both `HttpSink` and the new metrics poster.

**Tech Stack:** Python 3.12, stdlib only (`configparser`, `platform`, `shutil`, `http.client`) — no new dependency anywhere in this plan.

**Spec:** `docs/superpowers/specs/2026-09-02-product-project-metrics-design.md`

## Global Constraints

- No new dependency in `packages/odyssey-core` (`dependencies = []` stays true) or `services/collector`.
- Every new/renamed env var is env-first, explicit-argument-wins — the precedence `odyssey.config.resolve()` and `services/collector.resolve_config()` already use everywhere.
- ADR 0004 "never crash the host": every new failure path is counted (`Client.note_error`), never raised, except `ODYSSEY_DEBUG=1`.
- Clean-break rename, no backward-compat alias for `--keys-file`/`{"projects":[...]}` (approved in the spec's Decisions table).
- `public_ip` in a metrics snapshot is recorded server-side from the real TCP peer address — the SDK never calls a third-party IP-lookup service.
- `journey_metadata["project"]` is additive; `SCHEMA_VERSION` (currently `"2.0"`) does **not** bump.

---

### Task 1: Extract `HttpTransport` base class from `HttpSink`

Pure refactor, zero behavior change — the existing suite is the safety net; no new tests are written in this task.

**Files:**
- Modify: `packages/odyssey-core/src/odyssey/sinks.py:67-349` (the `ENV_ENDPOINT`/`ENV_API_KEY` constants through the end of `HttpSink`)
- Test: `packages/odyssey-core/tests/test_sinks.py` (existing, unmodified — must stay 100% green before and after)

**Interfaces:**
- Produces: `HttpTransport` class with `__init__(self, endpoint=None, *, api_key=None, timeout=DEFAULT_TIMEOUT, compress=True)`, `self.endpoint: str`, `self.api_key: Optional[str]`, `self.timeout: float`, `self.compress: bool`, `self._lock: threading.Lock`, methods `_connect() -> http.client.HTTPConnection`, `close() -> None`, `_request(path, payload, headers) -> Tuple[Optional[int], Optional[str], Optional[bytes], Optional[str]]`, `_check_backoff(subject: str) -> None` (raises `HttpSinkError` if inside a prior 429's Retry-After window), `_note_retry_after(retry_after_header: Optional[str]) -> None`.
- `HttpSink(HttpTransport)` keeps its existing public shape (`send`, `send_batch`) unchanged for every caller.

- [ ] **Step 1: Confirm the baseline is green**

Run: `cd packages/odyssey-core && uv run pytest tests/test_sinks.py -v`
Expected: all tests PASS (this is the refactor's regression net — note the count).

- [ ] **Step 2: Extract `HttpTransport`**

In `packages/odyssey-core/src/odyssey/sinks.py`, replace the `class HttpSink:` block's `__init__`, `_connect`, `close`, and `_request` (and the retry-after instance state) with a new base class above it, and slim `HttpSink` down to a subclass:

```python
class HttpTransport:
    """Shared stdlib HTTP transport for anything that POSTs to a collector:
    endpoint/api_key resolution, connection reuse (HTTP/1.1 keep-alive),
    gzip, and Retry-After backoff. What gets posted, and to which path, is
    entirely up to the subclass -- HttpSink posts journeys, the metrics
    poster (odyssey/metrics.py) posts host snapshots to a different path.
    Not a Sink itself (no send()) -- Sink is a structural Protocol
    (odyssey.spool.Sink), and this class has no opinion on journey shape.
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
                f"{type(self).__name__} needs an endpoint: pass endpoint=... "
                f"or set {ENV_ENDPOINT}"
            )
        self.endpoint = resolved.rstrip("/")
        self.api_key = api_key if api_key is not None else os.environ.get(ENV_API_KEY)
        self.timeout = timeout
        self.compress = compress
        # Set by a 429's Retry-After; checked before the next request so a
        # server that asked to be left alone is not immediately re-hit.
        self._retry_after_until: float = 0.0

        parsed = urlsplit(self.endpoint)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(
                f"{type(self).__name__} endpoint must be an http(s) URL: "
                f"{self.endpoint!r}"
            )
        self._scheme = parsed.scheme
        self._host = parsed.hostname
        self._port = parsed.port
        self._base_path = parsed.path.rstrip("/")
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
        """Release the reused connection. Idempotent."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _check_backoff(self, subject: str) -> None:
        """Raise if still inside a prior 429's Retry-After window."""
        if time.monotonic() < self._retry_after_until:
            wait = self._retry_after_until - time.monotonic()
            raise HttpSinkError(
                f"{subject}: backing off {wait:.0f}s more per the server's "
                f"last Retry-After"
            )

    def _note_retry_after(self, retry_after_header: Optional[str]) -> None:
        self._retry_after_until = time.monotonic() + _parse_retry_after(
            retry_after_header
        )

    def _request(
        self, path: str, payload: bytes, headers: Dict[str, str]
    ) -> Tuple[Optional[int], Optional[str], Optional[bytes], Optional[str]]:
        """One POST, reusing ``self._conn`` when a prior request left it open.

        A kept-alive connection the server (or an idle intermediary) closed
        in the meantime raises on the *first* use after that, not at close
        time -- so a dropped connection is retried once, transparently, with
        a fresh one, rather than surfacing as a spurious failure.
        """
        for attempt in range(2):
            if self._conn is None:
                self._conn = self._connect()
            try:
                self._conn.request("POST", path, body=payload, headers=headers)
                response = self._conn.getresponse()
                body = response.read()  # must drain the body to reuse the connection
                return response.status, response.getheader("Retry-After"), body, None
            except (http.client.HTTPException, OSError) as exc:
                if self._conn is not None:
                    self._conn.close()
                self._conn = None
                if attempt == 1:
                    return None, None, None, str(exc)
        return None, None, None, "unreachable"  # pragma: no cover - loop always returns
```

Then `HttpSink` becomes:

```python
class HttpSink(HttpTransport):
    """(existing docstring, unchanged)"""

    def send(
        self,
        journey_id: str,
        events: List[JourneyEvent],
        header: Optional[JourneyHeader] = None,
    ) -> None:
        self._check_backoff(journey_id)

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
            status, retry_after, _body, error = self._request(path, payload, headers)

        if error is not None:
            raise HttpSinkError(
                f"{journey_id}: could not reach {self.endpoint}: {error}"
            )
        if status == 429:
            self._note_retry_after(retry_after)
        if status is None or status >= 300:
            raise HttpSinkError(f"{journey_id}: HTTP {status} from {self.endpoint}")

    def send_batch(self, items: List[BatchItem]) -> Dict[str, Optional[str]]:
        """(existing docstring, unchanged)"""
        self._check_backoff(f"batch of {len(items)}")

        blobs = {}
        for journey_id, events, header in items:
            body = header_line(header=header) + "\n"
            body += "".join(encode_event(e) + "\n" for e in events)
            blobs[journey_id] = body
        payload = json.dumps({"journeys": blobs}).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self.compress:
            payload = gzip.compress(payload)
            headers["Content-Encoding"] = "gzip"
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        path = f"{self._base_path}/batch/events"
        with self._lock:
            status, retry_after, body, error = self._request(path, payload, headers)

        if error is not None:
            raise HttpSinkError(
                f"batch of {len(items)}: could not reach {self.endpoint}: {error}"
            )
        if status == 429:
            self._note_retry_after(retry_after)
            raise HttpSinkError(
                f"batch of {len(items)}: HTTP 429 from {self.endpoint}"
            )
        if status != 200:
            raise HttpSinkError(
                f"batch of {len(items)}: HTTP {status} from {self.endpoint}"
            )
        try:
            parsed = json.loads(body or b"{}")
            results = parsed["results"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise HttpSinkError(
                f"batch of {len(items)}: malformed response from {self.endpoint}: {exc}"
            ) from exc
        return {
            jid: None if r.get("ok") else str(r.get("error", "unknown error"))
            for jid, r in results.items()
        }
```

Check the real `send_batch` body in the file before pasting (lines ~277-300 hold the JSON-parsing/return tail this snippet reconstructs) — copy it verbatim rather than retyping if it differs from the reconstruction above; the only actual change in `send_batch` is replacing the inline `if time.monotonic() < self._retry_after_until: ...` block with `self._check_backoff(...)` and the inline `self._retry_after_until = time.monotonic() + _parse_retry_after(retry_after)` with `self._note_retry_after(retry_after)`.

- [ ] **Step 3: Confirm the suite is still green, byte-for-byte same count**

Run: `cd packages/odyssey-core && uv run pytest tests/test_sinks.py -v`
Expected: same PASS count as Step 1. Any failure means the extraction changed behavior — fix before proceeding, do not adjust the tests.

- [ ] **Step 4: Lint**

Run: `cd packages/odyssey-core && uv run --extra dev black --check . && uv run --extra dev isort --check-only . && uv run --extra dev flake8 --max-line-length=88 --extend-ignore=E203,E501,W503,F541,F841 src tests`
Expected: clean. If black/isort want changes, run them without `--check` and re-verify tests are still green.

- [ ] **Step 5: Commit**

```bash
git add packages/odyssey-core/src/odyssey/sinks.py
git commit -m "refactor(core): extract HttpTransport base class from HttpSink

Connection reuse, gzip, and Retry-After backoff move to a shared base
class so the new metrics poster (odyssey/metrics.py, a later task) can
reuse the same transport instead of hand-rolling its own http.client
code. Zero behavior change -- HttpSink's send()/send_batch() are
unchanged from the outside; existing tests/test_sinks.py stays green
with the same pass count before and after."
```

---

### Task 2: `services/collector` — rename `Project` to `Product`

**Files:**
- Modify: `services/collector/src/odyssey_collector/server.py` (module docstring lines 70-101; `Project` dataclass lines 194-207; `_load_keys_file`/`_init_keys_file` lines 210-278; `CollectorConfig` lines 285-321; `resolve_config` lines 324-349; `_Handler.do_GET`/`_get_projects` lines 380-408; `_Handler._authenticate` lines 512-527; `main()` argparse block lines 586-656)
- Modify: `services/collector/tests/test_server.py` (every `Project`/`projects`/`keys_file`/`_init_keys_file`/`/projects` reference)

**Interfaces:**
- Produces: `Product` dataclass (`slug: str`, `name: str`, `api_key: str`) replacing `Project`; `CollectorConfig.products: Optional[Tuple[Product, ...]]` replacing `.projects`; `CollectorConfig.product_for_key(api_key: str) -> Optional[Product]` replacing `.project_for_key`; `_load_products_file(path) -> Tuple[Product, ...]` replacing `_load_keys_file`; `_init_products_file(path, slug, name) -> Product` replacing `_init_keys_file`; `resolve_config(..., products_file: Optional[Path | str] = None, ...)` replacing `keys_file=`; `GET /products` replacing `GET /projects`; CLI flags `--products-file`, `--init-products-file`, `--product-slug` (default `"default"`), `--product-name` (default `"Default"`); env vars `ODYSSEY_COLLECTOR_PRODUCTS_FILE` replacing `ODYSSEY_COLLECTOR_KEYS_FILE`.

Every rename below is mechanical (same fields, same logic, new name) — verified by the existing test suite renamed 1:1 and re-passing, not by new coverage.

- [ ] **Step 1: Rename in `server.py` — module docstring**

Replace the "Project scoping (item 1.6)" section (lines 70-101) with:

```
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
```

- [ ] **Step 2: Rename `Project` → `Product`, `_load_keys_file` → `_load_products_file`**

Replace lines 194-248:

```python
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
```

- [ ] **Step 3: Rename `CollectorConfig.projects` → `.products`, `project_for_key` → `product_for_key`**

Replace lines 285-321:

```python
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
```

- [ ] **Step 4: Rename `resolve_config`'s `keys_file` param and env var**

Replace lines 129-134 (the `ENV_*` constants) — rename `ENV_KEYS_FILE = "ODYSSEY_COLLECTOR_KEYS_FILE"` to `ENV_PRODUCTS_FILE = "ODYSSEY_COLLECTOR_PRODUCTS_FILE"` — and replace lines 324-349:

```python
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
        products_file if products_file is not None else os.environ.get(ENV_PRODUCTS_FILE)
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
```

- [ ] **Step 5: Rename the `GET /projects` handler and `_authenticate`**

Replace lines 380-408 (`do_GET`/`_get_projects`):

```python
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
```

Replace lines 512-527 (`_authenticate`), which returns a `product_slug` now, and every `project_slug` variable name feeding it in `do_POST`/`_do_batch_post`/`_store` (lines 410-570) — rename the local variable `project_slug` to `product_slug` throughout those three methods too, mechanical rename only:

```python
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
```

In `_store` (line 529), rename the parameter `project_slug: Optional[str]` to `product_slug: Optional[str]` and its one use on line 559.

- [ ] **Step 6: Rename the CLI flags in `main()`**

Replace lines 599-628 (the `--keys-file`/`--init-keys-file`/`--project-slug`/`--project-name` arguments):

```python
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
```

(Note: the `--timezone` argument was already there between `--keys-file` and `--init-keys-file` in the original — keep it in place, only the surrounding two are renamed.)

Then replace lines 629-656 (the body that used `args.init_keys_file`/`args.keys_file`):

```python
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
```

- [ ] **Step 7: Rename every reference in `tests/test_server.py`**

Mechanical find/replace across the whole file (verified against the file read in this session — every occurrence is one of these):
- `from odyssey_collector.server import (CollectorConfig, Project, _init_keys_file, _safe_stem, resolve_config, serve)` → `from odyssey_collector.server import (CollectorConfig, Product, _init_products_file, _safe_stem, resolve_config, serve)`
- `Project(` → `Product(` (both `ACME`/`GLOBEX` fixtures and the `evil` fixture in `test_a_slug_cannot_traverse_out_of_data_dir`)
- `_init_keys_file` → `_init_products_file` (3 call sites in `test_init_keys_file_writes_a_loadable_roster`, `test_init_keys_file_refuses_to_overwrite_an_existing_file`, `test_init_keys_file_generates_a_different_key_each_time`)
- `projects=(...)` kwarg on `CollectorConfig(...)` → `products=(...)` (fixtures `scoped`, `test_a_slug_cannot_traverse_out_of_data_dir`)
- `keys_file=` kwarg on `resolve_config(...)` → `products_file=` (every `test_a_*_keys_file_*`/`test_a_valid_keys_file_*` test)
- `config.projects` → `config.products` (assertions)
- `/projects` URL path → `/products` (`test_get_projects_*` tests and their `_get_json` calls)
- Rename the test functions themselves (name only, body follows the substitutions above): `test_a_malformed_keys_file_fails_fast_at_startup` → `test_a_malformed_products_file_fails_fast_at_startup`; `test_a_keys_file_missing_the_projects_key_is_rejected` → `test_a_products_file_missing_the_products_key_is_rejected` (and its inline comment `# the old flat-map shape` and payload `{"sk-a": "proj_a"}` stay, only the JSON key checked in the `pytest.raises(..., match=...)` changes from `"keys file must be"` to `"products file must be"`); `test_a_project_entry_missing_a_field_is_rejected` → `test_a_product_entry_missing_a_field_is_rejected`; `test_a_duplicate_slug_is_rejected` unchanged name, body renamed; `test_a_duplicate_api_key_is_rejected` unchanged name, body renamed; `test_a_valid_keys_file_round_trips_through_resolve_config` → `test_a_valid_products_file_round_trips_through_resolve_config`; `test_init_keys_file_writes_a_loadable_roster` → `test_init_products_file_writes_a_loadable_roster`; `test_init_keys_file_refuses_to_overwrite_an_existing_file` → `test_init_products_file_refuses_to_overwrite_an_existing_file`; `test_init_keys_file_generates_a_different_key_each_time` → `test_init_products_file_generates_a_different_key_each_time`; `test_a_registered_key_lands_under_its_own_project` → `test_a_registered_key_lands_under_its_own_product`; `test_two_projects_writing_the_same_journey_id_never_collide` → `test_two_products_writing_the_same_journey_id_never_collide`; `test_api_key_and_projects_are_mutually_exclusive` → `test_api_key_and_products_are_mutually_exclusive` (and its `match="not both"` stays — the raised message still says "not both", only the surrounding words `product`/`products` change per Step 3's new message); `test_get_projects_lists_the_roster_by_slug_and_name` → `test_get_products_lists_the_roster_by_slug_and_name` (and its expected body key `"projects"` → `"products"`); `test_get_projects_never_includes_api_keys` → `test_get_products_never_includes_api_keys`; `test_get_projects_requires_a_registered_key` → `test_get_products_requires_a_registered_key`; `test_get_projects_is_404_outside_project_scoped_mode` → `test_get_products_is_404_outside_product_scoped_mode`; `project_path(...)` helper function → `product_path(...)`, and every call site; `test_a_batch_is_project_scoped_like_single_sends` → `test_a_batch_is_product_scoped_like_single_sends`.
- The `# Project scoping (item 1.6) — multiple registered keys, isolated storage` section comment → `# Product scoping — multiple registered keys, isolated storage`; the `# GET /projects — the roster, names + slugs, never keys` section comment → `# GET /products — the roster, names + slugs, never keys`.

- [ ] **Step 2 (verify): Run the renamed suite**

Run: `cd services/collector && uv run pytest tests -q`
Expected: same pass count as before this task (55, per this session's last run) — every renamed test still exercises the identical behavior under its new name.

- [ ] **Step 8: Lint**

Run: `cd services/collector && uv run --extra dev black . && uv run --extra dev isort . && uv run --extra dev flake8 --max-line-length=88 --extend-ignore=E203,E501,W503,F541,F841 src tests && uv run --extra dev pyrefly check`
Expected: clean (run black/isort without `--check` first since this task touches many lines, then re-run the test suite to confirm formatting didn't change behavior).

- [ ] **Step 9: Commit**

```bash
git add services/collector/src/odyssey_collector/server.py services/collector/tests/test_server.py
git commit -m "refactor(collector): rename Project to Product

Project was always the wrong word for the top-level, unique-key-per-
tenant auth boundary -- Product is what it actually is. Clean rename,
no backward-compat alias (nothing has publicly shipped against the old
--keys-file/{\"projects\":[...]} shape yet, per docs/WORKING.md): Project
dataclass -> Product, --keys-file -> --products-file,
ODYSSEY_COLLECTOR_KEYS_FILE -> ODYSSEY_COLLECTOR_PRODUCTS_FILE,
--init-keys-file -> --init-products-file, --project-slug/--project-name
-> --product-slug/--product-name, GET /projects -> GET /products.
Mechanical rename throughout server.py and every test in
test_server.py -- same pass count before and after (55).

See docs/superpowers/specs/2026-09-02-product-project-metrics-design.md."
```

---

### Task 3: `services/collector` — `POST /metrics`

**Files:**
- Modify: `services/collector/src/odyssey_collector/server.py` (add a handler + storage function)
- Test: `services/collector/tests/test_server.py` (new tests, appended)

**Interfaces:**
- Consumes: `CollectorConfig.products`/`.api_key`/`.data_dir`, `_Handler._authenticate()`, `_Handler._read_body()`, `_Handler._respond()`, `_safe_stem()` — all from Task 2's renamed code.
- Produces: `POST /metrics` route in `do_POST`; storage at `<data_dir>/<product_slug>/metrics/<YYYY-MM-DD>.jsonl` (product-scoped) or `<data_dir>/metrics/<YYYY-MM-DD>.jsonl` (single-key/open mode).

- [ ] **Step 1: Write the failing tests**

Append to `services/collector/tests/test_server.py`, in a new section after the batching tests and before "Malformed input":

```python
# --------------------------------------------------------------------------
# POST /metrics — opt-in host telemetry, its own channel, own storage
# --------------------------------------------------------------------------


def test_a_metrics_snapshot_is_accepted_and_stored(running):
    payload = json.dumps({"hostname": "box-1", "os": "Linux-test"}).encode()
    request = urllib.request.Request(
        f"{endpoint(running)}/metrics", data=payload, method="POST"
    )
    with urllib.request.urlopen(request) as resp:
        assert resp.status == 200
        assert json.loads(resp.read()) == {"ok": True}

    stored = list((running.config.data_dir / "metrics").glob("*.jsonl"))
    assert len(stored) == 1
    line = json.loads(stored[0].read_text().splitlines()[0])
    assert line["hostname"] == "box-1"
    assert line["os"] == "Linux-test"


def test_a_metrics_snapshot_records_the_real_peer_public_ip(running):
    payload = json.dumps({"hostname": "box-1"}).encode()
    request = urllib.request.Request(
        f"{endpoint(running)}/metrics", data=payload, method="POST"
    )
    with urllib.request.urlopen(request):
        pass

    stored = list((running.config.data_dir / "metrics").glob("*.jsonl"))
    line = json.loads(stored[0].read_text().splitlines()[0])
    # A local test client always connects from loopback -- this proves the
    # server derived it from the real socket, not from anything the client
    # could have claimed in the payload itself.
    assert line["public_ip"] in ("127.0.0.1", "::1")


def test_a_metrics_snapshot_requires_authorization_when_guarded(guarded):
    payload = json.dumps({"hostname": "box-1"}).encode()
    request = urllib.request.Request(
        f"{endpoint(guarded)}/metrics", data=payload, method="POST"
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(request)
    assert exc_info.value.code == 401
    assert not list((guarded.config.data_dir / "metrics").glob("*.jsonl"))


def test_a_metrics_snapshot_lands_under_its_product_when_scoped(scoped):
    payload = json.dumps({"hostname": "box-1"}).encode()
    request = urllib.request.Request(
        f"{endpoint(scoped)}/metrics",
        data=payload,
        method="POST",
        headers={"Authorization": "Bearer sk-acme"},
    )
    with urllib.request.urlopen(request):
        pass

    stored = list((scoped.config.data_dir / "proj_acme" / "metrics").glob("*.jsonl"))
    assert len(stored) == 1
    assert not list((scoped.config.data_dir / "proj_globex" / "metrics").glob("*.jsonl"))


def test_a_malformed_metrics_body_is_rejected_with_400(running):
    request = urllib.request.Request(
        f"{endpoint(running)}/metrics", data=b"not json", method="POST"
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(request)
    assert exc_info.value.code == 400
    assert not list((running.config.data_dir / "metrics").glob("*.jsonl"))


def test_a_non_object_metrics_body_is_rejected_with_400(running):
    request = urllib.request.Request(
        f"{endpoint(running)}/metrics",
        data=json.dumps([1, 2, 3]).encode(),
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(request)
    assert exc_info.value.code == 400
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd services/collector && uv run pytest tests/test_server.py -k metrics -v`
Expected: FAIL — `/metrics` currently 404s (`do_POST` has no route for it), so every test above fails on the `404` it actually gets vs. the `200`/`400`/`401` it expects.

- [ ] **Step 3: Implement `POST /metrics`**

In `services/collector/src/odyssey_collector/server.py`, add the route in `do_POST` (after the existing `/batch/events` check, before the `/journeys/.../events` parsing):

```python
    def do_POST(self) -> None:  # noqa: N802 - stdlib method name
        if self.path.strip("/") == "batch/events":
            self._do_batch_post()
            return
        if self.path.strip("/") == "metrics":
            self._do_metrics_post()
            return

        parts = self.path.strip("/").split("/")
        # ... (unchanged from here down)
```

Add the handler method (near `_do_batch_post`):

```python
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
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd services/collector && uv run pytest tests/test_server.py -k metrics -v`
Expected: PASS, all 6.

- [ ] **Step 5: Run the full suite**

Run: `cd services/collector && uv run pytest tests -q`
Expected: 61 passed (55 from Task 2 + 6 new).

- [ ] **Step 6: Lint**

Run: `cd services/collector && uv run --extra dev black --check . && uv run --extra dev isort --check-only . && uv run --extra dev flake8 --max-line-length=88 --extend-ignore=E203,E501,W503,F541,F841 src tests && uv run --extra dev pyrefly check`
Expected: clean.

- [ ] **Step 7: End-to-end verification (matches this session's own established pattern)**

```bash
cd services/collector
uv run odyssey-collector --port 8097 --data-dir /tmp/metrics-e2e &
sleep 1
curl -s -X POST http://127.0.0.1:8097/metrics -d '{"hostname":"real-check"}'
cat /tmp/metrics-e2e/metrics/*.jsonl
kill %1
```
Expected: `{"ok": true}`, then a JSONL line containing `"hostname": "real-check"` and `"public_ip": "127.0.0.1"`.

- [ ] **Step 8: Commit**

```bash
git add services/collector/src/odyssey_collector/server.py services/collector/tests/test_server.py
git commit -m "feat(collector): add POST /metrics -- opt-in host telemetry channel

Independent of journey capture: same auth rules (open/single-key/
product-scoped), own storage subdirectory (<data_dir>/<slug>/metrics/
<date>.jsonl or <data_dir>/metrics/<date>.jsonl), never mixed into a
journey shard. Records public_ip server-side from the real TCP peer
address of the connection -- the posting SDK never reports its own
public IP. 6 new tests; verified end to end against a real running
collector.

See docs/superpowers/specs/2026-09-02-product-project-metrics-design.md."
```

---

### Task 4: `packages/odyssey-core` — `odyssey/project.py`

**Files:**
- Create: `packages/odyssey-core/src/odyssey/project.py`
- Test: `packages/odyssey-core/tests/test_project.py`
- Modify: `packages/odyssey-core/scripts/run_tests.sh` (add a `project` module entry, per its own "add new modules to the case below" rule)

**Interfaces:**
- Produces: `ENV_PROJECT = "ODYSSEY_PROJECT"`; `resolve_project(*, cwd: Optional[Path] = None) -> Optional[str]` — env var, then `.git/config`'s `origin` remote, then `cwd` dirname; never raises.

- [ ] **Step 1: Write the failing tests**

Create `packages/odyssey-core/tests/test_project.py`:

```python
from __future__ import annotations

import subprocess
from pathlib import Path

from odyssey.project import ENV_PROJECT, resolve_project


def test_env_var_wins_over_everything(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_PROJECT, "from-env")
    (tmp_path / "irrelevant.txt").touch()
    assert resolve_project(cwd=tmp_path) == "from-env"


def test_no_git_falls_back_to_dirname(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_PROJECT, raising=False)
    project_dir = tmp_path / "my-cool-project"
    project_dir.mkdir()
    assert resolve_project(cwd=project_dir) == "my-cool-project"


def test_git_remote_origin_wins_over_dirname(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_PROJECT, raising=False)
    repo_dir = tmp_path / "checkout-dir-name-differs"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:acme/real-repo-name.git"],
        cwd=repo_dir,
        check=True,
    )
    assert resolve_project(cwd=repo_dir) == "real-repo-name"


def test_git_remote_origin_without_dot_git_suffix(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_PROJECT, raising=False)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/acme/no-suffix"],
        cwd=repo_dir,
        check=True,
    )
    assert resolve_project(cwd=repo_dir) == "no-suffix"


def test_git_repo_with_no_origin_remote_falls_back_to_dirname(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_PROJECT, raising=False)
    repo_dir = tmp_path / "no-origin-repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    assert resolve_project(cwd=repo_dir) == "no-origin-repo"


def test_malformed_git_config_falls_back_to_dirname(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_PROJECT, raising=False)
    repo_dir = tmp_path / "broken-git-repo"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()
    (repo_dir / ".git" / "config").write_text("not = [a valid = git config")
    assert resolve_project(cwd=repo_dir) == "broken-git-repo"


def test_git_dir_found_from_a_subdirectory(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_PROJECT, raising=False)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:acme/repo.git"],
        cwd=repo_dir,
        check=True,
    )
    sub = repo_dir / "a" / "b"
    sub.mkdir(parents=True)
    assert resolve_project(cwd=sub) == "repo"
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd packages/odyssey-core && uv run pytest tests/test_project.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'odyssey.project'`.

- [ ] **Step 3: Implement**

Create `packages/odyssey-core/src/odyssey/project.py`:

```python
"""Auto-detect a "project" tag -- which repo/codebase a process is
capturing from. Purely descriptive: it lands in
``JourneyHeader.journey_metadata["project"]`` (see ``odyssey.capture``).
Never an auth boundary -- that is ``services/collector``'s unrelated
``Product`` concept (a unique api_key per top-level tenant).

Chain: ``ODYSSEY_PROJECT`` env var, then ``.git/config``'s
``[remote "origin"]`` URL, then the cwd's directory name. The caller
(``odyssey.config.resolve()``) handles the "explicit argument" step above
this chain via its own sentinel -- this function only ever runs the
auto-detect part, and never raises: every failure degrades to the next
step, ending at the directory name, which always succeeds.
"""

from __future__ import annotations

import configparser
import os
from pathlib import Path
from typing import Optional

ENV_PROJECT = "ODYSSEY_PROJECT"


def _from_git_remote(start: Path) -> Optional[str]:
    """``start``'s ``.git/config``, walking up through parents the same way
    ``git`` itself resolves a repo from a subdirectory. Returns the last
    path segment of ``[remote "origin"]``'s ``url``, minus a trailing
    ``.git``. ``None`` on any failure -- no ``.git``, no ``origin``
    remote, unreadable or malformed config -- never raises.
    """
    try:
        current = start.resolve()
    except OSError:
        return None
    for candidate in (current, *current.parents):
        git_dir = candidate / ".git"
        if not git_dir.is_dir():
            continue
        config_path = git_dir / "config"
        if not config_path.exists():
            return None
        parser = configparser.ConfigParser()
        try:
            parser.read(config_path)
        except configparser.Error:
            return None
        for section in parser.sections():
            if section.strip() == 'remote "origin"':
                url = parser.get(section, "url", fallback=None)
                if not url:
                    return None
                name = url.rstrip("/").rsplit("/", 1)[-1]
                if name.endswith(".git"):
                    name = name[: -len(".git")]
                return name or None
        return None
    return None


def resolve_project(*, cwd: Optional[Path] = None) -> Optional[str]:
    """``ODYSSEY_PROJECT`` env var, then git remote ``origin``, then the
    cwd's directory name. Always returns a usable value in practice --
    the directory name step only fails to produce one if the working
    directory itself no longer exists, in which case this returns
    ``None`` rather than raising.
    """
    env = os.environ.get(ENV_PROJECT)
    if env:
        return env
    try:
        start = cwd if cwd is not None else Path.cwd()
    except OSError:
        return None
    detected = _from_git_remote(start)
    if detected:
        return detected
    try:
        name = start.resolve().name
    except OSError:
        return None
    return name or None
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd packages/odyssey-core && uv run pytest tests/test_project.py -v`
Expected: PASS, all 7.

- [ ] **Step 5: Add the module to the test runner's module map**

In `packages/odyssey-core/scripts/run_tests.sh`, add a line to the header comment list (after `#   context     — ...`):

```
#   project     — auto-detected "project" tag: env, git remote, dirname fallback
```

And a case branch (after the `context)` branch):

```bash
  project)
    uv run pytest tests/test_project.py "$@"
    ;;
```

- [ ] **Step 6: Lint**

Run: `cd packages/odyssey-core && uv run --extra dev black --check . && uv run --extra dev isort --check-only . && uv run --extra dev flake8 --max-line-length=88 --extend-ignore=E203,E501,W503,F541,F841 src tests && uv run --extra dev pyrefly check`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add packages/odyssey-core/src/odyssey/project.py packages/odyssey-core/tests/test_project.py packages/odyssey-core/scripts/run_tests.sh
git commit -m "feat(core): add odyssey/project.py -- auto-detected project tag

ODYSSEY_PROJECT env var, then .git/config's origin remote (walking up
through parent directories the same way git itself resolves a repo from
a subdirectory), then the cwd directory name. Purely descriptive, never
raises -- every failure degrades to the next step. Not wired into
init()/capture yet, that's the next task. 7 new tests.

See docs/superpowers/specs/2026-09-02-product-project-metrics-design.md."
```

---

### Task 5: Wire `project` into `odyssey.init()` and journey capture

**Files:**
- Modify: `packages/odyssey-core/src/odyssey/config.py` (add `UNSET` sentinel, `project` field, wire into `resolve()`)
- Modify: `packages/odyssey-core/src/odyssey/client.py` (`init()` gains a `project` parameter)
- Modify: `packages/odyssey-core/src/odyssey/capture.py` (`journey()` seeds `metadata["project"]`)
- Test: `packages/odyssey-core/tests/test_sdk.py` (or wherever `odyssey.init()`/`journey()` integration tests live — confirm the exact file with `grep -rn "def test_init\|import odyssey$" packages/odyssey-core/tests/test_sdk.py | head` before adding; append there)
- Test: extend `packages/odyssey-core/tests/test_project.py` with `Config`-level tests for the sentinel

**Interfaces:**
- Consumes: `resolve_project()` from Task 4.
- Produces: `odyssey.config.UNSET` (a sentinel object); `Config.project: Optional[str]`; `odyssey.init(..., project: Any = UNSET, ...)`; every journey opened after `init()` carries `journey_metadata["project"]` when `Config.project is not None` and the caller didn't already pass their own `project=` keyword to `odyssey.journey(...)`.

- [ ] **Step 1: Write the failing test for the sentinel in `config.py`**

Append to `packages/odyssey-core/tests/test_project.py`:

```python
from odyssey.config import UNSET, resolve


def test_config_resolve_runs_auto_detect_when_project_is_unset(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_PROJECT, "auto-detected")
    config = resolve()
    assert config.project == "auto-detected"


def test_config_resolve_explicit_project_wins_over_env(monkeypatch):
    monkeypatch.setenv(ENV_PROJECT, "from-env")
    config = resolve(project="from-caller")
    assert config.project == "from-caller"


def test_config_resolve_explicit_none_disables_project_entirely(monkeypatch):
    monkeypatch.setenv(ENV_PROJECT, "from-env")
    config = resolve(project=None)
    assert config.project is None


def test_config_resolve_project_default_is_unset_not_none():
    # UNSET must be importable and distinct from None -- this is the whole
    # point of the sentinel (see config.py's docstring on drain_interval_set
    # for the established precedent this mirrors).
    assert UNSET is not None
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd packages/odyssey-core && uv run pytest tests/test_project.py -v -k config_resolve`
Expected: FAIL — `resolve()` has no `project` parameter yet, `ImportError: cannot import name 'UNSET'`.

- [ ] **Step 3: Implement in `config.py`**

Add near the top of `packages/odyssey-core/src/odyssey/config.py`, after the existing `ENV_*` constants (after line 27):

```python
from odyssey.project import resolve_project

# Distinguishes "the caller didn't pass this argument, run the normal
# resolution chain" from "the caller explicitly passed None" -- the same
# problem drain_interval_set solves for drain_interval, but as a sentinel
# value instead of a paired boolean, since `project` has no companion
# "_set" flag threaded through every call site the way drain_interval
# does. Any object identity works; this one is deliberately mundane.
UNSET = object()
```

Add `project: Optional[str]` to the `Config` dataclass (after the existing `sample_rate` field, since that is the last field currently listed):

```python
    sample_rate: float
    # Which repo/codebase this process is capturing from -- purely
    # descriptive (lands in JourneyHeader.journey_metadata), never an auth
    # concept. None means "don't tag journeys with a project at all",
    # which is different from "not specified" -- see resolve()'s
    # project=UNSET default.
    project: Optional[str]
```

In `resolve()`'s signature, add `project: Any = UNSET` as a keyword-only parameter (needs `from typing import Any` added to the existing `typing` import if not already present — check the file's import line first), and in the returned `Config(...)` call add:

```python
        project=(project if project is not UNSET else resolve_project()),
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd packages/odyssey-core && uv run pytest tests/test_project.py -v`
Expected: PASS, all 11 (7 from Task 4 + 4 new).

- [ ] **Step 5: Wire `project` through `odyssey.init()`**

In `packages/odyssey-core/src/odyssey/client.py`:
- Add `from odyssey.config import Config, UNSET, resolve` (extending the existing `from odyssey.config import Config, resolve` import on line 27 — check the exact current line before editing, since it may already import other names).
- Add `Any` to the existing `from typing import ...` line if not already imported (it already is, per line 23: `from typing import Any, Dict, List, Optional, Protocol, Sequence, runtime_checkable`).
- In `init()`'s signature, add `project: Any = UNSET,` as a keyword-only parameter (alongside the existing `sample_rate: Optional[float] = None,`).
- In `init()`'s body, add `project=project,` to the `resolve(...)` call (alongside the existing `sample_rate=sample_rate,` line).
- Add a paragraph to `init()`'s docstring, after the existing `sample_rate` paragraph:

```
    ``project`` (``ODYSSEY_PROJECT``) tags every journey opened after this
    call with which repo/codebase it came from -- auto-detected from a git
    remote or the working directory when not passed explicitly, purely
    descriptive (``journey_metadata["project"]``), never an auth concept.
    Pass ``project=None`` explicitly to disable the tag entirely, which is
    different from not passing it at all (which runs the auto-detect
    chain) -- see ``odyssey.project.resolve_project``.
```

- [ ] **Step 6: Write the failing test for journey tagging**

Find the existing `odyssey.init()`/`odyssey.journey()` integration test file:

Run: `cd packages/odyssey-core && grep -l "def test_init\|import odyssey$" tests/*.py`

Append to whichever file that finds (expected: `tests/test_sdk.py`) — adapt the exact `odyssey.init(...)`/`spool_dir=`/`journey(...)` call shape to match that file's existing fixtures/patterns (read a neighboring test in the same file first, since this plan cannot see its exact current fixture helper names):

```python
def test_init_project_tags_every_journey(tmp_path, monkeypatch):
    monkeypatch.setenv("ODYSSEY_PROJECT", "tagged-project")
    odyssey.init(spool_dir=tmp_path / "spool", out_dir=tmp_path / "out", force=True)
    with odyssey.journey(id="j_project_tag"):
        pass
    header = odyssey.get_client().spool.header("j_project_tag")
    assert header.journey_metadata["project"] == "tagged-project"


def test_journey_level_project_kwarg_overrides_the_configured_one(tmp_path):
    odyssey.init(
        spool_dir=tmp_path / "spool", out_dir=tmp_path / "out",
        project="from-init", force=True,
    )
    with odyssey.journey(id="j_override", project="from-journey-call"):
        pass
    header = odyssey.get_client().spool.header("j_override")
    assert header.journey_metadata["project"] == "from-journey-call"


def test_project_none_means_no_project_tag_at_all(tmp_path):
    odyssey.init(
        spool_dir=tmp_path / "spool", out_dir=tmp_path / "out",
        project=None, force=True,
    )
    with odyssey.journey(id="j_no_project"):
        pass
    header = odyssey.get_client().spool.header("j_no_project")
    assert "project" not in (header.journey_metadata or {})
```

(If `Spool` has no public `header(journey_id)` lookup, use whatever this test file's existing tests already use to inspect a written shard's header — e.g. reading the shard file directly via `odyssey.jsonl.read_events` — grep the file for the existing pattern and match it rather than inventing a new one.)

- [ ] **Step 7: Run to verify they fail**

Run: `cd packages/odyssey-core && uv run pytest tests/test_sdk.py -k project -v`
Expected: FAIL — `journey_metadata` has no `"project"` key yet.

- [ ] **Step 8: Implement in `capture.py`**

In `packages/odyssey-core/src/odyssey/capture.py`'s `journey()` function, replace the `ctx = JourneyContext(` construction's `metadata=_jsonable(dict(metadata)),` line (line 381) with:

```python
    tagged_metadata = dict(metadata)
    if (
        client is not None
        and client.config.project is not None
        and "project" not in tagged_metadata
    ):
        tagged_metadata["project"] = client.config.project

    client_for_journey = client
    ctx = JourneyContext(
        journey_id=id or uuid4().hex,
        allocator=(
            client_for_journey.allocator
            if client_for_journey is not None
            else _NULL_ALLOCATOR
        ),
        metadata=_jsonable(tagged_metadata),
        data_source=data_source,
        trace_id=trace_id,
    )
```

(This restructures the existing `allocator=(client.allocator if client is not None else _NULL_ALLOCATOR)` ternary to use a locally-named `client_for_journey` purely for readability around the new block above it — check the real surrounding lines 362-380 before editing, since the exact ternary formatting must be preserved/adapted rather than guessed; the only functional change is the `tagged_metadata` construction and passing it instead of `dict(metadata)`.)

- [ ] **Step 9: Run to verify they pass**

Run: `cd packages/odyssey-core && uv run pytest tests/test_sdk.py -k project -v`
Expected: PASS, all 3.

- [ ] **Step 10: Run the full core suite**

Run: `cd packages/odyssey-core && bash scripts/run_tests.sh all`
Expected: every module passes, including the 3 new + 11 from `test_project.py`.

- [ ] **Step 11: Lint**

Run: `cd packages/odyssey-core && uv run --extra dev black --check . && uv run --extra dev isort --check-only . && uv run --extra dev pyrefly check` (flake8 config per this member's own Taskfile.yml — check it for the exact ignore list before running, it may differ from services/collector's)
Expected: clean.

- [ ] **Step 12: Commit**

```bash
git add packages/odyssey-core/src/odyssey/config.py packages/odyssey-core/src/odyssey/client.py packages/odyssey-core/src/odyssey/capture.py packages/odyssey-core/tests/test_project.py packages/odyssey-core/tests/test_sdk.py
git commit -m "feat(core): wire project auto-detection into init()/journey()

odyssey.init(project=...) -- UNSET sentinel distinguishes 'not passed,
run auto-detect' from 'explicitly None, disable the tag entirely', same
pattern config.py's drain_interval_set already established. Every
journey opened after init() gets journey_metadata[\"project\"] seeded
from Config.project unless the journey() call already passed its own
project= kwarg (per-journey override wins). 7 new tests.

See docs/superpowers/specs/2026-09-02-product-project-metrics-design.md."
```

---

### Task 6: `packages/odyssey-core` — `odyssey/metrics.py`

**Files:**
- Create: `packages/odyssey-core/src/odyssey/metrics.py`
- Test: `packages/odyssey-core/tests/test_metrics.py`
- Modify: `packages/odyssey-core/scripts/run_tests.sh` (add a `metrics` module entry)

**Interfaces:**
- Consumes: `HttpTransport`, `HttpSinkError`, `_parse_retry_after` from `odyssey.sinks` (Task 1).
- Produces: `build_snapshot(project: Optional[str] = None) -> Dict[str, Any]`; `MetricsReporter` class with `__init__(self, *, interval_seconds: float, project: Optional[str] = None, endpoint: Optional[str] = None, api_key: Optional[str] = None, on_error: Optional[Callable[[BaseException], None]] = None)`, `start() -> None`, `stop(timeout: float = 5.0) -> None`.

- [ ] **Step 1: Write the failing tests**

Create `packages/odyssey-core/tests/test_metrics.py`:

```python
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from odyssey.metrics import MetricsReporter, build_snapshot


def test_build_snapshot_has_the_stdlib_sourced_fields():
    snapshot = build_snapshot()
    assert "ts" in snapshot
    assert "hostname" in snapshot
    assert "os" in snapshot
    assert isinstance(snapshot["cpu_count"], int)
    assert "disk_total_bytes" in snapshot
    assert "disk_free_bytes" in snapshot
    assert "project" not in snapshot  # no project passed


def test_build_snapshot_includes_project_when_given():
    snapshot = build_snapshot(project="my-project")
    assert snapshot["project"] == "my-project"


class _CapturingHandler(BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        type(self).received.append(json.loads(body))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def capturing_server():
    _CapturingHandler.received = []
    server = HTTPServer(("127.0.0.1", 0), _CapturingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join()


def test_metrics_reporter_posts_on_the_configured_interval(capturing_server):
    host, port = capturing_server.server_address
    reporter = MetricsReporter(
        interval_seconds=0.05, endpoint=f"http://{host}:{port}", project="p"
    )
    reporter.start()
    try:
        time.sleep(0.2)
    finally:
        reporter.stop()
    assert len(_CapturingHandler.received) >= 2
    assert _CapturingHandler.received[0]["project"] == "p"


def test_metrics_reporter_never_raises_on_a_transport_failure():
    errors = []
    reporter = MetricsReporter(
        interval_seconds=0.05,
        endpoint="http://127.0.0.1:1",  # nothing listens here
        on_error=lambda exc: errors.append(exc),
    )
    reporter.start()
    try:
        time.sleep(0.2)
    finally:
        reporter.stop()
    assert len(errors) >= 1


def test_metrics_reporter_stop_is_idempotent(capturing_server):
    host, port = capturing_server.server_address
    reporter = MetricsReporter(interval_seconds=10, endpoint=f"http://{host}:{port}")
    reporter.start()
    reporter.stop()
    reporter.stop()  # must not raise
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd packages/odyssey-core && uv run pytest tests/test_metrics.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'odyssey.metrics'`.

- [ ] **Step 3: Implement**

Create `packages/odyssey-core/src/odyssey/metrics.py`:

```python
"""Opt-in, off-by-default server/host telemetry -- its own channel,
entirely independent of journey capture (see the design spec's "Metrics
channel" decision). Nothing in this module runs unless a caller
explicitly starts a :class:`MetricsReporter`; ``odyssey.init()`` only
does so when ``collect_metrics=True``.

Only stdlib-sourced fields: hostname, OS, CPU count, disk usage. Memory
is Linux-only (``/proc/meminfo``) and simply omitted elsewhere -- a
partial snapshot beats a failed one. The SDK never determines or reports
its own public IP; ``services/collector``'s ``POST /metrics`` handler
records that server-side, from the real TCP peer address.
"""

from __future__ import annotations

import gzip
import json
import os
import platform
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from odyssey.sinks import HttpSinkError, HttpTransport

MIN_METRICS_INTERVAL = 5.0
MAX_METRICS_INTERVAL = 86400.0


def _read_meminfo() -> Dict[str, int]:
    """Linux-only: total/available memory in bytes from ``/proc/meminfo``.
    Returns ``{}`` on any platform/condition where it can't be read."""
    path = Path("/proc/meminfo")
    if not path.exists():
        return {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return {}
    values: Dict[str, int] = {}
    for line in lines:
        if line.startswith("MemTotal:"):
            values["memory_total_bytes"] = int(line.split()[1]) * 1024
        elif line.startswith("MemAvailable:"):
            values["memory_available_bytes"] = int(line.split()[1]) * 1024
    return values


def build_snapshot(project: Optional[str] = None) -> Dict[str, Any]:
    """One point-in-time host snapshot. Never raises -- individual fields
    are simply omitted if their source is unavailable on this platform."""
    snapshot: Dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "hostname": platform.node(),
        "os": platform.platform(),
        "cpu_count": os.cpu_count(),
    }
    snapshot.update(_read_meminfo())
    try:
        usage = shutil.disk_usage(Path.cwd())
        snapshot["disk_total_bytes"] = usage.total
        snapshot["disk_free_bytes"] = usage.free
    except OSError:
        pass
    if project is not None:
        snapshot["project"] = project
    return snapshot


class _MetricsTransport(HttpTransport):
    """Posts one snapshot to ``{endpoint}/metrics``. Reuses HttpTransport's
    connection/gzip/backoff exactly as HttpSink does -- see
    odyssey/sinks.py."""

    def post(self, snapshot: Dict[str, Any]) -> None:
        self._check_backoff("metrics")

        payload = json.dumps(snapshot).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self.compress:
            payload = gzip.compress(payload)
            headers["Content-Encoding"] = "gzip"
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        path = f"{self._base_path}/metrics"
        with self._lock:
            status, retry_after, _body, error = self._request(path, payload, headers)

        if error is not None:
            raise HttpSinkError(f"metrics: could not reach {self.endpoint}: {error}")
        if status == 429:
            self._note_retry_after(retry_after)
            raise HttpSinkError(f"metrics: rate limited by {self.endpoint}")
        if status is None or status >= 300:
            raise HttpSinkError(f"metrics: HTTP {status} from {self.endpoint}")


class MetricsReporter:
    """Background thread that posts one :func:`build_snapshot` per
    ``interval_seconds``, modeled directly on
    ``odyssey.spool.IntervalDrainer`` -- same shape (daemon thread, sleeps
    the interval, wakes, does the one thing, repeats).

    Never raises out of the background loop (ADR 0004's "never crash the
    host") -- a failed post calls ``on_error`` if one was given, and tries
    again next interval. ``on_error`` is how ``Client`` wires this into
    its own error-counting (``note_error``) without this module depending
    on ``odyssey.client``.
    """

    def __init__(
        self,
        *,
        interval_seconds: float,
        project: Optional[str] = None,
        endpoint: Optional[str] = None,
        api_key: Optional[str] = None,
        on_error: Optional[Callable[[BaseException], None]] = None,
    ) -> None:
        self._interval = min(
            MAX_METRICS_INTERVAL, max(MIN_METRICS_INTERVAL, interval_seconds)
        )
        self._project = project
        self._on_error = on_error
        self._transport = _MetricsTransport(endpoint, api_key=api_key)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("metrics reporter already started")
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        self._transport.close()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._transport.post(build_snapshot(project=self._project))
            except Exception as exc:  # noqa: BLE001 - never crash the host
                if self._on_error is not None:
                    self._on_error(exc)
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd packages/odyssey-core && uv run pytest tests/test_metrics.py -v`
Expected: PASS, all 6.

- [ ] **Step 5: Add the module to the test runner's module map**

In `packages/odyssey-core/scripts/run_tests.sh`, add to the header comment (after the new `project` line from Task 4):

```
#   metrics     — opt-in host telemetry: snapshot fields, background reporter
```

And a case branch:

```bash
  metrics)
    uv run pytest tests/test_metrics.py "$@"
    ;;
```

- [ ] **Step 6: Lint**

Run: `cd packages/odyssey-core && uv run --extra dev black --check . && uv run --extra dev isort --check-only . && uv run --extra dev pyrefly check`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add packages/odyssey-core/src/odyssey/metrics.py packages/odyssey-core/tests/test_metrics.py packages/odyssey-core/scripts/run_tests.sh
git commit -m "feat(core): add odyssey/metrics.py -- opt-in host telemetry reporter

MetricsReporter is a background thread modeled on IntervalDrainer,
posting build_snapshot() (hostname/OS/CPU count/disk usage, all
stdlib-sourced -- memory is Linux-only via /proc/meminfo, omitted
elsewhere) to {endpoint}/metrics on a configurable interval. Reuses
HttpTransport (extracted in an earlier task) for the actual POST rather
than hand-rolling http.client code. Never raises out of the background
loop; calls an optional on_error callback instead. Not wired into
init()/Client yet, that's the next task. 6 new tests.

See docs/superpowers/specs/2026-09-02-product-project-metrics-design.md."
```

---

### Task 7: Wire `metrics` into `odyssey.init()` and `Client`

**Files:**
- Modify: `packages/odyssey-core/src/odyssey/config.py` (add `collect_metrics`/`metrics_interval` fields + env resolution)
- Modify: `packages/odyssey-core/src/odyssey/client.py` (`init()` gains params; `Client` starts/stops a `MetricsReporter`)
- Test: `packages/odyssey-core/tests/test_metrics.py` (append `Config`/`Client`-level tests)

**Interfaces:**
- Consumes: `MetricsReporter` from Task 6.
- Produces: `ODYSSEY_COLLECT_METRICS`, `ODYSSEY_METRICS_INTERVAL` env vars; `Config.collect_metrics: bool`, `Config.metrics_interval: float`; `odyssey.init(..., collect_metrics: Optional[bool] = None, metrics_interval: Optional[float] = None)`; `Client.metrics_reporter: Optional[MetricsReporter]`.

- [ ] **Step 1: Write the failing tests**

Append to `packages/odyssey-core/tests/test_metrics.py`:

```python
from odyssey.config import resolve


def test_collect_metrics_defaults_to_false():
    assert resolve().collect_metrics is False


def test_collect_metrics_env_var(monkeypatch):
    monkeypatch.setenv("ODYSSEY_COLLECT_METRICS", "1")
    assert resolve().collect_metrics is True


def test_collect_metrics_explicit_beats_env(monkeypatch):
    monkeypatch.setenv("ODYSSEY_COLLECT_METRICS", "1")
    assert resolve(collect_metrics=False).collect_metrics is False


def test_metrics_interval_default_is_300(monkeypatch):
    monkeypatch.delenv("ODYSSEY_METRICS_INTERVAL", raising=False)
    assert resolve().metrics_interval == 300.0


def test_metrics_interval_env_var(monkeypatch):
    monkeypatch.setenv("ODYSSEY_METRICS_INTERVAL", "60")
    assert resolve().metrics_interval == 60.0
```

And append (import `odyssey` at the top the same way `test_sdk.py` does — check that file's import style, this test belongs in `test_metrics.py` regardless since it's about the metrics feature, not core capture):

```python
import odyssey


def test_init_with_collect_metrics_starts_a_reporter(tmp_path, monkeypatch):
    monkeypatch.setenv("ODYSSEY_ENDPOINT", "http://127.0.0.1:1")  # never has to connect
    client = odyssey.init(
        spool_dir=tmp_path / "spool",
        out_dir=tmp_path / "out",
        collect_metrics=True,
        metrics_interval=5,
        drain_interval=None,
        force=True,
    )
    try:
        assert client.metrics_reporter is not None
    finally:
        client.shutdown()


def test_init_without_collect_metrics_starts_no_reporter(tmp_path):
    client = odyssey.init(
        spool_dir=tmp_path / "spool",
        out_dir=tmp_path / "out",
        drain_interval=None,
        force=True,
    )
    try:
        assert client.metrics_reporter is None
    finally:
        client.shutdown()


def test_shutdown_stops_the_metrics_reporter(tmp_path, monkeypatch):
    monkeypatch.setenv("ODYSSEY_ENDPOINT", "http://127.0.0.1:1")
    client = odyssey.init(
        spool_dir=tmp_path / "spool",
        out_dir=tmp_path / "out",
        collect_metrics=True,
        drain_interval=None,
        force=True,
    )
    reporter = client.metrics_reporter
    client.shutdown()
    assert reporter is not None
    assert reporter._thread is None  # stop() cleared it
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd packages/odyssey-core && uv run pytest tests/test_metrics.py -v -k "collect_metrics or metrics_interval or reporter"`
Expected: FAIL — `resolve()`/`init()` don't accept `collect_metrics`/`metrics_interval` yet; `Client` has no `.metrics_reporter` attribute.

- [ ] **Step 3: Implement in `config.py`**

Add the two env var constants near the top (after the existing `ENV_DRAIN_BATCH_SIZE` constant):

```python
ENV_COLLECT_METRICS = "ODYSSEY_COLLECT_METRICS"
ENV_METRICS_INTERVAL = "ODYSSEY_METRICS_INTERVAL"

DEFAULT_METRICS_INTERVAL = 300.0
```

Add two fields to `Config` (after the `project` field added in Task 5):

```python
    # Opt-in, off by default -- see odyssey/metrics.py. When False, nothing
    # in that module ever runs and no host metadata leaves the process.
    collect_metrics: bool
    metrics_interval: float
```

In `resolve()`'s signature, add:

```python
    collect_metrics: Optional[bool] = None,
    metrics_interval: Optional[float] = None,
```

And in the returned `Config(...)` call:

```python
        collect_metrics=(
            collect_metrics
            if collect_metrics is not None
            else _flag(os.environ.get(ENV_COLLECT_METRICS), False)
        ),
        metrics_interval=(
            metrics_interval
            if metrics_interval is not None
            else _number(os.environ.get(ENV_METRICS_INTERVAL), DEFAULT_METRICS_INTERVAL)
        ),
```

(`_flag` and `_number` are the existing helpers already used for `enabled`/`debug` and `drain_interval` respectively — reuse them, do not write new ones.)

- [ ] **Step 4: Implement in `client.py`**

Add the import: `from odyssey.metrics import MetricsReporter` (near the existing `from odyssey.sinks import FileSink` line).

In `Client.__init__`, after the existing `self.drainer = ...` block (the `if config.drain_interval is not None:` block), add:

```python
        self.metrics_reporter: Optional[MetricsReporter] = None
        if config.collect_metrics:
            try:
                self.metrics_reporter = MetricsReporter(
                    interval_seconds=config.metrics_interval,
                    project=config.project,
                    on_error=lambda exc: self.note_error("metrics", exc),
                )
                self.metrics_reporter.start()
            except Exception as exc:  # noqa: BLE001 - never crash the host
                self.note_error("metrics", exc)
```

In `Client.shutdown`, after `result = self.flush()` and before `self.spool.close()`, add:

```python
        if self.metrics_reporter is not None:
            self.metrics_reporter.stop()
            self.metrics_reporter = None
```

In `init()`'s signature, add `collect_metrics: Optional[bool] = None, metrics_interval: Optional[float] = None,` (alongside the `project: Any = UNSET,` parameter added in Task 5), and in its `resolve(...)` call add `collect_metrics=collect_metrics, metrics_interval=metrics_interval,`.

Add a docstring paragraph to `init()`, after the `project` paragraph added in Task 5:

```
    ``collect_metrics`` (``ODYSSEY_COLLECT_METRICS``, default ``False``)
    opts into a background reporter that posts one host snapshot
    (hostname, OS, CPU count, disk usage) to ``{endpoint}/metrics`` every
    ``metrics_interval`` seconds (``ODYSSEY_METRICS_INTERVAL``, default
    ``300``). Off by default -- when off, nothing in ``odyssey.metrics``
    ever runs and no host metadata leaves the process. Uses the same
    ``endpoint``/``api_key`` resolution ``HttpSink`` does
    (``ODYSSEY_ENDPOINT``/``ODYSSEY_API_KEY``) -- there is no separate
    metrics-specific endpoint setting.
```

- [ ] **Step 5: Run to verify they pass**

Run: `cd packages/odyssey-core && uv run pytest tests/test_metrics.py -v`
Expected: PASS, all 14 (6 from Task 6 + 8 new).

- [ ] **Step 6: Run the full core suite**

Run: `cd packages/odyssey-core && bash scripts/run_tests.sh all`
Expected: every module passes.

- [ ] **Step 7: Add `health()` visibility**

In `Client.health()`, add to the returned dict (after the existing `"drainer_running": self.drainer is not None,` line):

```python
            "metrics_reporter_running": self.metrics_reporter is not None,
```

Run: `cd packages/odyssey-core && uv run pytest tests/test_sdk.py -k health -v`
Expected: PASS (confirm this doesn't break an existing exact-dict-equality health test — if one exists asserting the full health() shape, it needs this new key added to its expected dict too; grep for it first: `grep -n "def test.*health" tests/test_sdk.py`).

- [ ] **Step 8: Lint**

Run: `cd packages/odyssey-core && uv run --extra dev black --check . && uv run --extra dev isort --check-only . && uv run --extra dev pyrefly check`
Expected: clean.

- [ ] **Step 9: End-to-end verification (matches this session's established pattern)**

```bash
cd services/collector && uv run odyssey-collector --port 8098 --data-dir /tmp/e2e-metrics-full &
sleep 1
cd ../../packages/odyssey-core
uv run python -c "
import time, os
os.environ['ODYSSEY_ENDPOINT'] = 'http://127.0.0.1:8098'
import odyssey
odyssey.init(spool_dir='/tmp/e2e-spool', out_dir='/tmp/e2e-out', collect_metrics=True, metrics_interval=1, drain_interval=None, force=True)
time.sleep(2.5)
odyssey.get_client().shutdown()
"
cat /tmp/e2e-metrics-full/metrics/*.jsonl
kill %1
```
Expected: at least 2 JSONL lines, each with `hostname`/`os`/`cpu_count`/`public_ip`.

- [ ] **Step 10: Commit**

```bash
git add packages/odyssey-core/src/odyssey/config.py packages/odyssey-core/src/odyssey/client.py packages/odyssey-core/tests/test_metrics.py
git commit -m "feat(core): wire opt-in metrics reporting into init()/Client

odyssey.init(collect_metrics=True, metrics_interval=...) -- off by
default (ODYSSEY_COLLECT_METRICS/ODYSSEY_METRICS_INTERVAL env
equivalents). Client starts a MetricsReporter in __init__ when opted
in, stops it in shutdown(); a reporter failure is counted via
note_error (never crashes the host), visible through
Client.health()['metrics_reporter_running']. 8 new tests; verified
end to end against a real running collector.

See docs/superpowers/specs/2026-09-02-product-project-metrics-design.md."
```

---

### Task 8: Docs pass — collector rename + new SDK knobs

**Files:**
- Modify: `services/collector/README.md`
- Modify: `docs/runbooks/run-services.md`
- Modify: `docs/environment-variables.md`
- Modify: `docs/COMPONENTS.md`
- Modify: `CHANGELOG.md`
- Modify: `docs/superpowers/specs/2026-09-02-product-project-metrics-design.md` (mark implemented)

- [ ] **Step 1: `services/collector/README.md`**

Rename every `--keys-file`/`Project`/`ODYSSEY_COLLECTOR_KEYS_FILE`/`--init-keys-file`/`--project-slug`/`--project-name`/`GET /projects` reference to the `Product` equivalents from Task 2 (the config table, the "Project scoping" section header → "Product scoping", the JSON example's `"projects"` key → `"products"`, and the "Bootstrapping the file" subsection added in this session's earlier `--init-keys-file` work). Add a new "Opt-in metrics" section documenting `POST /metrics` (auth rules, storage path, that `public_ip` is server-derived) and pointing at `packages/odyssey-core/src/odyssey/metrics.py` as the SDK side.

- [ ] **Step 2: `docs/runbooks/run-services.md`**

Rename every `--keys-file`/`ODYSSEY_COLLECTOR_KEYS_FILE`/`--init-keys-file`/`--project-slug`/`--project-name` reference in the "Switching to project-scoped mode" section (renamed to "Switching to product-scoped mode") to the `Product`/`--products-file` equivalents, including the systemd unit's `Environment=` example line.

- [ ] **Step 3: `docs/environment-variables.md`**

- Rename `ODYSSEY_COLLECTOR_KEYS_FILE` row to `ODYSSEY_COLLECTOR_PRODUCTS_FILE`.
- Add rows for `ODYSSEY_PROJECT`, `ODYSSEY_COLLECT_METRICS`, `ODYSSEY_METRICS_INTERVAL` under the "Capture" table.
- Update the "Naming collision to know about" section — it currently only discusses `ODYSSEY_API_KEY` vs `ODYSSEY_COLLECTOR_API_KEY`; add a note that `project` (SDK, descriptive tag) and `Product`/`products` (collector, auth boundary) are unrelated concepts despite the similar words, cross-referencing the design spec.

- [ ] **Step 4: `docs/COMPONENTS.md`**

Update the `services/collector` and `packages/odyssey-core` sections to mention `Product`/`POST /metrics`/`odyssey/project.py`/`odyssey/metrics.py` in their command/API surface lists.

- [ ] **Step 5: `CHANGELOG.md`**

Add an `### Added`/`### Changed` entry under `[Unreleased]` covering: the `Project`→`Product` rename (breaking, no alias), `POST /metrics`, `odyssey/project.py`, `odyssey/metrics.py`, the `HttpTransport` extraction. Follow this repo's existing changelog entry style (see recent entries for the pattern — one bolded one-line summary, then the mechanism, then why).

- [ ] **Step 6: Mark the spec implemented**

In `docs/superpowers/specs/2026-09-02-product-project-metrics-design.md`, change the `Status:` line at the top from `approved (design), not yet planned/implemented` to `implemented — see git log for the commits`.

- [ ] **Step 7: Verify every link still resolves**

Run:
```bash
cd /path/to/odyssey
for f in README.md docs/COMPONENTS.md docs/environment-variables.md docs/runbooks/run-services.md services/collector/README.md; do
  dir=$(dirname "$f")
  grep -oE '\[[^]]+\]\(([^)]+)\)' "$f" | grep -oE '\(([^)]+)\)' | tr -d '()' | sort -u | while read -r link; do
    case "$link" in http*) continue ;; esac
    path="${link%%#*}"; [ -z "$path" ] && continue
    [ ! -e "$dir/$path" ] && echo "$f -> MISSING: $link"
  done
done
```
Expected: no output.

- [ ] **Step 8: Commit**

```bash
git add services/collector/README.md docs/runbooks/run-services.md docs/environment-variables.md docs/COMPONENTS.md CHANGELOG.md docs/superpowers/specs/2026-09-02-product-project-metrics-design.md
git commit -m "docs: Product rename + new project/metrics knobs across every doc

services/collector/README.md, docs/runbooks/run-services.md,
docs/environment-variables.md, docs/COMPONENTS.md updated for the
Project->Product rename and the two new opt-in features (project
auto-detection, metrics capture). CHANGELOG entry added. Spec marked
implemented. Every doc link re-verified to resolve."
```

---

## Self-Review Notes (for whoever executes this plan)

- **Spec coverage:** Migration style (clean rename) → Task 2. Project-as-metadata-tag → Tasks 4-5. Metrics-as-separate-channel → Tasks 3, 6-7. Collector-derived public IP → Task 3 Step 3 (`self.client_address[0]`). OS+CPU/mem/disk-only payload → Task 6. The spec's "Migration note for this session's own recent commit" (rename `--init-keys-file` too) → folded into Task 2 rather than a separate task, since it's the same mechanical rename.
- **Task 5 Step 6/8** intentionally can't pin exact fixture code for `test_sdk.py` sight-unseen — the step tells the executor to `grep` for the real pattern first rather than guessing and risking a plausible-but-wrong test. This is the one place in this plan where "no placeholders" is satisfied by "read the real file before writing this step's code" rather than by pre-written code, because this plan's author did not have that file's exact current contents in context at planning time.
- **Type/name consistency check:** `Product`/`products`/`products_file`/`product_for_key`/`_load_products_file`/`_init_products_file` used consistently from Task 2 onward. `resolve_project()`/`ENV_PROJECT`/`UNSET` from Task 4 used consistently in Tasks 5 and 8. `HttpTransport`/`_check_backoff`/`_note_retry_after` from Task 1 used consistently in Task 6's `_MetricsTransport`. `MetricsReporter`/`build_snapshot`/`collect_metrics`/`metrics_interval` from Task 6 used consistently in Task 7.
