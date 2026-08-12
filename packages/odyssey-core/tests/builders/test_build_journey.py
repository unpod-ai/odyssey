"""Tests for odyssey.build.journey."""

from __future__ import annotations

from odyssey.builders.journey import (
    build_journey_from_messages,
    build_journey_from_parsed,
)
from odyssey.primitives import (
    Journey,
    Message,
    ParsedConversation,
    ParsedTurn,
    Reward,
    RewardComponent,
    Step,
    Task,
    Telemetry,
    ToolCall,
    ToolResponse,
)


def _make_parsed(messages_per_turn: list[list[tuple[str, str]]]) -> ParsedConversation:
    turns = []
    for i, msgs in enumerate(messages_per_turn):
        turns.append(
            ParsedTurn(
                messages=[Message(role=r, content=c) for r, c in msgs],
                source_run_id=f"trace-{i}",
                start_time=f"2025-01-01T00:0{i}:00Z",
                end_time=f"2025-01-01T00:0{i}:30Z",
                token_counts={
                    "total_tokens": 50,
                    "prompt_tokens": 30,
                    "completion_tokens": 20,
                },
            )
        )
    return ParsedConversation(
        conversation_id="conv-builder-test",
        data_source="test",
        num_turns=len(turns),
        turns=turns,
    )


def test_build_from_parsed_simple():
    parsed = _make_parsed([[("user", "Hello"), ("assistant", "Hi!")]])
    traj = build_journey_from_parsed(parsed)

    assert traj.task.conversation_id == "conv-builder-test"
    assert traj.trace_id is None
    assert traj.task.data_source == "test"
    assert traj.task.num_turns == 1
    assert len(traj.steps) > 0
    assert traj.error is None


def test_build_from_messages_sets_model_id():
    traj = build_journey_from_messages(
        [
            Message(role="user", content="hi"),
            Message(role="assistant", content="hello"),
        ],
        conversation_id="conv-model",
        data_source="unit-test",
        model_id="model_internal_123",
    )

    assert traj.model_id == "model_internal_123"


def test_journey_positional_optional_args_remain_backward_compatible():
    task = Task(conversation_id="conv-1")
    steps = [Step(messages=[Message(role="user", content="hello")])]
    reward = Reward(aggregated_value=1.0)
    telemetry = Telemetry(source="legacy", data={"value": 1})

    traj = Journey(task, steps, reward, None, None, None, telemetry, 7, "err")

    assert traj.reward == reward
    assert traj.telemetry == telemetry
    assert traj.idx == 7
    assert traj.error == "err"
    assert traj.trace_id is None
    assert traj.model_id is None


def test_journey_model_id_keyword_does_not_shift_legacy_positionals():
    task = Task(conversation_id="conv-1")
    steps = [Step(messages=[Message(role="user", content="hello")])]

    traj = Journey(task, steps, model_id="model_internal_123")

    assert traj.model_id == "model_internal_123"
    assert traj.reward is None


def test_build_from_parsed_cumulative_steps():
    parsed = _make_parsed(
        [
            [("user", "Q1"), ("assistant", "A1")],
            [("user", "Q2"), ("assistant", "A2")],
        ]
    )
    traj = build_journey_from_parsed(parsed)
    last_step = traj.steps[-1]
    contents = [m.content for m in last_step.messages]
    assert {"Q1", "A1", "Q2", "A2"} <= set(contents)


def test_build_from_parsed_execution_metrics():
    parsed = _make_parsed([[("user", "Hi"), ("assistant", "Hello")]])
    traj = build_journey_from_parsed(parsed)
    assert traj.execution_metrics.total_time is not None
    assert traj.execution_metrics.total_time >= 0
    assert traj.execution_metrics.termination_reason == "ENV_DONE"


def test_build_from_parsed_error():
    turn = ParsedTurn(
        messages=[Message(role="user", content="crash")],
        source_run_id="trace-err",
        error="something broke",
        token_counts={},
    )
    parsed = ParsedConversation(
        conversation_id="conv-err",
        data_source="test",
        num_turns=1,
        turns=[turn],
    )
    traj = build_journey_from_parsed(parsed)
    assert traj.error == "something broke"
    assert traj.execution_metrics.termination_reason == "ERROR"


def test_build_from_parsed_telemetry():
    parsed = _make_parsed([[("user", "Hi"), ("assistant", "Hello")]])
    traj = build_journey_from_parsed(parsed)
    assert traj.telemetry is not None
    assert traj.telemetry.source == "test"
    assert "content_hash" in traj.telemetry.data
    assert "idempotency_key" in traj.telemetry.data
    assert traj.telemetry.data["conversation_id"] == "conv-builder-test"
    assert "trace_id" not in traj.telemetry.data


