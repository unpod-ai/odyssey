# odyssey — session handoff

## Session summary (2026-08-28)

Closed all 8 of the "smaller, still open" items the 2026-08-27 handoff listed,
plus 5.7/5.8 (`configs/{sft,dpo,grpo}` and `experiments/<exp_id>.yaml`, done
earlier the same session). One of the 8 was a deliberate breaking change,
approved explicitly rather than deferred:

- **0′.4 voice events — real `SCHEMA_VERSION` major bump, 1.1 → 2.0.** A new
  `"voice"` `EventKind`, `VoiceEvent` dataclass, `JourneyEvent.voice`,
  `FoldResult.voice_events` (kept out of the SFT/DPO export path — no
  `trainable` notion). `integrations/livekit.py` now emits real
  `stt_transcript`/`barge_in` events from data it already computed. Golden
  fixture regenerated at 2.0 with a `voice` event; `test_contract.py` and
  `test_livekit.py` updated for the new event mixed into the seq space. **No
  migration tool** — a schema-1.x shard on disk no longer parses; this is the
  documented, one-way consequence, not a bug.
- **0.10 LangChain** — `integrations/langchain.py`, `OdysseyCallbackHandler()`
  factory (lazy `langchain_core` import, `odyssey[langchain]` extra). One flat
  journey per top-level `run_id`, following `livekit.py`'s explicit-context
  pattern rather than `_base.py`'s wrapped-client one, since LangChain's
  callback API is `run_id`/`parent_run_id`-tree-shaped. **0.11 OTel bridge
  deferred, documented only** — no concrete consuming backend named.
- **0′.6 sampling** — `ODYSSEY_SAMPLE_RATE`/`Config.sample_rate`, one coin-flip
  per journey at open (inherited by nested joins via `JourneyContext.state`),
  dropped before the spool is touched.
- **1.7 wire batching/compression/backpressure** — `HttpSink` gzips by
  default; a 429's `Retry-After` sets a client-side backoff window. Collector
  decompresses. Cross-journey batching explicitly not attempted (documented
  scope cut — would need a `drain()` redesign).
- **1.10 object-store landing** — `collect_from_object_store()` (S3-compatible
  via `boto3`, `odyssey-dataprep[s3]` extra), wired into `odyssey data collect
  --bucket`.
- **1.12/2.14 retention/TTL** — `spool.gc()` + `odyssey spool prune` CLI;
  collector's `prune.py` + `python -m odyssey_collector.prune`. Both
  operator-invoked, no auto-GC timer.
- **2.15 content-level PII scrub** — `odyssey.pii` (regex `scan_pii`/
  `redact_pii`, Luhn-checked credit cards), wired into `data_preparation`'s
  `clean_dir`/`validate_dir` as opt-in (`--pii-rules`).
- **9.10 pyrefly** — the one real error (`livekit.py`, a `getattr`+`callable`
  narrowing gap) fixed with a documented suppression comment. The 157
  `tests/` narrowing gaps are unchanged — a scope decision, not attempted.

Full workspace `task test` green after the schema bump (737 tests across
core/collector/dataprep/cli/training). See `docs/WORKING.md` for the
per-item table updates.

## Session summary (2026-08-27)

Started from a codebase where recording worked locally but nothing shipped
data anywhere, no training file existed, and half the planned workspace
members were still `.gitkeep`s. Ended with a real, tested, end-to-end path:
an agent call gets recorded → shipped over the network → landed on a durable
server, date-partitioned → turned into an actual SFT or DPO training file —
all reachable through one real `odyssey` console script. Nine feature/infra
commits plus three docs-only commits, all pushed to `main`. Full detail per
item is in the checklist below; this is the two-minute version:

- **CI** on all four workspace members (`ci-core`/`ci-collector`/
  `ci-dataprep`/`ci-cli.yml`) — 468 tests that enforced nothing now do.
