# odyssey — next-up checklist

Verified against code on 2026-08-27 (`uv run pytest tests -q` → 468 passed, 1 skipped).
Ordered by dependency, not by section number — do these top to bottom.

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
- [ ] **3.3** `data_preparation/normalization` — thin wrapper over `fold()` +
      `builders/messages.py`, which already exist and are fully tested. Confirmed:
      `data_preparation/` has zero code, nine `.gitkeep`s, no `pyproject.toml`.

## 5. Round out collection
- [ ] **0′.1** OpenAI drop-in client + patch — `messages_from_openai_chat` already
      parses the format; only the wrapper (mirroring `integrations/anthropic.py`)
      is missing. Confirmed: `integrations/` has only `anthropic.py` and `livekit.py`.
- [ ] **9.3** `cli/` real entrypoint (ADR 0003) — plugin-dispatched `odyssey` console
      script owned by `cli/`, core's `argparse` parser demoted to a registered plugin.
      Confirmed: `cli/` is three empty `.gitkeep` dirs; core still owns the console script.
- [ ] **9.9** ADR for the capture layer — the design in WORKING.md §1 (single-writer
      contract, ambient context, never-raise boundary) has no ADR, unlike 0001–0003.

## Blocking, separate from the roadmap
- [ ] **9.4** `NOTICE` copyright holder unresolved — blocks public release regardless
      of feature work. `packages/odyssey-core/NOTICE` exists; holder line needs checking.
- [ ] **4.3** Define `curated_watermark` — referenced in the corpus-version formula
      (`sha(recipe_hash + curated_watermark)`) throughout docs/README but not defined
      anywhere in code. Needed before 4.5 (corpus version function) can start.
      Confirmed: zero code hits, docs-only.

## Untouched, downstream of the above (do not start yet)
`training/`, `models/`, `evaluation/`, `services/api`, `apps/web`, `sdk/python`,
`sdk/javascript`, `datasets/` — all scaffolding, all wait on §2–3 above landing first.
