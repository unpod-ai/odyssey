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
Environment=ODYSSEY_COLLECTOR_API_KEY=change-me
# Environment=ODYSSEY_COLLECTOR_PRODUCTS_FILE=/etc/odyssey/collector-products.json
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

### Switching to product-scoped mode (`--products-file`)

**Create the roster file before flipping the `Environment=` line** — do
not skip this step. `_load_products_file` (`services/collector/server.py`)
deliberately refuses to start with a missing, empty, or malformed products
file, on purpose: a silently-created empty or placeholder roster would be
functionally identical to "every future POST gets a 401 with no
explanation" — the exact failure mode fail-fast startup exists to
prevent.

`odyssey-collector --init-products-file` bootstraps a real one: one
product, a fresh cryptographically random `api_key` (a genuine secret,
not a placeholder you're expected to remember to replace), refuses to
overwrite a file that already exists.

```bash
sudo install -d -m 0750 -o odyssey -g odyssey /etc/odyssey
sudo -u odyssey /opt/odyssey/.venv/bin/odyssey-collector \
  --init-products-file /etc/odyssey/collector-products.json \
  --product-slug acme --product-name "Acme Corp"
```

This prints the generated `api_key` once — save it now, it's also in the
file in plaintext but won't be echoed back to you again. Then, in the
unit file: comment out `ODYSSEY_COLLECTOR_API_KEY`, uncomment
`ODYSSEY_COLLECTOR_PRODUCTS_FILE=/etc/odyssey/collector-products.json`, and:

```bash
sudo systemctl daemon-reload
sudo systemctl restart odyssey-collector
journalctl -u odyssey-collector -n 20   # confirm it started clean, not a FileNotFoundError
```

**`--init-products-file`/`--product-slug`/`--product-name` have no
`ODYSSEY_COLLECTOR_*` env var equivalent, deliberately.** They're a
one-shot bootstrap action, not persistent server config — giving them an
env var would mean a stray `Environment=` line in the unit file makes
every `Restart=on-failure` restart re-run the bootstrap instead of
serving (and since the file already exists after the first run, it would
just exit 1 and restart-loop forever). Run it once, by hand, separately
from `serve`.

Every other collector CLI flag (`--host`/`--port`/`--data-dir`/
`--api-key`/`--products-file`/`--timezone`) has an `ODYSSEY_COLLECTOR_*`
env equivalent — see `services/collector/README.md`'s config table. Set
them as `Environment=` lines (or `EnvironmentFile=/etc/odyssey/collector.env`
for a real deployment, kept out of git).

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
