# Journey schema

Field-by-field reference for the wire format everything in this repo is
built on. For the *why* behind these choices (event-sourcing, ambient
context, never-raise capture), see
[`adr/0004-capture-layer.md`](adr/0004-capture-layer.md) — this doc is the
*what*, that one is the *why*. Source of truth is always
`packages/odyssey-core/src/odyssey/primitives.py` and `fold.py`; if this
doc and that code disagree, the code wins.

## The one thing on the wire: `JourneyEvent`

> Append-only, ordered by a client-assigned `seq` within `journey_id`,
> idempotent on `event_id`. Cumulative `Step[]` is never stored or
> transmitted — folding N events costs O(N); shipping N cumulative steps
> would cost O(N²).

```python
JourneyEvent(
    journey_id: str,
    seq: int,                    # non-negative, unique per (journey_id, writer)
    kind: EventKind,             # "message" | "signal" | "reward" | "terminal" | "voice"
    ts: str = <utc now, ISO>,
    event_id: str = <uuid4 hex>, # dedup key
    message: Message | None,     # required iff kind == "message"
    signal: Signal | None,       # required iff kind == "signal"
    reward: Reward | None,       # required iff kind == "reward"
    terminal: Terminal | None,   # required iff kind == "terminal"
    voice: VoiceEvent | None,    # required iff kind == "voice"
    model_id: str | None,        # per-event, not per-journey (a journey can span model switches)
    metadata: dict | None,       # caller tags; also where writer identity lives (see below)
)
```

`__post_init__` enforces exactly one payload field set, matching `kind` —
an event can never carry two payloads or the wrong one. `model_id` is
per-event on purpose: one journey can span model switches, retries, and
routing fallbacks, so a journey-level label would silently mix models.

### `EventKind` payloads

| `kind` | payload | carries |
|---|---|---|
| `message` | `Message` | one turn: `role`, `content`, `tool_calls`/`tool_response`/`tool_definitions`, `usage`, `finish_reason`, `reasoning`, `trainable_status` |
| `signal` | `Signal` | explicit feedback about an earlier event — `signal` (`thumbs_up`/`thumbs_down`/`regenerated`/`user_edit`), `target_seq`, `regen_order`, `edited_output`. This is what makes DPO possible: `Reward` is a scalar judgement, a `Signal` is an *ordering* |
| `reward` | `Reward` | `aggregated_value` + optional `components: [RewardComponent]` (name/value/weight/explanation) |
| `terminal` | `Terminal` | closes the journey — `termination_reason` (`TIMEOUT`/`ENV_DONE`/`MAX_STEPS`/`TRUNCATION`/`STALE`/`ERROR`/`NONE`), optional `error`. No event with a higher `seq` is accepted after it |
| `voice` | `VoiceEvent` | STT/TTS/barge-in/latency signal alongside a turn (item 0′.4) — `voice_kind` (`stt_transcript`/`tts_output`/`barge_in`/`latency`), `text`, `confidence`, `latency_ms`. Carries no `trainable` notion; folded separately (`FoldResult.voice_events`), plays no part in SFT/DPO export |

### Writer identity — a metadata key, not a schema field

`JourneyEvent.metadata[WRITER_META_KEY]` (`"_odyssey_writer"`) identifies
which process wrote an event. `seq` is allocated per-process, so two
processes recording one journey would issue the *same* numbers for
different turns — a journey that reads as valid while silently
interleaving two conversations. Putting this in `metadata` rather than a
new field is what kept `SCHEMA_VERSION` at a MINOR bump instead of MAJOR.
`fold()` detects this (`writers`, `writer_conflict`) and refuses to mark
the journey complete.

## The shard header: `JourneyHeader`

The first line of every `*.jsonl` shard — everything `fold()` needs to
build a `Task` without a caller having to supply it:

```python
JourneyHeader(
    odyssey_schema_version: str = SCHEMA_VERSION,
    journey_id: str | None,
    data_source: str | None,
    trace_id: str | None,
    started_at: str | None,
    journey_metadata: dict | None,   # snapshot of journey-level tags as of the first event
)
```

Only fields that cannot change once recording starts live here — a later
per-event tag has nowhere to land in a header that was already written.

## The read-time projection: `fold()`

`fold()` turns an append-only, possibly out-of-order, possibly duplicated
`JourneyEvent` stream into a `Journey`. Guarantees:

