#!/usr/bin/env bash
# odyssey test runner. Modules:
#   schema      — JourneyEvent validation, the fold, the projection
#   build       — ported message adapters, metrics, reward, cumulative steps
#   jsonl       — versioned JSONL codec: truncation and per-line rejection
#   spool       — local append-only capture, watermark, drain, handle cache
#   context     — ambient journey context and seq allocation
#   sdk         — init(), journey(), observe(), health(); capture never raises
#   integrations— provider capture: drop-in client, opt-in patch, no duplicates
#   cli         — the command-line drain trigger and health report
#   contract    — the golden fixture and the no-import-coupling gate
#   all         — everything
#
# Soup ships no test runner and its suite is a release journal (179 of 351 files
# named test_v<release>). odyssey establishes a module map instead; add new
# modules to the case below, not just to tests/.
set -euo pipefail
cd "$(dirname "$0")/.."

MODULE="${1:-all}"; shift || true

case "$MODULE" in
  list)
    grep -E '^#   [a-z]+' "$0" | sed 's/^#   //'
    ;;
  schema)
    uv run pytest tests/test_fold.py "$@"
    ;;
  build)
    uv run pytest tests/builders "$@"
    ;;
  jsonl)
    uv run pytest tests/test_jsonl.py "$@"
    ;;
  spool)
    uv run pytest tests/test_spool.py "$@"
    ;;
  context)
    uv run pytest tests/test_context.py "$@"
    ;;
  sdk)
    uv run pytest tests/test_sdk.py "$@"
    ;;
  integrations)
    uv run pytest tests/test_integrations.py "$@"
    ;;
  cli)
    uv run pytest tests/test_cli.py "$@"
    ;;
  contract)
    uv run pytest tests/test_contract.py "$@"
    ;;
  all)
    uv run pytest tests "$@"
    ;;
  *)
    echo "unknown module: $MODULE" >&2
    echo "run '$0 list' for the module list" >&2
    exit 2
    ;;
esac
