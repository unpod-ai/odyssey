"""Fold semantics: idempotency, ordering, gaps, terminal cut, labeling."""

from __future__ import annotations

import dataclasses
from typing import Optional

import pytest

from odyssey.fold import SUMMARIZATION_FLAG, derive_trainable_status, fold
from odyssey.primitives import (
    JourneyEvent,
    Message,
    Reward,
    Role,
    Signal,
    Terminal,
    TerminationReason,
    ToolCall,
    ToolResponse,
)

JID = "j_1"


def msg_event(seq: int, role: Role, content: Optional[str], **kw) -> JourneyEvent:
    return JourneyEvent(
        journey_id=JID,
        seq=seq,
        kind="message",
        message=Message(role=role, content=content, **kw),
        ts=f"2026-01-01T00:00:{seq:02d}+00:00",
        event_id=f"e{seq}",
    )


def terminal_event(
    seq: int, reason: TerminationReason = "ENV_DONE", error=None
) -> JourneyEvent:
    return JourneyEvent(
        journey_id=JID,
        seq=seq,
        kind="terminal",
        terminal=Terminal(termination_reason=reason, error=error),
        event_id=f"t{seq}",
    )


def basic_stream() -> list[JourneyEvent]:
    return [
        msg_event(0, "system", "you are helpful"),
        msg_event(1, "user", "hi"),
        msg_event(2, "assistant", "hello"),
        terminal_event(3),
    ]


# --------------------------------------------------------------------------
# Event validation
# --------------------------------------------------------------------------


def test_event_requires_matching_payload():
    with pytest.raises(ValueError, match="requires a 'message' payload"):
        JourneyEvent(journey_id=JID, seq=0, kind="message")


def test_event_rejects_extra_payload():
    with pytest.raises(ValueError, match="must not carry"):
        JourneyEvent(
            journey_id=JID,
            seq=0,
            kind="message",
            message=Message(role="user", content="x"),
            terminal=Terminal(),
        )


def test_event_rejects_negative_seq():
    with pytest.raises(ValueError, match="non-negative"):
        JourneyEvent(
            journey_id=JID, seq=-1, kind="message", message=Message(role="user")
        )


def test_payload_property_returns_the_set_payload():
    e = msg_event(0, "user", "hi")
    assert e.payload == e.message


# --------------------------------------------------------------------------
# Idempotency and ordering
# --------------------------------------------------------------------------


def test_duplicate_event_ids_are_dropped():
    events = basic_stream()
    once = fold(events, data_source="t")
    twice = fold(events + events, data_source="t")
    assert twice.duplicates_dropped == len(events)
    assert twice.journey == once.journey


def test_out_of_order_arrival_folds_identically():
    events = basic_stream()
    shuffled = [events[2], events[0], events[3], events[1]]
    assert (
        fold(shuffled, data_source="t").journey == fold(events, data_source="t").journey
    )


def test_empty_stream_rejected():
    with pytest.raises(ValueError, match="empty event stream"):
        fold([], data_source="t")


def test_mixed_journey_ids_rejected():
    other = dataclasses.replace(msg_event(1, "user", "hi"), journey_id="j_2")
    with pytest.raises(ValueError, match="one journey"):
        fold([msg_event(0, "user", "a"), other], data_source="t")


# --------------------------------------------------------------------------
# Gaps and completeness
# --------------------------------------------------------------------------


def test_gap_marks_journey_incomplete_and_names_the_hole():
    events = [
        msg_event(0, "user", "a"),
        msg_event(1, "assistant", "b"),
        msg_event(3, "assistant", "d"),
    ]
    r = fold(events, data_source="t")
    assert r.missing_seqs == [2]
    assert r.complete is False
    assert r.trainable is False


def test_no_terminal_means_incomplete_even_without_gaps():
    r = fold(
        [msg_event(0, "user", "a"), msg_event(1, "assistant", "b")], data_source="t"
    )
    assert r.missing_seqs == []
    assert r.terminated is False
    assert r.complete is False


def test_contiguous_terminated_stream_is_complete():
    r = fold(basic_stream(), data_source="t")
    assert r.complete is True and r.trainable is True


def test_events_after_terminal_are_rejected_and_counted():
    events = basic_stream() + [msg_event(4, "assistant", "late")]
    r = fold(events, data_source="t")
    assert r.rejected_after_terminal == 1
    # the late turn never reaches the journey
    assert all(m.content != "late" for s in r.journey.steps for m in s.messages)


