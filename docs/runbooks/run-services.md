# Running `services/collector`, `services/api`, and `apps/web` in production

Every command below was actually run against this repo before being
written down (`gunicorn -k uvicorn.workers.UvicornWorker ...` against a
real `services/api`, a real `.venv/bin/odyssey-collector`, a real
`pnpm build` + `next start` for `apps/web`) — not copied from generic
docs.

## One venv for both, built once

```bash
cd /opt/odyssey                     # wherever this repo is deployed
uv sync --all-packages --extra prod
```

`--extra prod` pulls in `gunicorn` for `services/api` (see below); a
plain `uv sync --all-packages` is enough if you're only running
`services/collector` or using uvicorn's own `--workers` flag instead of
gunicorn. Either way you get one `.venv/bin/` with every workspace
member's console scripts in it: `odyssey`, `odyssey-collector`, `uvicorn`,
`gunicorn`.

## `services/collector` — stdlib `ThreadingHTTPServer`, not WSGI/ASGI

**gunicorn does not apply here.** `services/collector` is deliberately
plain `http.server` (see `services/collector/README.md`'s "Why stdlib,
not FastAPI") — there is no WSGI/ASGI application object for gunicorn (or
any app-server) to load. Run the process directly and let `systemd`
supervise it.

`/etc/systemd/system/odyssey-collector.service`:

```ini
[Unit]
Description=odyssey-collector (trace ingest)
After=network.target

[Service]
Type=simple
User=odyssey
Group=odyssey
WorkingDirectory=/opt/odyssey
Environment=ODYSSEY_COLLECTOR_HOST=127.0.0.1
Environment=ODYSSEY_COLLECTOR_PORT=8787
Environment=ODYSSEY_COLLECTOR_DATA_DIR=/var/lib/odyssey/collector-data
# one of these two, never both — see services/collector/README.md
# Environment=ODYSSEY_COLLECTOR_API_KEY=change-me
Environment=ODYSSEY_DB_URI=sqlite:////var/lib/odyssey/odyssey.db
Environment=ODYSSEY_COLLECTOR_AUTH_CACHE_TTL_SECONDS=60
ExecStart=/opt/odyssey/.venv/bin/odyssey-collector
Restart=on-failure
RestartSec=2
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

**No `ProtectSystem=strict`/`ReadWritePaths`/`PrivateTmp` here on
purpose** — an earlier version of this unit set `ProtectSystem=strict`
without `PrivateTmp`, which remounts `/tmp` read-only along with
everything outside `ReadWritePaths`; `_Handler._store`
(`services/collector/src/odyssey_collector/server.py`) writes each posted
batch to a scratch file to validate it before appending, so it 500'd
every `POST /journeys/<id>/events`/`/batch/events` with "no usable
temporary directory" (`POST /metrics` was unaffected — no temp file in
its path — so metrics landing while journey posts 500 was the signature
of this exact misconfiguration). `_store`'s scratch dir now lives under
`data_dir` itself regardless of this setting, so the underlying bug is
fixed in code either way — but this deployment runs without the
filesystem-hardening directives entirely, by choice, rather than
maintaining them alongside the app-level fix.

```bash
sudo mkdir -p /var/lib/odyssey/collector-data && sudo chown odyssey:odyssey /var/lib/odyssey/collector-data
sudo systemctl daemon-reload
sudo systemctl enable --now odyssey-collector
systemctl status odyssey-collector
journalctl -u odyssey-collector -f
```

**Quiet by default, no per-request access log** — `_Handler.log_message`
is a deliberate no-op unless `ODYSSEY_COLLECTOR_DEBUG=1` (or `--debug`) is
set, in which case every request (method, path, status) logs to stdout
via the `odyssey_collector.requests` logger, visible through
`journalctl -u odyssey-collector -f`. Add
`Environment=ODYSSEY_COLLECTOR_DEBUG=1` to the unit's `[Service]` block
and `daemon-reload && restart` to turn it on; remove it and restart again
to go back to quiet.

### Auth: `--api-key` vs `--db-uri` (product-scoped), never both

`services/collector` supports three independent auth modes: no auth
(neither set, for local dev), `ODYSSEY_COLLECTOR_API_KEY`/`--api-key`
(one shared bearer token, unscoped — unaffected by anything below), and
`ODYSSEY_DB_URI`/`--db-uri` (multi-tenant, product-scoped, via a shared
SQLite database). Setting both `ODYSSEY_COLLECTOR_API_KEY` and
`ODYSSEY_DB_URI` raises at startup — pick one. Neither is required for
the server to start; `ODYSSEY_DB_URI`/`--db-uri` is required specifically
to run the product-management CLI flags below.

`ODYSSEY_DB_URI`/`--db-uri` must be a `sqlite:///` URI, not a bare
filesystem path — three slashes for a relative path
(`sqlite:///./odyssey.sqlite3`) or four slashes for an absolute path
(`sqlite:////var/lib/odyssey/odyssey.db`); a bare path raises `ValueError`
and the collector will crash-loop under systemd.

