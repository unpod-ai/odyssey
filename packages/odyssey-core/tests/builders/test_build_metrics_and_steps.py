"""Tests for odyssey.build.metrics and odyssey.build.steps."""

from __future__ import annotations

from odyssey.builders.metrics import (
    aggregate_cost,
    aggregate_tokens,
    count_tool_calls,
    count_tool_failures,
    parse_total_time,
    tool_error_rate,
)
from odyssey.builders.steps import build_cumulative_steps
from odyssey.primitives import Message, ToolCall, ToolResponse


def test_count_tool_calls_only_assistant():
    msgs = [
        Message(
            role="assistant",
            tool_calls=[
                ToolCall(name="a", arguments={}),
                ToolCall(name="b", arguments={}),
            ],
        ),
        Message(role="user", content="hi"),
        Message(role="assistant", content="no tools"),
    ]
    assert count_tool_calls(msgs) == 2


def test_count_tool_failures_only_errored():
    msgs = [
        Message(
            role="tool",
            tool_response=ToolResponse(id="1", name="x", arguments={}, response="ok"),
        ),
        Message(
            role="tool",
            tool_response=ToolResponse(id="2", name="y", arguments={}, error="boom"),
        ),
    ]
    assert count_tool_failures(msgs) == 1


def test_tool_error_rate_zero_when_no_calls():
    assert tool_error_rate([Message(role="user", content="hi")]) == 0.0


def test_tool_error_rate_normal():
    msgs = [
        Message(role="assistant", tool_calls=[ToolCall(name="x", arguments={})]),
        Message(
            role="tool",
            tool_response=ToolResponse(id="1", name="x", arguments={}, error="boom"),
        ),
    ]
    assert tool_error_rate(msgs) == 1.0


def test_parse_total_time_delta():
    assert parse_total_time("2025-01-01T00:00:00Z", "2025-01-01T00:00:10Z") == 10.0


def test_parse_total_time_none_when_missing_endpoint():
    assert parse_total_time(None, "2025-01-01T00:00:10Z") is None
    assert parse_total_time("2025-01-01T00:00:00Z", None) is None


def test_parse_total_time_none_when_malformed():
    assert parse_total_time("garbage", "2025-01-01T00:00:10Z") is None


def test_aggregate_tokens_sums_across_runs():
    runs = [
        {"total_tokens": 10, "prompt_tokens": 5, "completion_tokens": 5},
        {"total_tokens": 20, "prompt_tokens": 12, "completion_tokens": 8},
        {"total_tokens": None},
    ]
    assert aggregate_tokens(runs) == {
        "total_tokens": 30,
        "prompt_tokens": 17,
        "completion_tokens": 13,
    }


def test_aggregate_cost_handles_none():
    assert (
        aggregate_cost([{"total_cost": 1.5}, {"total_cost": None}, {"total_cost": 2.5}])
        == 4.0
    )


def test_cumulative_steps_empty():
    assert build_cumulative_steps([]) == []


def test_cumulative_steps_simple():
    msgs = [
        Message(role="system", content="be helpful"),
        Message(role="user", content="hi"),
        Message(role="assistant", content="hello"),
    ]
    steps = build_cumulative_steps(msgs)
    assert len(steps) >= 1
    assert steps[-1].messages[-1].content == "hello"
    assert all(m.content is not None for m in steps[-1].messages)


def test_cumulative_steps_tool_roundtrip_stays_in_one_turn():
    """A tool call and its result do not end a step.

    The user asked once; the agent looking something up before answering is
    still that one exchange. Splitting on the tool response produced three
    near-identical cumulative snapshots of a single turn.
    """
    msgs = [
        Message(role="user", content="calc"),
        Message(role="assistant", tool_calls=[ToolCall(name="c", arguments={})]),
        Message(
            role="tool",
            tool_response=ToolResponse(id="1", name="c", arguments={}, response="42"),
        ),
        Message(role="assistant", content="final"),
    ]
    steps = build_cumulative_steps(msgs)
    assert len(steps) == 1
    assert len(steps[0].messages) == 4
    assert steps[0].messages[-1].content == "final"


def test_cumulative_steps_one_per_turn():
    """Two exchanges, two steps, cumulative."""
    msgs = [
        Message(role="system", content="sys"),
        Message(role="user", content="q1"),
        Message(role="assistant", content="a1"),
        Message(role="user", content="q2"),
        Message(role="assistant", content="a2"),
    ]
    steps = build_cumulative_steps(msgs)
    assert [len(s.messages) for s in steps] == [3, 5]


def test_cumulative_steps_consecutive_assistant_utterances_are_one_turn():
    """A voice agent emits one item per spoken utterance.

    Snapshotting each one gave a step per utterance -- 33 steps for a 15-turn
    phone call, every one a near-copy of the last.
    """
    msgs = [
        Message(role="user", content="hello"),
        Message(role="assistant", content="Three players, got it."),
        Message(role="assistant", content="Let me check availability."),
        Message(role="user", content="ok"),
        Message(role="assistant", content="Found a slot."),
    ]
    steps = build_cumulative_steps(msgs)
    assert [len(s.messages) for s in steps] == [3, 5]


def test_cumulative_steps_consecutive_user_utterances_are_one_turn():
    """Split STT finals and barge-in both produce back-to-back user messages."""
    msgs = [
        Message(role="user", content="two players"),
        Message(role="user", content="for the twenty fourth"),
        Message(role="assistant", content="noted"),
    ]
    steps = build_cumulative_steps(msgs)
    assert len(steps) == 1


def test_cumulative_steps_trailing_user_turn_is_kept_but_untrainable():
    """The caller's last words survive even with no reply to pair them with."""
    msgs = [
        Message(role="user", content="book it"),
        Message(role="assistant", content="done"),
        Message(role="user", content="thanks"),
    ]
    steps = build_cumulative_steps(msgs)
    assert [s.messages[-1].role for s in steps] == ["assistant", "user"]
    assert steps[-1].messages[-1].content == "thanks"


def test_cumulative_steps_mid_system_swap_is_copy_on_write():
    """Mid-conversation system swap must not rewrite history.

    Legitimate providers (LangSmith, most OpenAI-wrapping loops) re-send
    the system prompt at each LLM call, producing a flat message list with
    multiple system messages. Earlier steps must keep the system prompt
    they were built with; later steps use the updated one.
    """
    msgs = [
        Message(role="system", content="v1"),
        Message(role="user", content="hi"),
        Message(role="assistant", content="hello"),
        Message(role="system", content="v2"),
        Message(role="user", content="again"),
        Message(role="assistant", content="hi again"),
    ]
    steps = build_cumulative_steps(msgs)

    first_systems = [m.content for m in steps[0].messages if m.role == "system"]
    assert first_systems == ["v1"], "pre-swap step lost its original system prompt"

    last_systems = [m.content for m in steps[-1].messages if m.role == "system"]
    assert last_systems == [
        "v2"
    ], "post-swap step did not pick up the new system prompt"


def test_cumulative_steps_consecutive_system_block_stacks():
    """A contiguous block of system messages mid-conversation forms one
    replacement -- the first clears the prefix, the rest stack in order.
    """
    msgs = [
        Message(role="system", content="v1"),
        Message(role="user", content="hi"),
        Message(role="assistant", content="hello"),
        Message(role="system", content="v2a"),
        Message(role="system", content="v2b"),
        Message(role="user", content="again"),
        Message(role="assistant", content="ok"),
    ]
    steps = build_cumulative_steps(msgs)
    last_systems = [m.content for m in steps[-1].messages if m.role == "system"]
    assert last_systems == ["v2a", "v2b"]