def test_terminal_reason_and_error_reach_the_journey():
    events = basic_stream()[:-1] + [terminal_event(3, reason="ERROR", error="boom")]
    r = fold(events, data_source="t")
    metrics = r.journey.execution_metrics
    assert metrics is not None
    assert metrics.termination_reason == "ERROR"
    assert r.journey.error == "boom"


# --------------------------------------------------------------------------
# trainable_status state machine
# --------------------------------------------------------------------------


def test_role_defaults_only_train_assistant_turns():
    r = fold(basic_stream(), data_source="t")
    final = r.journey.steps[-1].messages
    by_role = {m.role: m.trainable_status for m in final}
    assert by_role["assistant"] == "trainable"
    assert by_role["user"] == "not_trainable"
    assert by_role["system"] == "not_trainable"


def test_regenerated_signal_supersedes_its_target():
    events = basic_stream()[:-1] + [
        JourneyEvent(
            journey_id=JID,
            seq=3,
            kind="signal",
            signal=Signal(signal="regenerated", target_seq=2, regen_order=0),
            event_id="s3",
        ),
        msg_event(4, "assistant", "hello again"),
        terminal_event(5),
    ]
    r = fold(events, data_source="t")
    final = {m.content: m.trainable_status for m in r.journey.steps[-1].messages}
    assert final["hello"] == "superseded"
    assert final["hello again"] == "trainable"


def test_user_edit_supersedes_and_carries_the_replacement():
    events = basic_stream()[:-1] + [
        JourneyEvent(
            journey_id=JID,
            seq=3,
            kind="signal",
            signal=Signal(signal="user_edit", target_seq=2, edited_output="hi there"),
            event_id="s3",
        ),
        terminal_event(4),
    ]
    r = fold(events, data_source="t")
    assert r.signals[0].edited_output == "hi there"
    assert r.journey.steps[-1].messages[-1].trainable_status == "superseded"


def test_thumbs_down_blocks_training_thumbs_up_forces_it():
    down = derive_trainable_status(
        {0: Message(role="assistant", content="a")},
        [Signal(signal="thumbs_down", target_seq=0)],
    )
    up = derive_trainable_status(
        {0: Message(role="user", content="a")},
        [Signal(signal="thumbs_up", target_seq=0)],
    )
    assert down[0] == "not_trainable"
    assert up[0] == "trainable"


def test_summarization_flag_outranks_everything():
    statuses = derive_trainable_status(
        {
            0: Message(
                role="assistant", content="summary", metadata={SUMMARIZATION_FLAG: True}
            )
        },
        [Signal(signal="thumbs_up", target_seq=0)],
    )
    assert statuses[0] == "summarization_boundary"


def test_no_message_keeps_the_dataclass_default():
    """Every message in a folded journey carries an assigned status."""
    r = fold(basic_stream(), data_source="t")
    for step in r.journey.steps:
        for m in step.messages:
            assert m.trainable_status in {
                "trainable",
                "not_trainable",
                "superseded",
                "summarization_boundary",
            }


def test_step_status_follows_the_turn_it_ends_on():
    r = fold(basic_stream(), data_source="t")
    last = r.journey.steps[-1]
    assert last.trainable_status == last.messages[-1].trainable_status == "trainable"


# --------------------------------------------------------------------------
# Per-event model attribution
# --------------------------------------------------------------------------


def test_single_model_journey_keeps_a_journey_level_label():
    events = [
        msg_event(0, "user", "hi"),
        dataclasses.replace(
            msg_event(1, "assistant", "yo"), model_id="openai/gpt-4.1-mini"
        ),
        terminal_event(2),
    ]
    r = fold(events, data_source="t")
    assert r.model_ids == ["openai/gpt-4.1-mini"]
    assert r.journey.model_id == "openai/gpt-4.1-mini"


def test_mid_journey_model_switch_refuses_a_single_label():
    events = [
        msg_event(0, "user", "hi"),
        dataclasses.replace(
            msg_event(1, "assistant", "a"), model_id="openai/gpt-4.1-mini"
        ),
        dataclasses.replace(
            msg_event(2, "assistant", "b"), model_id="anthropic/claude-haiku-4-5"
        ),
        terminal_event(3),
    ]
    r = fold(events, data_source="t")
    assert r.model_ids == ["openai/gpt-4.1-mini", "anthropic/claude-haiku-4-5"]
    # A single journey-level label would misattribute half the turns.
    assert r.journey.model_id is None


