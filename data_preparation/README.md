# odyssey-dataprep

`data_preparation` stages: raw traces → curated, split, versioned corpus.
Registers `odyssey data ...` (`docs/STRUCTURE.md`'s planned surface — all
of it is built: `collection`, `cleaning`, `normalization`, `annotation`,
`augmentation`, `validation`, `splitting`, `flows`, `recipes`; see
`docs/WORKING.md` Step 3, items 3.1-3.9).

## Stages

- **`collection/`** — `collect_from_spool`/`collect_from_collector`/
  `collect_from_object_store` reassemble rotated (spool), date-partitioned
  (`services/collector`), or S3-key-listed (object store) shards into one
  flat `*.jsonl` per journey, grouped by each event's own `journey_id`.
- **`cleaning/`** — `dedupe_journeys` (by `content_hash`), `drop_dead_turns`
  (splices a dead delta out of every later step's cumulative history),
  `repair_encoding` (NFC + strip C0 controls), `scrub_pii_content`
  (opt-in via `--pii-rules`).
- **`normalization/`** — `normalize_odyssey_dir` / `normalize_byod_dir`.
  Raw traces → canonical `Journey` artifacts, via odyssey-core's own
  `fold()` and BYOD builders (`openai_chat`/`anthropic_messages`/
  `vercel_ai_sdk` formats). No new parsing logic here — a stage wrapper
  over an engine that already existed and was already tested.
- **`annotation/`** — `build_queue` (one JSONL line per journey + preview)
  and `apply_reviews` (a decision's `score` becomes the journey's `Reward`;
  `approved`/`notes` land under `telemetry.data.annotation`). No external
  queue system — a local JSONL file is the queue.
- **`augmentation/`** — `perturb_tool_calls` (deterministic synthetic
  negatives, always on) plus opt-in LLM-backed `paraphrase_journey` /
  `generate_synthetic_negative` (`odyssey-dataprep[llm]` extra, off by
  default — an LLM call per journey is a real cost this stage doesn't
  spend unless asked).
- **`validation/`** — `validate_schema`, `check_pii_redaction` (reuses
  `odyssey.spool._is_secret`'s exact matching rule), `check_leakage`,
  `check_drift`. Exits `3` on breach — the lineage-violation code CI greps
  for (ADR 0003).
- **`splitting/`** — `split_dir` groups by `trace_id` (falls back to the
  journey's own id), assigns via a deterministic hash of the group key,
  never `random` — a group never splits across train/val/test.
- **`flows/`** — `run_recipe`, a stdlib sequencer over
  `collection`/`normalization`/`cleaning`/`validation`/`splitting`
  (uniform dir-in/dir-out contract). Deliberately not Prefect — no
  scheduling/retry/UI need to justify the dependency. `validation` is a
  gate (aborts the run on breach); `splitting` must be last.
  `annotation`/`augmentation` don't fit the uniform contract and are
  called directly, not sequenced.
- **`recipes/*.yaml`** — declarative, order-sensitive stage lists;
  `recipe_hash` is part of `corpus version = sha(recipe_hash + curated_watermark)`.

Also owns the `datasets/` corpus registry: `odyssey data build-corpus` /
`odyssey data card`.

## CLI

```
odyssey data normalize
odyssey data collect
odyssey data clean
odyssey data queue / apply-reviews
odyssey data augment
odyssey data validate
odyssey data split
odyssey data recipe-hash / corpus-version / build-corpus / card
```

## Run it

```bash
cd data_preparation
uv sync --extra dev
uv run pytest tests
```