### Product/tenant management

Product roster and authentication (when using `--db-uri` mode) are managed through the SQLite database pointed to by `ODYSSEY_DB_URI`. The database is initialized automatically on first use if it doesn't exist. Use the CLI commands to create, list, revoke, and rotate products:

**Important:** `ODYSSEY_DB_URI` must point to the same SQLite file in both `services/collector` **and** `services/api`. Both services read from this shared database for product scoping and authentication caching. Mismatches will cause authentication failures.

Create a new product with a fresh random API key:

```bash
sudo -u odyssey /opt/odyssey/.venv/bin/odyssey-collector \
  --db-uri sqlite:////var/lib/odyssey/odyssey.db --create-product \
  --product-slug acme --product-name "Acme Corp"
```

This prints the generated `api_key` once — save it now, as it cannot be retrieved later (only hashes are stored in the database).

List all products:

```bash
sudo -u odyssey /opt/odyssey/.venv/bin/odyssey-collector \
  --db-uri sqlite:////var/lib/odyssey/odyssey.db --list-products
```

Add a new product to an already-running deployment:

```bash
sudo -u odyssey /opt/odyssey/.venv/bin/odyssey-collector \
  --db-uri sqlite:////var/lib/odyssey/odyssey.db --create-product \
  --product-slug globex --product-name "Globex Inc"
```

The collector reads products and authentication state at request time (cached per `--auth-cache-ttl-seconds`), so new products are live immediately — no restart needed.

Revoke a product's access (prevent it from authenticating):

```bash
sudo -u odyssey /opt/odyssey/.venv/bin/odyssey-collector \
  --db-uri sqlite:////var/lib/odyssey/odyssey.db --revoke-product acme
```

Rotate a product's API key (invalidate the old key, generate a new one):

```bash
sudo -u odyssey /opt/odyssey/.venv/bin/odyssey-collector \
  --db-uri sqlite:////var/lib/odyssey/odyssey.db --rotate-product acme
```

**Upgrading an existing deployment that used `--products-file`/`products.json`:**
that flag and `ODYSSEY_COLLECTOR_PRODUCTS_FILE` no longer exist — there
is no dual-mode fallback. Order matters:

1. Stop the collector (existing `products.json` tenants keep failing to
   authenticate at this point, briefly — that's expected).
2. Run the one-time migration into the new shared database, hashing
   every existing product's `api_key` as-is (no key rotation, no
   disruption to already-integrated callers once the collector comes
   back up):
   ```bash
   sudo -u odyssey /opt/odyssey/.venv/bin/odyssey-collector \
     --db-uri sqlite:////var/lib/odyssey/odyssey.db --migrate-products-from-json /path/to/old/products.json
   ```
