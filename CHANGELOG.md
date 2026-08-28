# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this
project has not yet made a versioned release, so entries accumulate under
`[Unreleased]`. Pre-changelog history lives in `git log`.

## [Unreleased]

### Changed

- **BREAKING**: `SCHEMA_VERSION` bumped `1.1` → `2.0` (item 0′.4). Added a new
  `"voice"` `EventKind` and `VoiceEvent` payload (`voice_kind`, `text`,
  `confidence`, `latency_ms`, `metadata`) for STT/TTS/barge-in/latency
  signals, wired into `integrations/livekit.py`. A schema-1.x reader has no
  branch for `"voice"` and cannot safely ignore an unrecognized kind the way
  a 1.0 reader ignored 1.1's new header keys, so `jsonl.py`'s major-version
  gate now refuses any file declaring a `1.x` (or earlier) schema version. No
  migration tool ships with this change — a schema-1.x `*.jsonl` shard on
  disk simply stops parsing under this reader.
- `HttpSink` (item 1.7) now reuses one `http.client.HTTPConnection` across
  `send()`/`send_batch()` calls instead of opening a fresh connection per
  journey — `services/collector`'s `_Handler.protocol_version = "HTTP/1.1"`
  opts in server-side. Draining N journeys in one process now costs one
  TCP/TLS handshake, not N; a dropped keep-alive connection is retried once
  transparently.
- `packages/odyssey-core`'s `pyrefly` config now permanently checks `tests/`
  as well as `src`/`scripts` (item 9.10). Turning this on surfaced 200
  errors, not the ~157 previously estimated: 4 were real type-safety bugs in
  `src/` (a too-narrow `_guard` callback signature and some loosely-`str`-typed
  fields in `integrations/langchain.py` that should have been the real
  `Role`/`TerminationReason` literals), fixed properly rather than
  suppressed. The remaining 196 were `tests/` narrowing gaps — `Optional[...]`
  fields accessed without narrowing first — fixed per call site with
  `assert x is not None` / `(x or {})[...]` idioms, plus a couple of test
  helper signatures widened to their real `Literal` types.

### Added

- `integrations/langchain.py` — `OdysseyCallbackHandler()` for LangChain
  (optional `odyssey[langchain]` extra), one flat journey per top-level
  `run_id`.
- `odyssey.pii` — regex-based `scan_pii`/`redact_pii` for content-level PII
  (email/phone/credit card with Luhn check/SSN), wired into
  `data_preparation`'s `clean_dir`/`validate_dir` as opt-in.
- Sampling: `ODYSSEY_SAMPLE_RATE` / `Config.sample_rate`, one coin-flip per
  journey at open time.
- `HttpSink` gzip compression (default on) and client-side `Retry-After`
  backoff on HTTP 429; `services/collector` decompresses accordingly.
- `data_preparation`'s `collect_from_object_store()` — S3-compatible raw-layer
  collection (optional `odyssey-dataprep[s3]` extra), wired into
  `odyssey data collect --bucket`.
- `spool.gc()` / `odyssey spool prune` and `services/collector`'s
  `prune.py` / `python -m odyssey_collector.prune` — retention/TTL for
  fully-drained shards and stale date partitions, operator-invoked only.
- `integrations/anthropic.py`: async streaming capture (item 0′.5) —
  `AsyncAnthropic.messages.stream()` now records the assembled final message,
  matching the existing sync `messages.stream()` behavior.
- `services/collector`: server-side idempotency (item 1.9) — `_store()` skips
  any `event_id` already committed to the destination file, so a retried
  `HttpSink` POST no longer double-writes the raw layer.
- `integrations/gemini.py` (item 0.9) — drop-in `Client` for `google-genai`
  (optional `odyssey[gemini]` extra), sync (`client.models`) + async
  (`client.aio.models`) + opt-in `instrument()` patch. New
  `builders.messages.messages_from_gemini` parser for Gemini's
  `Content`/`parts` shape (`function_call`/`function_response` parts,
  `thought` parts → `Message.reasoning`).
- LangGraph compatibility (item 0′.2) — no new code: verified that a
  compiled `StateGraph`'s `invoke()`/`ainvoke()` and every node (including
  `langgraph.prebuilt.ToolNode`) dispatch through the same
  `on_chain_start`/`on_chain_end`/`on_llm_*`/`on_tool_*` callback tree the
  existing `OdysseyCallbackHandler()` already records, against real
  installed `langgraph`/`langchain-core`.
- `services/collector`: project scoping (item 1.6) — a `projects` roster
  (`--keys-file`/`ODYSSEY_COLLECTOR_KEYS_FILE`, JSON `{"projects":
  [{"slug", "name", "api_key"}, ...]}`), mutually exclusive with the
  existing single shared `api_key`. Each project's key writes into its own
  `<data_dir>/<slug>/<date>/` partition — structural isolation, not just an
  access check on shared storage. New `GET /projects` (any registered key)
  lists `{slug, name}` for the roster, never keys.
- `integrations/otel.py` (items 0.11/0′.3) — `OdysseySpanProcessor()`, an
  `opentelemetry.sdk.trace.SpanProcessor` (optional `odyssey[otel]` extra).
  One journey per OTel trace; a span becomes a `Message` only when it
  carries `gen_ai.*` content, checked across the three shapes actually
  emitted in practice (`gen_ai.input.messages`/`.output.messages`
  attributes, `gen_ai.content.prompt`/`.completion` events, legacy
  `gen_ai.prompt`/`.completion` attributes). Other instrumentation
  vocabularies (OpenInference, etc.) are an explicit, documented scope cut.
- `data_preparation`'s `augmentation.paraphrase_journey`/
  `.generate_synthetic_negative` (item 3.5, optional `odyssey-dataprep[llm]`
  extra) — LLM-backed paraphrase and synthetic-negative generation, both
  opt-in and off by default. `paraphrase_journey` rewords only real user
  turns, keeping the assistant's trainable output untouched.
  `generate_synthetic_negative` emits a `superseded`-then-`trainable` step
  chain — the shape `odyssey.dpo.dpo_pairs` looks for. Wired into
  `odyssey data augment --paraphrase N --synthetic-negatives`.
- Cross-journey payload batching (item 1.7) — `HttpSink.send_batch()` posts
  several journeys' events in one `POST /batch/events` request instead of
  one POST per journey; `services/collector` validates and stores each
  journey independently through the same path a single-journey POST uses
  and reports a per-journey `{"ok": true|false, ...}` result. `drain()`
  gains `batch_size` (`Spool.push(..., batch_size=N)`,
  `odyssey.init(drain_batch_size=N)` / `ODYSSEY_DRAIN_BATCH_SIZE`), opt-in
  and defaulting to `1` (today's per-journey behavior, unchanged for a sink
  without `send_batch`). Every journey's watermark still advances or
  retries off *that journey's own* reported outcome, never off other
  journeys in the same batch — resolves the earlier "needs a redesign of
  `drain()`'s per-journey semantics for partial-batch failure" concern
  rather than working around it.

### Removed

- `primitives.TelemetryEvent` (item 1.11) — dead code targeting a
  `push_events()` pipeline and a `POST /api/v1/telemetry/events` backend,
  neither of which exists anywhere in this repo. `Telemetry` (no suffix,
  `JourneyEvent.telemetry`) is unrelated and unaffected.