def test_build_from_parsed_uses_explicit_trace_id():
    parsed = _make_parsed([[("user", "Hi"), ("assistant", "Hello")]])
    parsed.trace_id = "trace-explicit"
    traj = build_journey_from_parsed(parsed)
    assert traj.trace_id == "trace-explicit"
    assert traj.telemetry.data["trace_id"] == "trace-explicit"


def test_build_from_parsed_tool_metrics():
    msgs = [
        Message(role="user", content="calc"),
        Message(
            role="assistant",
            content="calling tool",
            tool_calls=[ToolCall(name="calc", arguments={"x": 1})],
        ),
        Message(
            role="tool",
            content="42",
            tool_response=ToolResponse(
                id="tc1", name="calc", arguments={}, response="42"
            ),
        ),
        Message(
            role="assistant",
            content="calling broken tool",
            tool_calls=[ToolCall(name="broken", arguments={})],
        ),
        Message(
            role="tool",
            content="error",
            tool_response=ToolResponse(
                id="tc2", name="broken", arguments={}, error="fail"
            ),
        ),
        Message(role="assistant", content="done"),
    ]
    parsed = ParsedConversation(
        conversation_id="conv-tools",
        data_source="test",
        num_turns=1,
        turns=[ParsedTurn(messages=msgs, source_run_id="t1", token_counts={})],
    )
    traj = build_journey_from_parsed(parsed)
    assert traj.metrics.num_tool_calls == 2
    assert traj.metrics.num_tool_failures == 1
    assert traj.metrics.tool_error_rate == 0.5


def test_build_from_messages_basic():
    msgs = [
        Message(role="system", content="be helpful"),
        Message(role="user", content="hi"),
        Message(role="assistant", content="hello"),
    ]
    traj = build_journey_from_messages(
        msgs,
        conversation_id="conv-1",
        data_source="acme",
    )
    assert traj.task.conversation_id == "conv-1"
    assert traj.trace_id is None
    assert traj.task.data_source == "acme"
    assert traj.task.id == "acme:conv-1"
    assert traj.task.num_steps == len(traj.steps)
    assert traj.error is None
    assert traj.telemetry.source == "acme"
    assert traj.telemetry.data["conversation_id"] == "conv-1"
    assert "trace_id" not in traj.telemetry.data


def test_build_from_messages_uses_explicit_trace_id():
    traj = build_journey_from_messages(
        [
            Message(role="user", content="hi"),
            Message(role="assistant", content="hello"),
        ],
        conversation_id="conv-1",
        data_source="acme",
        trace_id="trace-1",
    )
    assert traj.trace_id == "trace-1"
    assert traj.telemetry.data["trace_id"] == "trace-1"


def test_build_from_messages_with_reward():
    reward = Reward(
        aggregated_value=0.5,
        aggregation_method="mean",
        components=[RewardComponent(name="quality", value=0.5, scaled_value=0.5)],
    )
    traj = build_journey_from_messages(
        [
            Message(role="user", content="hi"),
            Message(role="assistant", content="hello"),
        ],
        conversation_id="c",
        data_source="acme",
        reward=reward,
    )
    assert traj.reward is reward


def test_build_from_messages_timestamps():
    traj = build_journey_from_messages(
        [Message(role="user", content="hi"), Message(role="assistant", content="hi!")],
        conversation_id="c",
        data_source="acme",
        start_time="2025-01-01T00:00:00Z",
        end_time="2025-01-01T00:00:10Z",
    )
    assert traj.execution_metrics.total_time == 10.0
    assert traj.execution_metrics.termination_reason == "ENV_DONE"


def test_build_from_messages_error_sets_termination():
    traj = build_journey_from_messages(
        [Message(role="user", content="hi"), Message(role="assistant", content="x")],
        conversation_id="c",
        data_source="acme",
        error="boom",
    )
    assert traj.error == "boom"
    assert traj.execution_metrics.termination_reason == "ERROR"


def test_build_from_messages_extra_telemetry_merges():
    traj = build_journey_from_messages(
        [
            Message(role="user", content="hi"),
            Message(role="assistant", content="hello"),
        ],
        conversation_id="c",
        data_source="acme",
        extra_telemetry={"source_run_ids": ["r1", "r2"]},
    )
    assert traj.telemetry.data["source_run_ids"] == ["r1", "r2"]
    assert "content_hash" in traj.telemetry.data
    assert traj.telemetry.data["conversation_id"] == "c"


def test_content_hash_stable_across_builds():
    msgs = [
        Message(role="user", content="hi"),
        Message(role="assistant", content="hello"),
    ]
    a = build_journey_from_messages(msgs, conversation_id="c", data_source="acme")
    b = build_journey_from_messages(msgs, conversation_id="c", data_source="acme")
    assert a.telemetry.data["content_hash"] == b.telemetry.data["content_hash"]