3. Update the unit file: replace `--products-file`/`ODYSSEY_COLLECTOR_PRODUCTS_FILE`
   with `--db-uri`/`ODYSSEY_DB_URI` (same value used in step 2).
4. Set the identical `ODYSSEY_DB_URI` on the `odyssey-api` unit too (see
   below) — both services must point at the same file.
5. Restart both services.

The database is initialized automatically on first use if it doesn't
exist — no separate `--init-products-file`-style bootstrap step. It
also now holds the only copy of every product's key hash: **back it up**
(`sqlite3 /var/lib/odyssey/odyssey.db ".backup /path/to/backup.db"`) as
part of your regular backup rotation. A corrupt or unreadable file makes
both services refuse to start rather than ever auto-deleting it — restore
from backup, don't try to recreate it from scratch (that reissues every
product's key).

All other collector CLI flags (`--host`/`--port`/`--data-dir`/`--timezone`) have `ODYSSEY_COLLECTOR_*` env equivalents — see `services/collector/README.md`'s config table. Set them as `Environment=` lines (or `EnvironmentFile=/etc/odyssey/collector.env` for a real deployment, kept out of git).

## `services/api` — FastAPI/ASGI, two supported ways to run it

### Option A — `uvicorn --workers N`, no extra dependency

`uvicorn>=0.30` (already a base dependency of `services/api`) has its own
multi-process worker manager — the lowest-friction production option
when you don't already have gunicorn-based infra:

```bash
/opt/odyssey/.venv/bin/uvicorn odyssey_api.main:app \
  --app-dir /opt/odyssey/services/api/src \
  --host 127.0.0.1 --port 8000 --workers 4
```

`/etc/systemd/system/odyssey-api.service`:

```ini
[Unit]
Description=odyssey-api (read API)
After=network.target

[Service]
Type=simple
User=odyssey
Group=odyssey
WorkingDirectory=/opt/odyssey/services/api
Environment=ODYSSEY_API_HOST=127.0.0.1
Environment=ODYSSEY_API_PORT=8000
Environment=ODYSSEY_API_JOURNEYS_DIR=/var/lib/odyssey/collector-data
Environment=ODYSSEY_API_DATASETS_REGISTRY=/opt/odyssey/data_preparation/datasets/registry.yaml
Environment=ODYSSEY_API_MODELS_REGISTRY=/opt/odyssey/training/models/registry.yaml
Environment=ODYSSEY_API_EVAL_REGISTRY=/opt/odyssey/evaluation/datasets/registry.yaml
Environment=ODYSSEY_API_EVAL_REPORTS_DIR=/opt/odyssey/evaluation/reports
Environment=ODYSSEY_API_EXPORTS_DIR=/var/lib/odyssey/exports
Environment=ODYSSEY_DB_URI=sqlite:////var/lib/odyssey/odyssey.db
# Environment=ODYSSEY_API_AUTH_KEY=change-me
ExecStart=/opt/odyssey/.venv/bin/uvicorn odyssey_api.main:app \
  --app-dir /opt/odyssey/services/api/src \
  --host %E{ODYSSEY_API_HOST} --port %E{ODYSSEY_API_PORT} --workers 4
Restart=on-failure
RestartSec=2
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

(`%E{VAR}` needs systemd ≥ 246; on an older systemd, hardcode
`--host`/`--port` instead of interpolating the `Environment=` lines.)

`ODYSSEY_API_AUTH_KEY` is commented out above because auth is opt-in
and off by default — uncomment it and set a real value to require a
bearer token on every route except `/health`. **If you set it here,
you must set the identical value on the `odyssey-web` unit below** —
`services/api` and `apps/web` have to agree on the key or the
dashboard will silently 401 every page.

**`ODYSSEY_DB_URI` here must be the exact same value as the
`odyssey-collector` unit's** — both services read/write one shared
SQLite file (`services/api`'s read-only index; `services/collector`'s
`products` table, when running in `--db-uri`/product-scoped mode). A
mismatch means `services/api` builds its own separate, empty index
against a file `services/collector` never writes to. With `--workers
4` above, each uvicorn worker process runs its own independent
background indexer thread against that same file — harmless (SQLite's
WAL mode serializes the writes) but redundant; not worth tuning down
unless indexing shows up as measurable overhead.

### Option B — gunicorn, `uvicorn.workers.UvicornWorker`

For operators standardised on gunicorn's process manager (graceful
worker reload, `SIGHUP` reload, `--max-requests` recycling). Needs
`uv sync --all-packages --extra prod` (adds `gunicorn` — it is **not** a
base dependency of `services/api`, see `services/api/pyproject.toml`).

```bash
cd /opt/odyssey/services/api
/opt/odyssey/.venv/bin/gunicorn -k uvicorn.workers.UvicornWorker \
  --chdir src --bind 127.0.0.1:8000 --workers 4 \
  odyssey_api.main:app