- **`HttpSink`** (stdlib `urllib`, no new dependency) + **`services/collector`**
  (new member, stdlib `http.server`) — the local-only spool now has a real
  network destination, verified with a live SDK → spool → HTTP → collector →
  readable-file round trip, not just unit tests.
- **`odyssey sft` / `odyssey dpo`** — the actual training-file writers. The
  DPO pair extractor's first design (prefix-hash grouping) was wrong and the
  test suite caught it; the fix (walk `journey.steps` in order) is documented
  in the module itself so it isn't silently reintroduced.
- **Date-partitioned storage**, timezone-configurable (`ODYSSEY_TIMEZONE` /
  `ODYSSEY_COLLECTOR_TIMEZONE`, UTC default) — both the local spool and the
  collector, so neither a shard nor a stored file grows unbounded forever.
- **`data_preparation/normalization`** — first real code in that member. Found
  and fixed a genuine bug while building it (BYOD-parsed journeys had every
  message stuck `not_trainable`, including assistant replies) by inspecting a
  live smoke test's output before writing the regression test for it.
- **OpenAI drop-in client** (+ every OpenAI-*compatible* provider for free —
  same SDK, different `base_url`) — verified against the real installed
  `openai` package, not just a fake.
- **`cli/`**, the real `odyssey` console script (ADR 0003) — lazy plugin
  discovery via entry points, verified `odyssey --help` never imports a
  member's own code. Hit and documented two real, non-obvious typer 0.27
  bugs along the way (see `cli/src/odyssey_cli/registry.py`'s docstring).
- **ADR 0004** (the capture layer's design, never written down before) and
  **`openspec/changes/add-journey-schema/design.md`** (cited by code since
  the original port, never actually committed until now) — including a real
  definition for `curated_watermark`, the one piece of the corpus-versioning
  formula that had never been specified anywhere.

**What's still open, deliberately not started:** `NOTICE`'s copyright-holder
question (item 9.4) — researched this session (PyPI metadata is genuinely
empty; there's a lead, parked, not written down here — see git history
around the "picked up later" commit if you need it) but it needs a human to
actually reach out, not more engineering.

## Start here next session

Since the summary above was written, `data_preparation`'s entire Step 3
(all seven stages) plus Step 4's `datasets/` layer shipped:

- **3.9 → 4.4 → 4.5** — `recipes/` (`Recipe`/`recipe_hash`) and
  `versioning.py` (`compute_curated_watermark`/`corpus_version`), via
  `odyssey data recipe-hash` / `odyssey data corpus-version`.
- **4.6 → 4.7 → 4.8** — `datasets.py` (manifest/registry/card writers), via
  `odyssey data build-corpus` / `odyssey data card`. `next_version` doubles
  as `curated_watermark.seq` — one counter, not two to keep in sync.
- **3.1 collection** — `collect_from_spool`/`collect_from_collector`
  reassemble rotated or date-partitioned shards into one flat `*.jsonl` per
  journey (grouped by each event's own `journey_id`, not filename — the
  collector's filename stem is not guaranteed reversible).
- **3.2 cleaning** — dedupe by `content_hash`, dead-turn drop (splices the
  dropped delta out of every later step's cumulative history — a naive
  per-message filter would have corrupted the prefix invariant), NFC +
  control-char encoding repair. Content-level PII scrub intentionally not
  here — still needs item 2.15, which doesn't exist.
- **3.4 annotation** — a local-JSONL human-review queue (`build_queue`) and
  decision-apply (`apply_reviews`, reuses `build_reward_from_scalar`).
- **3.5 augmentation** — `perturb_tool_calls`, deterministic synthetic
  negatives via a dropped required argument. Paraphrase/general synthetic
  negatives need an LLM and are deliberately not implemented — flagged 🟡,
  not faked.
- **3.6 validation** — schema/PII-redaction/leakage/drift checks; `odyssey
  data validate` exits 3 on breach (ADR 0003's lineage-violation code).
  PII check reuses `odyssey.spool._is_secret`'s exact matching rule.
- **3.7 splitting** — groups by `trace_id` (session), assigns via a
  deterministic hash, never `random`. Ships the test 3.7 explicitly
  demanded: same group key never lands in two splits.
- **3.8 flows** — `run_recipe`, a stdlib sequencer (deliberately not
  Prefect — no scheduling/retry/UI need to justify the dependency) chaining
  `collection → normalization → cleaning → validation → splitting`.
  `annotation`/`augmentation` don't fit the uniform dir-in/dir-out contract
  and are called directly, not auto-sequenced.
- **9.5 / 9.7 / 9.8** — stale `src/odyssey/build/` path fixed;
  `.pre-commit-config.yaml`/`CHANGELOG.md`/`SECURITY.md`/`CODEOWNERS`
  written; `packages/odyssey-core/docs/README.md` written, closing both
  contract tests that were previously no-ops.
- **9.10 re-verified, not fixed** — opting `tests/` into pyrefly surfaces
  158 errors today (was 77, stale), one real, 157 narrowing gaps in test
  files. `task types`/CI unaffected either way. Left open — a scope
  decision (~150+ touch points), not a bug fix.

70 tests added this session across `data_preparation` (7 → 77) — every
new stage verified with a real end-to-end `odyssey data <cmd>` run, not
just unit-tested: `collect → normalize → clean → validate → split` and
`queue → apply-reviews`, `augment`, all exercised against real files on
disk with real exit codes checked (including `validate`'s exit 3).

**Update — 5.6 (soup-cli adapter) shipped since the above was written:**

New `training/` workspace member (`odyssey-training`), `soup-cli>=0.73,<1`
as a real dependency (light install — no `[train]` extra, no torch in this
member). Researched Unsloth first (it isn't a separate thing to integrate —
it's one of soup-cli's own `backend` choices, alongside `transformers`/
`mlx`, nothing extra to build), then verified the real, installed soup-cli
0.73.3 source (`soup_cli/config/schema.py`, `soup_cli/data/formats.py`)
rather than trusting scraped docs, which turned out to matter: the docs
implied DPO wants flat strings; the actual code (`_convert_dpo`) accepts
conversational message lists straight through to `trl.DPOTrainer`.

- `training/src/odyssey_training/soup_adapter.py` — `write_sft_config`
  (`odyssey sft`'s `{"messages": [...]}` is already soup-cli's `chatml`
  format verbatim, zero translation), `translate_dpo_shard` +
  `write_dpo_config` (`odyssey dpo`'s `chosen`/`rejected` are a single
  message; soup-cli/TRL want a message *list* — the one real gap, now
  closed). Every config is validated against the real, installed
  `soup_cli.config.schema.SoupConfig` before being written.
- `odyssey train sft-config` / `odyssey train dpo-config`, verified
  end-to-end against a real spool → `odyssey sft`/`odyssey dpo` → generated
  `soup.yaml`, then round-tripped through soup-cli's own real
  `load_config()` and `soup_cli.data.formats._convert_chatml`/`_convert_dpo`
  — not just our own schema import.
- 7 new tests in `training/tests/`. `ci-training.yml` added; `ci-cli.yml`
  now also triggers on `training/**` (a new dependency there changes what
  `odyssey doctor` discovers and how long cold `--help` takes).
- **Side effect worth watching**: cold `--help` went from 172ms to 346ms
  once soup-cli's own dependencies (httpx, pydantic, huggingface-hub) landed
  in the shared venv. Still comfortably under the 400ms budget (see 9.3),
  but the margin is real now, not enormous — a good reason to be
  deliberate about which future members get a comparably heavy light-install
  dependency.

**What's next, in dependency order:**

1. **9.4** — `NOTICE` copyright holder. The only remaining hard blocker
   (public distribution), needs a human, not more engineering. **LlamaIndex
   hooks (0.10) are intentionally bundled with this item** — picked up
   together in a later pass, not attempted now.
2. **`services/api`** (8.1–8.3) — bigger scope (`odyssey-schemas` +
   FastAPI + OpenAPI), the next major unbuilt piece per `docs/STRUCTURE.md`.
   5.7/5.8 are done (this session), so Step 5 is closed except 5.9
   (checkpoint → object store).

**Smaller, closed this session (2026-08-28):** 0.10 (LangChain), 0′.4 (voice
events — breaking `SCHEMA_VERSION` 1.1 → 2.0), 0′.6 (sampling), 1.7
(batching/compression/backpressure), 1.10 (object-store landing), 1.12/2.14
(retention/TTL), 2.15 (content-level PII scrub), 9.10 (the one real pyrefly
error, suppressed).

**Also closed later the same session:** 0′.5 (async streaming for Anthropic),
1.9 (server-side idempotency in `services/collector`), 1.11 (removed dead
`TelemetryEvent` code), 0.9 (Gemini drop-in client), 0′.2 (LangGraph
compatibility — no additional code, only verification it dispatches the same
callback tree LangChain already does), 1.6 (project scoping — a `projects`
roster of `{slug, name, api_key}` in `services/collector`, structural storage
isolation per project, `GET /projects`), 0.11/0′.3 (OTel bridge —
`integrations/otel.py`'s `OdysseySpanProcessor()`, one journey per trace,
`gen_ai.*` content only — a documented scope cut against other
instrumentation vocabularies, not silent data loss), 1.7 (connection reuse
across `send()`/`send_batch()` calls via HTTP/1.1 keep-alive, **plus**
cross-journey *payload* batching itself — `HttpSink.send_batch()` /
`POST /batch/events`, opt-in via `batch_size=N`, every journey's outcome
independent so the earlier "needs a redesign of `drain()`'s per-journey
semantics" concern is resolved rather than avoided), 3.5
(`paraphrase_journey`/`generate_synthetic_negative`, optional
`odyssey-dataprep[llm]` extra, both opt-in and off by default — the
synthetic-negative chain's `superseded`-then-`trainable` step order was
verified against `odyssey.dpo.dpo_pairs`'s real ordering rule), 9.10
(`tests/` now permanently in `pyrefly`'s `project-includes`; 200 errors
surfaced, not the ~157 estimated — 4 were real `src/` type bugs, fixed
properly; the other 196 were narrowing gaps, fixed per call site).

**Still open:** LlamaIndex hooks (a genuinely different, non-LangChain-compatible
instrumentation API — **deliberately deferred, bundled with item 9.4**, not
started here).

---

# Detailed log — what shipped, item by item

Verified against code on 2026-08-27 (`task test` → core 553 passed/1 skipped,
collector 17 passed, dataprep 77 passed, cli 16 passed; all four CI
workflows green on `main`, including `ci-cli`'s `doctor` cold-start check
after its 200ms→400ms/best-of-3 fix).
Ordered by dependency, not by section number.

## 1. Lock in what already works
- [x] **9.2** `.github/workflows/ci-core.yml` — path-filtered to `packages/odyssey-core/**`,
      runs isort/black (check-only) + flake8 + pyrefly + full test suite + golden-fixture
      staleness check on push/PR to `main`. Fixed two pre-existing black-version-drift
      files (`client.py`, `test_livekit.py`) so CI starts green, not red.

## 2. Make "one destination" real (the actual critical path)
- [x] **1.5** `HttpSink` in `packages/odyssey-core/src/odyssey/sinks.py` — stdlib
      `urllib` only (core stays `dependencies = []`). POSTs the same JSONL bytes
      a shard on disk would hold to `{endpoint}/journeys/{journey_id}/events`.
      Raises `HttpSinkError` on any non-2xx or transport failure, which `drain()`
      already treats as retryable — no changes needed to `Spool.push()` /
      `IntervalDrainer` / the CLI. 18 new tests against a real local
      `http.server`, exported as `odyssey.HttpSink`.
- [x] **1.6** Auth (client half) — `HttpSink(api_key=...)` / `ODYSSEY_API_KEY` env
      fallback sends a `Bearer` token. **Still open:** project scoping, which
      needs a server to scope against (see 1.8).
- [x] **1.8** `services/collector` — new workspace member, stdlib `http.server`
      (not FastAPI — deliberately deferred to `services/api`, see its README).
      Receives exactly what `HttpSink` posts, round-trips through
      `odyssey.jsonl.read_events`/`write_events` so there's one codec, not two,
      writes `<journey_id>.jsonl` byte-identical to `FileSink`'s shape. Optional
      `Authorization: Bearer` gate, `GET /health`. 10 tests (dogfooding
      `odyssey.HttpSink` as the client) + a live manual smoke test: SDK record
      → spool → HttpSink → collector → readable file, verified end to end.
      CI added (`ci-collector.yml`). **Still open:** project scoping
      (multi-tenant auth beyond one shared key) and object-store backing
      (still local disk) — noted in the README's "Not done here" section.

## 3. Ship the actual training artifact (the payoff)
- [x] **5.4b** SFT export writer — `sft.py` + `odyssey sft`. One JSON line
      (`{"messages": [...]}`) per step whose final turn is `trainable`, one
      combined `.jsonl` shard (not one-file-per-conversation like Trajectory
      JSON — that's what an SFT trainer actually wants to point at). Gated on
      `result.trainable` (== complete), same as every other exporter. 13 tests.
- [x] **5.5** DPO pair extractor — `dpo.py` + `odyssey dpo`. Walks
      `journey.steps` in order (not prefix-hash grouping — a first attempt at
      that broke, because a regenerated candidate's own step carries the
      earlier rejection in its cumulative history, so literal prefixes never
      match across candidates). A run of `superseded` steps immediately
      followed by one `trainable` step is a decision point; every rejected
      candidate pairs against the winner. Verified against the golden
      fixture's real regenerated→user_edit→thumbs_up chain (2 pairs, matching
      `test_contract.py`'s independently-asserted status labels) plus a live
      CLI smoke test. 15 tests. **KTO/ORPO not done** — different (unpaired)
      data shape, noted explicitly in the module docstring.
- Refactored `export.py`: extracted `_gather_from_dir`/`_gather_from_spool`
  so all three exporters (Trajectory JSON, SFT, DPO) share one "gather
  FoldResults from a directory or a spool" implementation instead of three.

## 4. Cheapest real pipeline win
- [x] **3.3** `data_preparation/normalization` — new workspace member
      (`odyssey-dataprep`). `normalize_odyssey_dir` (thin wrapper over
      `odyssey.export.export_dir`) and `normalize_byod_dir` (dispatches to
      `builders/messages` parsers by format name, then
      `build_journey_from_messages`). Found and fixed a real gap while
      building it: BYOD-built journeys had every `trainable_status` stuck at
      the dataclass default (`not_trainable`), including assistant replies,
      since `build_journey_from_messages` runs no `fold()`. Fixed by reusing
      `fold.derive_trainable_status` directly (empty signal list) — no new
      logic, same rule a signal-less odyssey-recorded journey gets. 12
      tests + a live smoke test (verified the fix visually: assistant turn
      correctly labelled `trainable` before writing the regression test).
      CI added (`ci-dataprep.yml`). Sibling stages (`collection`,
      `cleaning`, `annotation`, `augmentation`, `validation`, `splitting`,
      `flows`, `recipes`) are untouched, still `.gitkeep` scaffolding.

## 5. Round out collection
- [x] **0′.1** OpenAI drop-in client + patch — `integrations/openai.py` +
      `integrations/_openai_base.py`, mirroring `integrations/anthropic.py`.
      Simpler in one respect: OpenAI's system prompt is `messages[0]`, not a
      separate kwarg, so the existing "record only the unrecorded tail" logic
      already covers it with no special case. `messages_from_openai_chat`
      raises on a malformed entry (right for a batch import, wrong on an
      auto-capture path) — `_safe_openai_messages` degrades to a best-effort
      message instead of losing the turn. **OpenAI-compatible providers**
      (Groq, Together, local vLLM/Ollama, ...) work automatically: they speak
      the same SDK with a different `base_url`, and the wrapper forwards
      constructor kwargs untouched — no per-provider code needed. 24 tests
      against a fake SDK plus a live smoke test against the real installed
      `openai` package (request build + response parse, not just the fake),
      and `instrument()`'s default patch target verified against the real
      SDK's internal module layout. `stream=True` passes through unrecorded
      (open item, same as Anthropic's async streaming gap).
- [x] **9.3** `cli/` real entrypoint (ADR 0003) — new `typer`+`rich` workspace
      member. Lazy plugin registry (`odyssey.commands` entry-point group):
      `list_commands` reads entry-point names via `importlib.metadata` (no
      import), `get_command` only calls `entry_point.load()` for the one
      group actually dispatched — verified `odyssey --help` never imports
      `odyssey.cli`. All 7 `odyssey-core` subcommands mounted under
      `odyssey spool`, `odyssey data normalize` from `odyssey-dataprep`,
      deprecated `odyssey push`/`odyssey status` top-level aliases (warn to
      stderr), `odyssey doctor` (cold `--help` measured at 172ms, under the
      200ms budget). Core drops `[project.scripts]`, registers
      `spool = "odyssey.cli:register"` instead; `python -m odyssey.cli`
      unaffected. Hit two real typer 0.27 quirks while building this (both
      documented in `registry.py`'s docstring): a raw `click.Group` root
      breaks nested `--help` once mixed with typer-built subcommands (typer
      no longer shares one exception hierarchy with installed `click`), and
      a lazily-returned command has no `.name` unless set explicitly. 16
      tests, `ci-cli.yml` added.
- [x] **9.9** ADR for the capture layer — `docs/adr/0004-capture-layer.md`.
      Covers the event-sourced core (JourneyEvent as the sole wire unit,
      Step[] as a read-time projection), ambient ContextVar-based journey
      tracking, the single-writer-per-journey contract with detection
      (writer_id, fold's writer_conflict, CLI exit 3), the never-raise
      boundary, and local-only recording (the spool as retry queue). Also
      names its own deliberate exception to ADR 0001 rule 1 (packages/ = no
      side effects) explicitly, which is what item 9.9 actually asked for.
      Fixed two other stale "no ADR" / "no CI" references elsewhere in
      WORKING.md that had drifted since those items shipped.

## Blocking, separate from the roadmap
- [ ] **9.4** `NOTICE` copyright holder unresolved — blocks public release regardless
      of feature work. `packages/odyssey-core/NOTICE` exists; holder line needs checking.
- [x] **4.3** Define `curated_watermark` — written up in
      `openspec/changes/add-journey-schema/design.md` Decision 9 (also closes
      item 9.6, the previously-cited-but-absent design.md itself). `{seq, hash}`:
      `hash = content_hash` over the sorted `(journey_id, journey_content_hash)`
      set — the correctness guarantee (changes iff the curated set or its
      content actually changes; a timestamp or bare count both fail that,
      explicitly ruled out) — `seq` a per-corpus incrementing run counter for
      the human-facing story. Reuses `odyssey.hashing.content_hash` directly,
      no new hashing primitive. **Definition only — not yet implemented in
      code.** That's item 4.5 (the corpus version function itself), still open
      and now unblocked.

## Untouched, downstream of the above (do not start yet)
`training/`, `models/`, `evaluation/`, `services/api`, `apps/web`, `sdk/python`,
`sdk/javascript`, `datasets/` — all scaffolding, all wait on §2–3 above landing first.
