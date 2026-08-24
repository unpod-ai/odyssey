"""Fold an append-only ``JourneyEvent`` stream into a ``Journey``.

The fold is the read side of odyssey's event-sourced core. It must be safe to
run over a stream that arrived out of order, contains duplicates, or is missing
events entirely — because `seq` is client-assigned and the drain is at-least-once.

Guarantees:

- **idempotent** — deduplicated on ``event_id``, so replays and re-drains are free
- **order-tolerant** — sorted on ``seq``, so arrival order is irrelevant
- **gap-detecting** — a hole in ``seq`` marks the journey incomplete rather than
  silently yielding a shorter journey that looks whole
- **terminal-respecting** — events after the terminal ``seq`` are rejected, counted,
  and excluded

Cumulative ``Step[]`` are produced here, at read time, by the ported
``builders.steps.build_cumulative_steps``. They are never stored or transmitted —
folding is the projection (design.md Decisions 1 and 8).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from odyssey.builders.journey import build_journey_from_messages
from odyssey.primitives import (
    WRITER_META_KEY,
    Journey,
    JourneyEvent,
    JourneyMetrics,
    Message,
    Reward,
    Signal,
    Step,
    TrainableStatus,
)

# Signals that mean "the targeted output was replaced by a later one".
_SUPERSEDING: frozenset[str] = frozenset({"regenerated", "user_edit"})

# A message whose metadata carries this key is a compaction boundary regardless
# of role — the escape hatch that lets a producer say "loss here is on a summary,
# not on the original turns".
SUMMARIZATION_FLAG = "summarization_boundary"

# A turn the speaker never finished: the caller barged in and the agent stopped
# mid-utterance. Voice producers set it (see integrations/livekit.py); text ones
# never will.
#
# It is structural, like the summarization flag, and it beats an explicit
# thumbs-up on purpose. Consider which error is worse: dropping a truncated turn
# a human happened to approve loses one example, while training on it teaches the
# model to stop mid-sentence — a defect that shows up in every future generation.
INTERRUPTED_FLAG = "interrupted"


@dataclass(frozen=True)
class FoldResult:
    """A folded journey plus everything the fold learned while folding it.

    ``complete`` is the gate an exporter must respect: an incomplete journey has
    a known hole in it and training on it silently teaches the model a
    conversation that never happened.
    """

    journey: Journey
    journey_id: str
    complete: bool
    missing_seqs: List[int] = field(default_factory=list)
    duplicates_dropped: int = 0
    rejected_after_terminal: int = 0
    signals: List[Signal] = field(default_factory=list)
    model_ids: List[str] = field(default_factory=list)
    terminated: bool = False
    writers: List[str] = field(default_factory=list)

    @property
    def trainable(self) -> bool:
        """Whether this journey may be exported for training."""
        return self.complete

    @property
    def writer_conflict(self) -> bool:
        """Two or more processes wrote this journey — the numbering is unsound.

        ``seq`` is allocated per process, so two writers hand out the same
        numbers for different turns. The result reads as a valid journey while
        being a silent interleaving of two conversations, which is exactly the
        failure that must never reach a corpus. Reported, and ``complete`` is
        false.
        """
        return len(self.writers) > 1

    @property
    def incomplete_reason(self) -> Optional[str]:
        """Why this journey is not exportable, or ``None`` when it is."""
        if self.writer_conflict:
            return f"writer conflict: {len(self.writers)} writers {self.writers}"
        if self.missing_seqs:
            return f"missing seq {self.missing_seqs}"
        if not self.terminated:
            return "no terminal event: journey may still be running"
        return None


def derive_trainable_status(
    messages_by_seq: Dict[int, Message],
    signals: List[Signal],
) -> Dict[int, TrainableStatus]:
    """Assign a ``TrainableStatus`` to every message event.

    Upstream declared this field and never populated it — the four-state machine
    has no prior art to port (design.md Decision 4), so this is the definition.

    Precedence, highest first:

    1. ``summarization_boundary`` — structural; the turn is a compaction artifact
    2. ``interrupted``           — structural; the speaker never finished the turn
    3. ``superseded``            — a ``regenerated``/``user_edit`` signal replaced it
    4. ``not_trainable``         — an explicit ``thumbs_down``
    5. ``trainable``             — an explicit ``thumbs_up``
    6. role default              — assistant is trainable, everything else is not

    Rule 6 is the whole point: only the model's own outputs carry gradient. A
    system prompt or a tool result is context, not a target.

    Rules 1 and 2 sit above the human signals because they describe what the turn
    *is*, not how good it was. A cut-off utterance is not a valid target even if
    someone approved its content.
    """
    superseded: set[int] = set()
    thumbs_down: set[int] = set()
    thumbs_up: set[int] = set()
    for sig in signals:
        if sig.signal in _SUPERSEDING:
            superseded.add(sig.target_seq)
        elif sig.signal == "thumbs_down":
            thumbs_down.add(sig.target_seq)
        elif sig.signal == "thumbs_up":
            thumbs_up.add(sig.target_seq)

    out: Dict[int, TrainableStatus] = {}
    for seq, msg in messages_by_seq.items():
        meta = msg.metadata or {}
        if meta.get(SUMMARIZATION_FLAG):
            out[seq] = "summarization_boundary"
        elif meta.get(INTERRUPTED_FLAG) and msg.role == "assistant":
            # Only the agent's own turns matter here: a user being cut off is
            # normal conversation, while a half-generated agent reply is a target
            # that would teach truncation.
            out[seq] = "not_trainable"
        elif seq in superseded:
            out[seq] = "superseded"
        elif seq in thumbs_down:
            out[seq] = "not_trainable"
        elif seq in thumbs_up:
            out[seq] = "trainable"
        elif msg.role == "assistant":
            out[seq] = "trainable"
        else:
            out[seq] = "not_trainable"
    return out


def fold(
    events: List[JourneyEvent],
    *,
    data_source: str,
    conversation_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    task_metadata: Optional[Dict[str, Any]] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
) -> FoldResult:
    """Fold events for exactly one journey into a :class:`FoldResult`.

    Raises ``ValueError`` on an empty stream or a stream mixing ``journey_id``s —
    both are caller bugs, not recoverable data conditions.
    """
    if not events:
        raise ValueError("cannot fold an empty event stream")

    journey_ids = {e.journey_id for e in events}
    if len(journey_ids) > 1:
        raise ValueError(
            f"fold() takes events for one journey; got {sorted(journey_ids)}"
        )
    journey_id = next(iter(journey_ids))

    # 1. Idempotency: first occurrence of an event_id wins.
    by_id: Dict[str, JourneyEvent] = {}
    duplicates = 0
    for e in events:
        if e.event_id in by_id:
            duplicates += 1
            continue
        by_id[e.event_id] = e

    ordered = sorted(by_id.values(), key=lambda e: e.seq)

    # 2. Terminal cut. The lowest-seq terminal wins; anything above it is not
    #    part of the journey and is reported rather than quietly kept.
    terminal_seq: Optional[int] = None
    for e in ordered:
        if e.kind == "terminal":
            terminal_seq = e.seq
            break
    rejected = 0
    if terminal_seq is not None:
        kept = [e for e in ordered if e.seq <= terminal_seq]
        rejected = len(ordered) - len(kept)
        ordered = kept

    # 3. Gap detection over the surviving contiguous range.
    present = {e.seq for e in ordered}
    highest = max(present)
    missing = sorted(set(range(0, highest + 1)) - present)

    # 4. Partition payloads.
    messages_by_seq: Dict[int, Message] = {}
    signals: List[Signal] = []
    reward: Optional[Reward] = None
    termination_reason = None
    error: Optional[str] = None
    model_ids: List[str] = []
    writers: List[str] = []
    for e in ordered:
        if e.model_id and e.model_id not in model_ids:
            model_ids.append(e.model_id)
        writer = (e.metadata or {}).get(WRITER_META_KEY)
        if isinstance(writer, str) and writer not in writers:
            writers.append(writer)
        if e.kind == "message" and e.message is not None:
            messages_by_seq[e.seq] = e.message
        elif e.kind == "signal" and e.signal is not None:
            signals.append(e.signal)
        elif e.kind == "reward" and e.reward is not None:
            reward = e.reward  # last reward event wins
        elif e.kind == "terminal" and e.terminal is not None:
            termination_reason = e.terminal.termination_reason
            error = e.terminal.error

    # 5. Label, then rebuild the messages in seq order carrying their status.
    statuses = derive_trainable_status(messages_by_seq, signals)
    messages: List[Message] = [
        dataclasses.replace(msg, trainable_status=statuses[seq])
        for seq, msg in sorted(messages_by_seq.items())
    ]

    # 6. Build the Journey. Cumulative steps are computed here and only here.
    journey = build_journey_from_messages(
        messages,
        conversation_id=conversation_id or journey_id,
        data_source=data_source,
        reward=reward,
        task_metadata=task_metadata,
        error=error,
        start_time=start_time,
        end_time=end_time,
        termination_reason=termination_reason,
        trace_id=trace_id,
        # Journey-level model_id only when the journey never switched models;
        # otherwise a single label would misattribute. Per-event stays authoritative.
        model_id=model_ids[0] if len(model_ids) == 1 else None,
    )
    journey = _populate_derived(journey, reward=reward, messages=messages)

    return FoldResult(
        journey=journey,
        journey_id=journey_id,
        # A second writer invalidates the sequence numbering itself, so it gates
        # export exactly like a hole does.
        complete=not missing and terminal_seq is not None and len(writers) <= 1,
        missing_seqs=missing,
        duplicates_dropped=duplicates,
        rejected_after_terminal=rejected,
        signals=signals,
        model_ids=model_ids,
        terminated=terminal_seq is not None,
        writers=writers,
    )


def _populate_derived(
    journey: Journey,
    *,
    reward: Optional[Reward],
    messages: List[Message],
) -> Journey:
    """Fill the metrics upstream declared but never assigned.

    ``aggregated_reward`` and ``num_tool_response_none`` had no producer in the
    source SDK; neither did ``Step.trainable_status``. Each gets one here.
    """
    none_responses = sum(
        1
        for m in messages
        if m.tool_response is not None and m.tool_response.response is None
    )
    base = journey.metrics or JourneyMetrics()
    metrics = dataclasses.replace(
        base,
        aggregated_reward=reward.aggregated_value if reward else None,
        num_tool_response_none=none_responses,
    )
    # A step inherits the status of the turn it ends on — that is the message
    # the step exists to train.
    steps: List[Step] = [
        dataclasses.replace(
            step,
            trainable_status=(
                step.messages[-1].trainable_status if step.messages else "not_trainable"
            ),
        )
        for step in journey.steps
    ]
    return dataclasses.replace(journey, metrics=metrics, steps=steps)