```

Swap the `ExecStart=` line in the systemd unit above for:

```ini
ExecStart=/opt/odyssey/.venv/bin/gunicorn -k uvicorn.workers.UvicornWorker \
  --chdir /opt/odyssey/services/api/src --bind 127.0.0.1:8000 --workers 4 \
  odyssey_api.main:app
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now odyssey-api
systemctl status odyssey-api
journalctl -u odyssey-api -f
```

## `apps/web` — Next.js dashboard

Needs `services/api` reachable first (`ODYSSEY_API_BASE_URL`). Uses
Node/pnpm, not the Python `.venv` the two services above share — build
once, run the built output with `next start` (verified: `pnpm build`
succeeds, `next start` serves a real `200` on the built output).

```bash
cd /opt/odyssey
corepack enable
pnpm install --frozen-lockfile     # root pnpm workspace: apps/web + sdk/javascript
pnpm --filter @odyssey/web build
```

`/etc/systemd/system/odyssey-web.service`:

```ini
[Unit]
Description=odyssey-web (dashboard)
After=network.target odyssey-api.service

[Service]
Type=simple
User=odyssey
Group=odyssey
WorkingDirectory=/opt/odyssey/apps/web
Environment=NODE_ENV=production
Environment=ODYSSEY_API_BASE_URL=http://127.0.0.1:8000
# Environment=ODYSSEY_API_AUTH_KEY=change-me
ExecStart=/opt/odyssey/apps/web/node_modules/.bin/next start -p 3000
Restart=on-failure
RestartSec=2
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now odyssey-web
systemctl status odyssey-web
journalctl -u odyssey-web -f
```

A code change means rebuilding before restarting — `next start` serves
whatever `pnpm --filter @odyssey/web build`'s last run produced, it does
not rebuild on its own:

```bash
cd /opt/odyssey && git pull && pnpm --filter @odyssey/web build
sudo systemctl restart odyssey-web
```

## After deploying: regenerate the contract if `services/api` changed

If this deploy includes a `services/api` route/schema change, run
`./scripts/codegen.sh` (or let `codegen-drift.yml` catch it in CI first)
before shipping — `services/api/openapi.json` is what both SDKs and
`apps/web` are generated against. See
[`../data-contracts.md`](../data-contracts.md).

## What this runbook does not cover

Object-store-backed deployment (S3/MinIO for `services/collector`'s
storage, a real database for `services/api`) is out of scope here — see
each member's own README for what's actually built vs. deferred, and
[`../COMPONENTS.md`](../COMPONENTS.md) for the full list of deliberate
scope cuts. `infra/{docker,k8s,terraform}` has no concrete target yet —
this runbook is the "run a real process on a real box" version, not a
container/orchestration one. `apps/web` running behind a reverse proxy
(nginx/Caddy, TLS termination) also isn't covered — `next start` alone
is HTTP-only on the port given.
