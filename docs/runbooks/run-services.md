# Running `services/collector` and `services/api` in production

Every command below was actually run against this repo before being
written down (`gunicorn -k uvicorn.workers.UvicornWorker ...` against a
real `services/api`, a real `.venv/bin/odyssey-collector`) — not copied
from generic docs.

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
# Environment=ODYSSEY_COLLECTOR_KEYS_FILE=/etc/odyssey/collector-keys.json
ExecStart=/opt/odyssey/.venv/bin/odyssey-collector
Restart=on-failure
RestartSec=2
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/var/lib/odyssey/collector-data

[Install]
WantedBy=multi-user.target
```

```bash
sudo mkdir -p /var/lib/odyssey/collector-data && sudo chown odyssey:odyssey /var/lib/odyssey/collector-data
sudo systemctl daemon-reload
sudo systemctl enable --now odyssey-collector
systemctl status odyssey-collector
journalctl -u odyssey-collector -f
```

### Switching to project-scoped mode (`--keys-file`)

**Create the roster file before flipping the `Environment=` line** — do
not skip this step. `_load_keys_file` (`services/collector/server.py`)
deliberately refuses to start with a missing, empty, or malformed keys
file, on purpose: a silently-created empty or placeholder roster would be
functionally identical to "every future POST gets a 401 with no
explanation" — the exact failure mode fail-fast startup exists to
prevent.

`odyssey-collector --init-keys-file` bootstraps a real one: one project,
a fresh cryptographically random `api_key` (a genuine secret, not a
placeholder you're expected to remember to replace), refuses to
overwrite a file that already exists.

```bash
sudo install -d -m 0750 -o odyssey -g odyssey /etc/odyssey
sudo -u odyssey /opt/odyssey/.venv/bin/odyssey-collector \
  --init-keys-file /etc/odyssey/collector-keys.json \
  --project-slug acme --project-name "Acme Corp"
```

This prints the generated `api_key` once — save it now, it's also in the
file in plaintext but won't be echoed back to you again. Then, in the
unit file: comment out `ODYSSEY_COLLECTOR_API_KEY`, uncomment
`ODYSSEY_COLLECTOR_KEYS_FILE=/etc/odyssey/collector-keys.json`, and:

```bash
sudo systemctl daemon-reload
sudo systemctl restart odyssey-collector
journalctl -u odyssey-collector -n 20   # confirm it started clean, not a FileNotFoundError
```

**`--init-keys-file`/`--project-slug`/`--project-name` have no
`ODYSSEY_COLLECTOR_*` env var equivalent, deliberately.** They're a
one-shot bootstrap action, not persistent server config — giving them an
env var would mean a stray `Environment=` line in the unit file makes
every `Restart=on-failure` restart re-run the bootstrap instead of
serving (and since the file already exists after the first run, it would
just exit 1 and restart-loop forever). Run it once, by hand, separately
from `serve`.

Every other collector CLI flag (`--host`/`--port`/`--data-dir`/
`--api-key`/`--keys-file`/`--timezone`) has an `ODYSSEY_COLLECTOR_*` env
equivalent — see `services/collector/README.md`'s config table. Set them as
`Environment=` lines (or `EnvironmentFile=/etc/odyssey/collector.env` for
a real deployment, kept out of git).

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

## After deploying: regenerate the contract if `services/api` changed

If this deploy includes a `services/api` route/schema change, run
`./scripts/codegen.sh` (or let `codegen-drift.yml` catch it in CI first)
before shipping — `services/api/openapi.json` is what both SDKs and
`apps/web` are generated against. See
[`../data-contracts.md`](../data-contracts.md).

## What this runbook does not cover

`apps/web` (Next.js) and object-store-backed deployment (S3/MinIO for
`services/collector`'s storage, a real database for `services/api`) are
out of scope here — see each member's own README for what's actually
built vs. deferred, and [`../COMPONENTS.md`](../COMPONENTS.md) for the
full list of deliberate scope cuts. `infra/{docker,k8s,terraform}` has no
concrete target yet — this runbook is the "run a real process on a real
box" version, not a container/orchestration one.
