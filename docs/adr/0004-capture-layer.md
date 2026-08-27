# ADR 0004 — The capture layer: event-sourced, ambient, never-raise

Status: accepted · Date: 2026-08-27

## Context

The product requirement, stated plainly in `docs/WORKING.md`: install it in
**one place**; every call log lands automatically; everything collects into
**one destination**; that destination feeds model training. Two halves pull
in different directions — collection must be invisible (no call site names a
`journey_id` or a `seq`), and the dump must be training-grade (cumulative
turns, tool-call correlation, per-turn `trainable` labels, DPO-ready
preference pairs) rather than an observability trace with everything logged.

Meeting the first half needs a process-wide singleton — a live `atexit`
hook, an optional `SIGTERM` handler, a background drain thread — inside
`packages/odyssey-core`. That is a deliberate exception to ADR 0001's own
rule: "`packages/` imports nothing above it... no side effects." This ADR is
that exception, made explicit, and the design that makes it safe to hold.

## Decision

**1. Event-sourced core.** `JourneyEvent` is the only unit ever written to
disk or sent over a network — append-only, ordered by a client-assigned
`seq` within `journey_id`, idempotent on `event_id`. Cumulative `Step[]` is
never stored or transmitted: a step holds the whole conversation up to its
point, so shipping N cumulative steps costs O(N²) bytes where shipping N
events costs O(N). `Step[]` is a *projection*, computed by `fold()` only at
read time (`test_no_step_record_is_ever_encoded` enforces this at the wire
level).

**2. Ambient context, not threaded parameters.** `journey()` opens or joins
a journey held in a `ContextVar`; `_emit()` pulls `journey_id` and the next
`seq` from it. No call site names either. `ContextVar` propagates into
`asyncio` tasks automatically but not into a new `threading.Thread` —
`context.bind()` exists for the explicit handoff that case needs.

**3. Single writer per journey — a contract with detection, not an
assumption.** `seq` is allocated per process, seeded from whatever is
already on disk. Two processes recording one journey would both seed from
the same maximum and issue the same numbers — a journey that *reads* as
valid while silently interleaving two conversations, and `fold()`'s own
`event_id` dedup cannot catch that on its own. Every event therefore carries
a `writer_id` in `JourneyEvent.metadata` (`WRITER_META_KEY`) — not a new
schema field, which is what keeps `SCHEMA_VERSION` at a MINOR bump rather
than a MAJOR one. `fold()` reports `writers`, exposes `writer_conflict`, and
sets `complete = False` when there is more than one; the CLI exits `3`.

**4. Never crash the host.** An observability layer that takes down the
application it observes is worse than no observability layer. Every capture
failure is counted (`Client.stats`, surfaced through `odyssey.health()`),
never raised — except `ODYSSEY_DEBUG=1`, which re-raises so a developer sees
the fault during development.

**5. Recording never touches the network.** `journey()`/`_emit()` append to
a local on-disk spool and return; a separate drain (interval thread, CLI
command, or explicit `flush()`) ships batches out of band. The local shard
*is* the retry queue: no backoff scheduler, no in-memory buffer to lose — a
shard stays on disk until the sink acknowledges it, and only then does the
watermark advance.

## Alternatives rejected

- **Auto-create a journey when none is active.** A single orphaned event
  would mint a one-event journey with no terminal, which `fold()` can never
  mark complete — untrainable noise that accumulates silently. Events
  outside a journey are dropped and counted instead; integration wrappers
  (`integrations/anthropic.py`, `integrations/openai.py`) open their own
  journey so a standalone call is still captured.
- **Per-writer sequence numbers**, avoiding the single-writer constraint
  entirely. Real, and still open — but it is a `SCHEMA_VERSION` MAJOR bump,
  and nothing today actually spans two writers by design. Detection now,
  redesign only when a real need appears, not speculatively.
- **Record everything, Langfuse-style.** A corpus is not a span log: an
  arbitrary internal function call is noise that every downstream recipe
  would have to filter back out. `@observe()` establishes journey context
  and records nothing by itself; `@observe(as_tool=True)` records a call
  specifically because tool use is behaviour worth training on. This is the
  one place the two designs (observability vs. training corpus) genuinely
  diverge.
- **Raise on capture failure.** Directly violates decision 4. The one
  escape hatch (`ODYSSEY_DEBUG=1`) is opt-in and documented as a
  development aid, not a production posture.

## Consequences

- `packages/odyssey-core` holds real process-wide mutable state and real
  side effects (an `atexit` hook always, a `SIGTERM` handler when
  opted in, a daemon drain thread) — an intentional, narrow exception to
  ADR 0001 rule 1, justified by decision 1 of this ADR (the whole reason a
  capture layer exists is to be the one integration point).
- A second writer on one journey is a detectable but unrecoverable
  corruption: the fold refuses the journey rather than silently training on
  an interleaved conversation. Operationally this shows up as `fold()`
  reporting `complete = False` and the CLI's `health` command exiting `3` —
  both already tested (`test_two_writers_are_detected`,
  `test_health_exits_3_on_a_writer_conflict`).
- Durability rests on the OS page cache flush, not `fsync`, by default —
  `fsync=False` trades a narrow crash window (process killed *and* the OS
  itself dies before flushing) for hot-path latency. `SpoolConfig(fsync=True)`
  is the escape hatch when the threat model is machine loss, not process
  loss.
- Every provider integration (Anthropic, OpenAI, LiveKit) is a *consumer* of
  this ADR, not a new design: each wraps `journey()`/`_emit()` and inherits
  the never-raise boundary and the single-writer contract for free.
