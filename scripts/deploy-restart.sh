#!/usr/bin/env bash
# Pulls latest main, rebuilds both the Python venv and the JS workspace,
# then restarts all three systemd-supervised services in dependency
# order: collector and api (independent of each other) first, web last
# since `next build` bakes in a prerender pass that hits the live api.
#
# Run as the deploy user with passwordless sudo for `systemctl` (or run
# the whole script under sudo) — everything else (git, uv, pnpm) runs as
# the checkout's owner, not root.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "==> git pull"
git pull --ff-only

echo "==> uv sync (services/collector, services/api)"
uv sync --all-packages --extra prod

echo "==> pnpm install (workspace)"
pnpm install

echo "==> build @odyssey/sdk"
pnpm --filter @odyssey/sdk build

echo "==> restart collector/api"
# Must happen before the web build below: next build prerenders every
# static route against a live services/api, so the api needs to already
# be running the new code (e.g. new routers) before that prerender pass.
sudo systemctl restart odyssey-collector.service odyssey-api.service

echo "==> build apps/web"
# ODYSSEY_API_BASE_URL must point at a live services/api — next build
# prerenders every static route against it.
set -a
source /var/apps/odyssey/.env
set +a
pnpm --filter @odyssey/web build

echo "==> restart web"
sudo systemctl restart odyssey-web.service

echo "==> status"
systemctl is-active odyssey-collector.service odyssey-api.service odyssey-web.service

echo "deploy-restart.sh: done"