# --------------------------------------------------------------------------
# Derived metrics that upstream never populated
# --------------------------------------------------------------------------


def test_aggregated_reward_is_populated_from_the_reward_event():
    events = basic_stream()[:-1] + [
        JourneyEvent(
            journey_id=JID,
            seq=3,
            kind="reward",
            reward=Reward(aggregated_value=0.8, aggregation_method="identity"),
            event_id="r3",
        ),
        terminal_event(4),
    ]
    r = fold(events, data_source="t")
    metrics = r.journey.metrics
    assert metrics is not None
    assert metrics.aggregated_reward == 0.8


def test_num_tool_response_none_is_counted():
    events = [
        msg_event(0, "user", "go"),
        msg_event(
            1,
            "assistant",
            None,
            tool_calls=[ToolCall(name="f", arguments={}, id="c1")],
        ),
        msg_event(
            2,
            "tool",
            None,
            tool_response=ToolResponse(id="c1", name="f", arguments={}, response=None),
        ),
        terminal_event(3),
    ]
    r = fold(events, data_source="t")
    metrics = r.journey.metrics
    assert metrics is not None
    assert metrics.num_tool_response_none == 1


# --------------------------------------------------------------------------
# Projection
# --------------------------------------------------------------------------


def test_steps_are_cumulative_and_grow_by_turn():
    r = fold(basic_stream(), data_source="t")
    counts = [len(s.messages) for s in r.journey.steps]
    assert counts == sorted(counts)
    assert counts[-1] == 3  # system + user + assistant


# --------------------------------------------------------------------------
# Writer conflict
#
# `seq` is allocated per process, so two processes recording one journey issue
# the same numbers for different turns. The result reads as a valid journey while
# actually interleaving two conversations — the one corruption that must never
# reach a corpus quietly. The fold has to catch it, which is the whole reason
# writer identity is stamped into event metadata.
# --------------------------------------------------------------------------


def written_by(event: JourneyEvent, writer: str) -> JourneyEvent:
    from odyssey.primitives import WRITER_META_KEY

    return dataclasses.replace(event, metadata={WRITER_META_KEY: writer})


def test_a_single_writer_is_reported_and_stays_trainable():
    events = [written_by(e, "w1") for e in basic_stream()]
    result = fold(events, data_source="t")
    assert result.writers == ["w1"]
    assert not result.writer_conflict
    assert result.trainable
    assert result.incomplete_reason is None


def test_two_writers_are_detected():
    events = basic_stream()
    tagged = [written_by(e, "w1" if i % 2 == 0 else "w2") for i, e in enumerate(events)]
    result = fold(tagged, data_source="t")
    assert sorted(result.writers) == ["w1", "w2"]
    assert result.writer_conflict


def test_a_writer_conflict_blocks_export_even_with_no_gaps():
    """Complete-looking and still unexportable: that is the point."""
    events = basic_stream()
    tagged = [written_by(e, "w1" if i % 2 == 0 else "w2") for i, e in enumerate(events)]
    result = fold(tagged, data_source="t")
    assert result.missing_seqs == []
    assert result.terminated
    assert not result.complete
    assert not result.trainable


def test_the_conflict_names_itself_in_the_reason():
    events = basic_stream()
    tagged = [written_by(e, "w1" if i % 2 == 0 else "w2") for i, e in enumerate(events)]
    reason = fold(tagged, data_source="t").incomplete_reason
    assert reason is not None
    assert "writer conflict" in reason
    assert "w1" in reason and "w2" in reason


def test_untagged_events_report_no_writers_and_stay_trainable():
    """Events written before this layer existed must not become unexportable."""
    result = fold(basic_stream(), data_source="t")
    assert result.writers == []
    assert not result.writer_conflict
    assert result.trainable


def test_a_gap_is_reported_ahead_of_a_missing_terminal():
    events = [msg_event(0, "user", "a"), msg_event(2, "assistant", "b")]
    reason = fold(events, data_source="t").incomplete_reason
    assert reason is not None
    assert "missing seq [1]" in reason