- **idempotent** — deduplicated on `event_id`, so replays/re-drains are free
- **order-tolerant** — sorted on `seq`, arrival order irrelevant
- **gap-detecting** — a hole in `seq` marks the journey incomplete rather
  than silently yielding a shorter journey that looks whole
- **terminal-respecting** — events after the terminal `seq` are rejected,
  counted, and excluded

```python
FoldResult(
    journey: Journey,
    journey_id: str,
    complete: bool,              # the gate every exporter must respect
    missing_seqs: list[int],
    duplicates_dropped: int,
    rejected_after_terminal: int,
    signals: list[Signal],
    model_ids: list[str],
    terminated: bool,
    writers: list[str],
    voice_events: list[VoiceEvent],
)
```

`complete` is `False` whenever there's a `writer_conflict`, a
`missing_seqs` gap, or no terminal event yet — `incomplete_reason`
explains which. Only a complete journey may be exported for training
(`FoldResult.trainable` is literally `complete`).

## `Journey` — the folded result

```python
Journey(
    task: Task,                          # id, data_source, conversation_id, num_turns/steps, total_tokens/cost
    steps: list[Step],                   # CUMULATIVE — see below
    reward: Reward | None,
    metrics: JourneyMetrics | None,      # steps, tokens_generated, aggregated_reward, tool-call counts, tool_error_rate
    execution_metrics: ExecutionMetrics | None,  # total_time, termination_reason
    reference_journey: dict | None,
    telemetry: Telemetry | None,         # source + free-form data (annotation decisions land here)
    idx: int | None,
    error: str | None,
    trace_id: str | None,
    model_id: str | None,
)
```

`Step.messages` is **cumulative** — each step holds the whole
conversation up to that point. This is why `Step[]` is a projection
computed only at read time and never stored or transmitted on the wire:
shipping N cumulative steps costs O(N²) bytes where shipping N events
costs O(N) (`test_no_step_record_is_ever_encoded` enforces this).

## `TrainableStatus` — the four-state machine

`Message.trainable_status` / `Step.trainable_status`, one of:

- `trainable` — a real assistant output, safe to train on
- `not_trainable` — everything else by default (user turns, system prompts)
- `superseded` — replaced by a later regeneration/edit (`Signal.signal` in
  `{regenerated, user_edit}`) — kept for DPO's rejected side, excluded
  from SFT
- `summarization_boundary` — a message flagged (any role) as a compaction
  point; loss is attributed to the summary, not the original turns it
  replaced

`derive_trainable_status()` in `fold.py` is the single place this is
computed — no other module re-derives it (`data_preparation/normalization`
reuses it directly for BYOD imports, which have no signal history of
their own).

## Versioning (`SCHEMA_VERSION`)

Bumped only for a breaking change to the **on-the-wire event shape**. The
reader rejects an unrecognized MAJOR outright rather than mis-parsing
(`jsonl.py`).

| Version | Kind | What changed |
|---|---|---|
| `1.0` → `1.1` | MINOR (additive) | Header gained journey identity (`JourneyHeader`); a `message.trainable_status` still at the writer default is no longer encoded. A 1.0 reader still parses a 1.1 file — extra header keys are ignored, the absent label decodes back to its default |
| `1.x` → `2.0` | MAJOR (breaking) | New `"voice"` `EventKind` with its own payload field (item 0′.4). A 1.x reader's kind-dispatch has no branch for `"voice"` — it would drop real turns or raise, not safely ignore them. No migration tool ships with this bump; a 1.x shard on disk simply stops parsing under a 2.x reader |

Current: `SCHEMA_VERSION = "2.0"`.

## Where this schema is consumed

```
JourneyEvent (this doc)
  → odyssey.spool / services/collector    append-only storage, exactly this shape
  → fold() → Journey                      the read-time projection
  → builders/{messages,journey,steps,metrics,reward}.py   trace → training-example assembly
  → data_preparation                      collection/cleaning/normalization/... over Journey
  → odyssey_schemas (services/api's DTOs) a narrowed, wire-safe *view* of Journey/JourneyMetrics
                                           for services/api's JSON responses — not this schema
                                           re-encoded, a deliberately smaller read-only projection
```

`odyssey_schemas.JourneyDetailOut`/`JourneySummaryOut`/`JourneyMetricsOut`
are **not** `JourneyEvent`/`Journey` reused — they're independently
defined DTOs that expose only what a read API caller needs (see
`packages/odyssey-schemas/README.md`).
