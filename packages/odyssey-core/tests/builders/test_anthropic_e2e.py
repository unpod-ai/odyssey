"""End-to-end tests proving Anthropic tool_result handling feeds the
downstream builder/metrics pipeline correctly.

Regression guard for two bugs in the initial Anthropic parser:

1. ``tool_result`` blocks kept the original ``"user"`` role, so
   ``count_tool_failures`` (which keys on ``role == "tool"``) undercounted
   and ``build_cumulative_steps`` (which cuts steps on ``role == "tool"``)
   missed step boundaries.
2. Multiple ``tool_result`` blocks in one user message overwrote each
   other; only the last survived.
"""

from __future__ import annotations

from odyssey.builders.journey import build_journey_from_messages
from odyssey.builders.messages import messages_from_anthropic_messages
from odyssey.builders.metrics import count_tool_calls, count_tool_failures
from odyssey.builders.steps import build_cumulative_steps


def test_anthropic_tool_failures_counted_downstream():
    raw = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu_1", "name": "search", "input": {}},
                {"type": "tool_use", "id": "tu_2", "name": "search", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "ok"},
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_2",
                    "content": "boom",
                    "is_error": True,
                },
            ],
        },
    ]
    msgs = messages_from_anthropic_messages(raw)
    # 2 calls issued, both delivered, one errored
    assert count_tool_calls(msgs) == 2
    assert count_tool_failures(msgs) == 1


def test_anthropic_tool_results_create_step_boundaries():
    raw = [
        {"role": "user", "content": "calc"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu_1", "name": "calc", "input": {"x": 1}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "42"},
            ],
        },
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "the answer is 42"}],
        },
    ]
    msgs = messages_from_anthropic_messages(raw)
    # The role fix is what this guards: Anthropic ships a tool_result inside a
    # `user` message, and it must land as role `tool`. Read it off the message,
    # not off a step count -- the whole roundtrip is one turn, so one step.
    assert [m.role for m in msgs] == ["user", "assistant", "tool", "assistant"]
    steps = build_cumulative_steps(msgs)
    assert len(steps) == 1
    assert steps[-1].messages[-1].content == "the answer is 42"


def test_anthropic_journey_end_to_end_metrics():
    raw = [
        {"role": "user", "content": "work"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu_a", "name": "a", "input": {}},
                {"type": "tool_use", "id": "tu_b", "name": "b", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_a",
                    "content": "fail",
                    "is_error": True,
                },
                {"type": "tool_result", "tool_use_id": "tu_b", "content": "ok"},
            ],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]
    msgs = messages_from_anthropic_messages(raw)
    traj = build_journey_from_messages(
        msgs, conversation_id="conv-anthro", data_source="anthropic-e2e"
    )
    metrics = traj.metrics
    assert metrics is not None
    assert metrics.num_tool_calls == 2
    assert metrics.num_tool_failures == 1
    assert metrics.tool_error_rate == 0.5
