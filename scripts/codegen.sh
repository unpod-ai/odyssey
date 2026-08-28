#!/usr/bin/env bash
# Regenerates every codegen'd artifact from services/api's live app, in
# dependency order: the OpenAPI schema first (item 8.3), then the clients
# that read it (item 8.4's sdk/python today; sdk/javascript, item 8.5, not
# built yet — add its regen step here the same commit it lands).
#
# `codegen-drift.yml` runs the `--check`/`check-drift` variants of these
# same two steps in CI — see docs/STRUCTURE.md's "sdk codegen ·
# check-drift (scripts/codegen)" command surface.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

uv run odyssey api openapi --out services/api/openapi.json
uv run odyssey sdk codegen

echo "codegen.sh: done"
