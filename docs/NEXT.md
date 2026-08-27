# odyssey — session handoff

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

`curated_watermark` is now *defined* (`design.md` Decision 9) but not
*implemented*. The natural next unit of work, in order:

1. **3.9** — a minimal `data_preparation/recipes/*.yaml` format: declarative,
   hashable, describing a cleaning/normalization/augmentation pipeline config.
2. **4.4** — `recipe_hash`: hash that recipe with the already-existing
   `odyssey.hashing.content_hash` (reuse, not a new primitive).
3. **4.5** — the corpus version function itself:
   `content_hash({"recipe": recipe_hash, "watermark": curated_watermark})`,
   per `design.md`'s exact spec. This is genuinely unblocked now — the only
   reason it wasn't started this session is running out of session, not a
   design gap.

That's a clean, self-contained unit: define the recipe shape, hash it, wire
it to the already-written `curated_watermark` definition, and the whole
`raw traces → corpus version` half of the lineage chain in `README.md`
becomes real for the first time.

**Other legitimate directions**, if priorities have shifted: keep extending
`data_preparation` (`cleaning`/`annotation`/`validation` are still
`.gitkeep`s), or start `services/api` (bigger scope — `odyssey-schemas` +
FastAPI + OpenAPI, the next major unbuilt piece per `docs/STRUCTURE.md`).

---

# Detailed log — what shipped, item by item

Verified against code on 2026-08-27 (`task test` → core 553 passed/1 skipped,
collector 17 passed, dataprep 13 passed, cli 16 passed).
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
