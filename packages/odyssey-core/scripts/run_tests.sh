#!/usr/bin/env bash
# odyssey test runner. Modules:
#   schema      — JourneyEvent validation, the fold, the projection
#   build       — ported message adapters, metrics, reward, cumulative steps
#   jsonl       — versioned JSONL codec: truncation and per-line rejection
#   spool       — local append-only capture, watermark, drain, handle cache
#   sinks       — drain destinations: FileSink, HttpSink over stdlib HTTP
#   context     — ambient journey context and seq allocation
#   project     — auto-detected "project" tag: env, git remote, dirname fallback
#   metrics     — opt-in host telemetry: snapshot fields, background reporter
#   sdk         — init(), journey(), observe(), health(); capture never raises
#   integrations— provider capture: Anthropic + OpenAI + Gemini drop-in/patch, no duplicates
#   langchain   — LangChain (+ LangGraph) callback handler (items 0.10/0'.2)
#   otel        — OpenTelemetry span bridge (items 0.11/0'.3)
#   pii         — content-level PII scan/redact (item 2.15)
#   cli         — the command-line drain trigger and health report
#   sft         — SFT export: one line per trainable turn
#   dpo         — DPO pair extraction: (prompt, chosen, rejected)
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
  sinks)
    uv run pytest tests/test_sinks.py "$@"
    ;;
  context)
    uv run pytest tests/test_context.py "$@"
    ;;
  project)
    uv run pytest tests/test_project.py "$@"
    ;;
  metrics)
    uv run pytest tests/test_metrics.py "$@"
    ;;
  sdk)
    uv run pytest tests/test_sdk.py "$@"
    ;;
  integrations)
    uv run pytest tests/test_integrations.py tests/test_openai_integration.py tests/test_gemini_integration.py "$@"
    ;;
  langchain)
    uv run pytest tests/test_langchain_integration.py "$@"
    ;;
  otel)
    uv run pytest tests/test_otel_integration.py "$@"
    ;;
  pii)
    uv run pytest tests/test_pii.py "$@"
    ;;
  cli)
    uv run pytest tests/test_cli.py "$@"
    ;;
  sft)
    uv run pytest tests/test_sft.py "$@"
    ;;
  dpo)
    uv run pytest tests/test_dpo.py "$@"
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
