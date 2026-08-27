# design.md — the journey schema and corpus versioning

Cited by `packages/odyssey-core/src/odyssey/primitives.py`, `fold.py`, and
`pyproject.toml` as "design.md Decision N" since the `trajectory-sdk` port,
but never actually committed — see `docs/WORKING.md` §10 ("Dead code and
stale references") and item 9.6. This file closes that gap.

Decisions are numbered to match the citations already in the code. 1, 4, and
8 are reconstructed here from what the shipped code and its own docstrings
already establish — not new design, just the record those citations were
always pointing at. 9 is new: the `curated_watermark` definition (item 4.3).

## Decision 1 — Events are the wire; `Step[]` is a projection

`JourneyEvent` is the only unit odyssey ever writes to disk or sends over a
network — append-only, ordered by a client-assigned `seq` within
`journey_id`, idempotent on `event_id`. Cumulative `Step[]` (a step holds
the whole conversation up to its own turn) is never stored or transmitted;
`fold()` computes it at read time from the flat event stream, via
`builders.steps.build_cumulative_steps`.

Enforced by `test_no_step_record_is_ever_encoded`
(`packages/odyssey-core/tests/test_contract.py`). See also
`docs/adr/0004-capture-layer.md` Decision 1, which restates this from the
write side.

## Decision 4 — Porting policy: drop dead fields, define undocumented ones

Two situations came up porting the schema from `trajectory-sdk`, and both
get the same treatment: silence in the upstream code is not itself a
specification.

- **A field upstream declared but never assigned, and nothing read.**
  Dropped rather than carried forward as dead weight. `Step.info` and
  `ExecutionMetrics.env_time`/`llm_time` are the two instances
  (`primitives.py`) — odyssey has neither an environment nor per-call timing
  to source `env_time`/`llm_time` from, and nothing upstream ever populated
  `info` either. Re-added only when something can actually populate them.
- **A field upstream declared but never *implemented*.** `Message.
  trainable_status`'s four-state machine had no prior art to port — nothing
  in the donor codebase ever assigned it. Rather than leave it permanently
  at its dataclass default, `fold.derive_trainable_status` is odyssey's own
  definition: a five-rule precedence (structural flags outrank human
  signals, human signals outrank the role default) documented in full in
  `fold.py`'s own docstring.

## Decision 8 — Why a projection, not stored cumulative state

The specific cost argument behind Decision 1: a step holds every message up
to its own turn, so N cumulative steps written to the wire would repeat
O(N²) bytes — a 12-turn call's last step alone already contains all 12
turns' worth of messages, and each earlier step is a strict prefix of it.
Writing and shipping N flat events costs O(N) instead, and `fold()` pays the
O(N) reconstruction cost exactly once, at read time, per consumer — not once
per write. This is also why `odyssey export --last-step` exists: the
Trajectory JSON *artifact* (where cumulative state is allowed to exist,
because it never hits the wire) still pays the same O(N²) cost across all N
steps unless the caller only wants the final, complete one.

## Decision 9 — `curated_watermark`

The corpus-version formula, stated everywhere from `README.md` to
`docs/adr/0002-artifacts-out-of-git.md` but never implemented:

```
corpus version = sha(recipe_hash + curated_watermark)
```

`recipe_hash` (item 4.4, still separately undefined — needs `data_preparation/
recipes/*.yaml` to exist first, item 3.9) answers "processed which way."
`curated_watermark` answers the other half: "built from which data." For the
formula to mean anything, `curated_watermark` must satisfy one hard
constraint — **it changes if and only if the actual set of curated journeys,
or their content, changes.** A timestamp cutoff or a bare count both fail
this: neither reflects a retraction (a journey pulled after a bad-data
report) or a correction, so two corpora with genuinely different content
could silently collide on the same version string. That defeats the reason
a version exists at all.

### Definition

`curated_watermark` is a pair, computed once per curation run over the set
of journeys `data_preparation/annotation` has approved for inclusion:

```python
curated_watermark = {
    "seq": <int>,   # monotonically increasing; one curation run = one seq
    "hash": <hex>,  # content_hash(sorted [(journey_id, journey_content_hash), ...])
}
```

- **`hash`** is what actually protects the version formula's correctness.
  Computed with `odyssey.hashing.content_hash` — already built, already
  used for exactly this shape of fingerprinting
  (`Journey.telemetry.data["content_hash"]`, `hashing.idempotency_key`) —
  over the sorted list of `(journey_id, content_hash(journey))` for every
  journey in the curated set. Sorting makes it order-independent; per-journey
  content hashing (not just the id) means a re-annotated or corrected
  journey changes the watermark even if the *set* of ids is unchanged.
  Reuses `canonicalize`'s existing null-stripping and key-sorting, so the
  hash is stable across re-serialization the same way `content_hash` already
  is for a single `Journey`.
- **`seq`** is the human-facing half — "corpus v12 was curation run 47" — a
  plain incrementing counter, not itself part of the correctness guarantee.
  Lives wherever curation-run state is tracked; the natural home is
  `datasets/manifests/<name>/v<N>.json` (already the tracked-in-git home for
  corpus metadata per `docs/adr/0002-artifacts-out-of-git.md`), not a new
  store.

The version formula becomes:

```python
corpus_version = content_hash({"recipe": recipe_hash, "watermark": curated_watermark})
```

i.e. `sha` in the shorthand notation is this project's existing
`hashing.content_hash`, not a bare string concatenation — consistent with
every other version/idempotency string in the codebase.

### Consequences

- Computing `hash` requires the per-journey `content_hash` to already exist
  for every curated journey — it does, `build_journey_from_messages` and
  `build_journey_from_parsed` both stamp `telemetry.data["content_hash"]`
  today. No new hashing primitive, only a new aggregation over existing ones.
- Recomputing the full set's hash from scratch on every curation run is
  O(N) in the number of curated journeys — cheap at the scale this project
  operates at today, and revisit with an incremental/Merkle-tree structure
  only if that stops being true.
- `seq` needs exactly one durable counter, scoped per corpus name. A
  restart or a failed run must not skip or reuse a value — the same
  "seed from disk, never reissue" discipline `context.SeqAllocator` already
  applies to per-journey `seq`, at a different scope.
- Splitting (item 3.7, "by group key, never by row") reads a curated set
  *after* this watermark is computed, not before — the watermark identifies
  what was curated, not how it was later divided into train/val/test.

### Alternatives rejected

- **Timestamp cutoff alone.** Simple and human-readable, but does not
  satisfy the hard constraint above — silently wrong on any retraction or
  correction. See the framing above.
- **Count alone.** Same failure mode, weaker: collides even more easily
  than a timestamp (two unrelated sets of the same size look identical).
- **Hash alone, no `seq`.** Correct, but loses the human-facing story a
  team actually wants ("which run is this") — `git log` doesn't help here
  the way it does for code, since a curation run is a data event, not a
  commit. `seq` costs one integer; keeping it is worth that.
