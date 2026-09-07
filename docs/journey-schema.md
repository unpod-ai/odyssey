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
| `message` | `Message` | one turn: `role`, `content`, `tool_calls`/`tool_response`/`tool_definitions`, `usage`, `finish_reason`, `reasoning`, `trainable_status`, and (2.1) `latency_ms`, `ttft_ms`, `provider`, `agent_id` — see [Timing and agent attribution](#timing-and-agent-attribution-21) |
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
    agent_id: str | None,            # 2.1 — the agent this journey started under
    agent_name: str | None,          # 2.1 — its human-readable name
    framework: str | None,           # 2.1 — which capture path wrote this shard
)
```

Only fields that cannot change once recording starts live here — a later
per-event tag has nowhere to land in a header that was already written.
`agent_id` is the one field that *can* change, and it is handled as a
snapshot plus a per-event delta rather than by moving it out of the header
— see below.

`framework` is `"livekit"`, `"pipecat"`, `"langchain"`, `"otel"`, or `None`
for a directly wrapped provider client. It answers "which integration is
actually feeding this corpus", which is otherwise only inferable from the
shape of what arrived.

## Timing and agent attribution (2.1)

Four optional fields on `Message` and three on `JourneyHeader`, added
because a corpus that cannot say how long a turn took, or which agent
produced it, cannot be filtered on either.

| Field | On | What it means |
|---|---|---|
| `latency_ms` | `Message` | Wall time of the provider call, request sent → response returned. Recorded on the **response** turn only: a request turn has no duration of its own, and putting the pair's latency on both halves double-counts it for anything summing the column |
| `ttft_ms` | `Message` | Time to first token. Meaningful for a streamed completion and for voice (LiveKit/Pipecat report it directly); `None` for a non-streamed call, where it would be indistinguishable from `latency_ms` |
| `provider` | `Message` | The SDK behind the call — `"openai"`, `"anthropic"`, `"gemini"`, or whatever a voice framework names its plugin. Distinct from `JourneyEvent.model_id`: one provider serves many models, and an OpenAI-compatible gateway serves models that are not OpenAI's at all |
| `agent_id` | `Message` | Which agent produced *this* turn — set only after a handoff, see the rule below |
| `agent_id` / `agent_name` / `framework` | `JourneyHeader` | The agent the journey started under, and the capture path that wrote the shard |

Timing is produced by `integrations/_timing.py`, shared by all three
provider bases: a `perf_counter`-based `Timer` measured *around* the
provider call (so the number is the provider's latency, not odyssey's own
bookkeeping, and a wall-clock adjustment mid-call cannot yield a negative
duration), and a `stamp()` that never overwrites a value an integration
that knew better — a streaming wrapper with its own TTFT — already set.

### The snapshot-plus-delta rule for `agent_id`

`agent_name` and `framework` are fixed for a journey and live only in the
header. `agent_id` is the exception: a handoff (LiveKit's
`session.current_agent`, a LangGraph node, a Pipecat flow node) changes who
is answering mid-journey, so a header field alone would attribute the whole
call to whoever started it.

The header freezes whoever started, and `JourneyContext.agent_delta()`
reports the difference, so `Message.agent_id` is written **only** on turns
that actually changed hands — exactly the rule `journey_metadata` /
`JourneyContext.event_metadata()` already follow for caller tags. For a
single-agent journey, which is nearly all of them, no message carries the
field at all; stamping the same string onto every turn is the per-event
repetition the header exists to end.

**How to read it**: take the header's `agent_id`, then override it wherever
a message names one. That yields correct attribution at every `seq` without
tracking handoff events of your own.

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
| `2.0` → `2.1` | MINOR (additive) | `Message` gained `latency_ms`, `ttft_ms`, `agent_id`, `provider`; `JourneyHeader` gained `agent_id`, `agent_name`, `framework` (see [above](#timing-and-agent-attribution-21)). Every one is optional and defaults to `None`, so a 2.0 shard decodes under 2.1 unchanged and a 2.0 reader ignores the extra keys — additive in both directions, hence MINOR. No new `EventKind`, no changed field meaning |

Current: `SCHEMA_VERSION = "2.1"`.

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
