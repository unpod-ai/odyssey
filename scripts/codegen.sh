#!/usr/bin/env bash
# Regenerates every codegen'd artifact from services/api's live app, in
# dependency order: the OpenAPI schema first (item 8.3), then both
# clients that read it — sdk/python (8.4) and sdk/javascript (8.5).
#
# `codegen-drift.yml` runs the `--check`/`check-drift` variants of these
# same steps in CI — see docs/STRUCTURE.md's "sdk codegen ·
# check-drift (scripts/codegen)" command surface.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

uv run odyssey api openapi --out services/api/openapi.json
uv run odyssey sdk codegen
corepack pnpm --filter @odyssey/sdk codegen

echo "codegen.sh: done"
