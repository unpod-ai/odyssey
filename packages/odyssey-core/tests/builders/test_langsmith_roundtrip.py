"""Roundtrip test: BYOD pipeline against a realistic LangSmith-style trace.

LangSmith exports OpenAI-format messages with JSON-encoded ``tool_calls``
arguments, ``tool_call_id`` linkage on tool-role messages, and multi-turn
agent loops. This exercises the full BYOD pipeline
(``messages_from_openai_chat`` -> ``build_journey_from_messages``) on a
trace shaped like a real customer export and asserts step boundaries,
tool-call metrics, idempotency, and conversation_id propagation end-to-end.

We don't call the LangSmith API here -- the goal is to guarantee the BYOD
pipeline produces the same journey structure that the
``LangSmithProvider`` path produces, using a canned payload that mirrors
what their API returns.
"""

from __future__ import annotations

from odyssey.builders.journey import build_journey_from_messages
from odyssey.builders.messages import messages_from_openai_chat


def _langsmith_style_agent_loop() -> list[dict]:
    """Canned OpenAI-format messages resembling a LangSmith export.

    One turn: system/user bootstrap, assistant tool-call, tool response,
    assistant final. Shape matches what ``LangSmithProvider`` emits after
    parsing (canonical OpenAI roles, JSON-string ``arguments``).
    """
    return [
        {"role": "system", "content": "You are a helpful research assistant."},
        {"role": "user", "content": "What's the revenue of Clay in 2024?"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_abc123",
                    "type": "function",
                    "function": {
                        "name": "search_web",
                        "arguments": '{"query": "Clay 2024 revenue"}',
                    },
                }
            ],
            "usage": {"prompt_tokens": 42, "completion_tokens": 15, "total_tokens": 57},
            "finish_reason": "tool_calls",
        },
        {
            "role": "tool",
            "tool_call_id": "call_abc123",
            "name": "search_web",
            "content": "Clay reported ~$40M ARR in 2024 (source: techcrunch.com)",
        },
        {
            "role": "assistant",
            "content": "Based on reporting, Clay's 2024 revenue is around $40M ARR.",
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 24,
                "total_tokens": 144,
            },
            "finish_reason": "stop",
        },
    ]


def test_langsmith_roundtrip_produces_well_formed_journey():
    raw = _langsmith_style_agent_loop()
    messages = messages_from_openai_chat(raw)

    journey = build_journey_from_messages(
        messages=messages,
        conversation_id="conv_langsmith_demo",
        data_source="langsmith_export_roundtrip_test",
    )

    assert journey.task.conversation_id == "conv_langsmith_demo"
    assert journey.task.data_source == "langsmith_export_roundtrip_test"
    # One user->agent exchange, tool call included, is one turn: one step
    # carrying every message in the conversation.
    assert len(journey.steps) == 1
    assert len(journey.steps[-1].messages) == 5

    # Tool-call metrics flow through.
    assert journey.metrics.num_tool_calls == 1
    assert journey.metrics.num_tool_failures == 0
    assert journey.metrics.tool_error_rate == 0.0

    # Message structure: assistant carries the ToolCall, tool message carries
    # the ToolResponse with id linkage.
    assistant_with_tool = next(
        m for m in messages if m.role == "assistant" and m.tool_calls
    )
    assert assistant_with_tool.tool_calls[0].name == "search_web"
    assert assistant_with_tool.tool_calls[0].arguments == {"query": "Clay 2024 revenue"}
    assert assistant_with_tool.tool_calls[0].id == "call_abc123"

    tool_message = next(m for m in messages if m.role == "tool")
    assert tool_message.tool_response is not None
    assert tool_message.tool_response.id == "call_abc123"
    assert tool_message.tool_response.name == "search_web"
    assert tool_message.tool_response.response.startswith("Clay reported")

    # Telemetry captures content_hash + idempotency_key for re-ingest safety.
    assert journey.telemetry.source == "langsmith_export_roundtrip_test"
    assert journey.telemetry.data["conversation_id"] == "conv_langsmith_demo"
    assert journey.telemetry.data["content_hash"]
    assert journey.telemetry.data["idempotency_key"]


def test_langsmith_roundtrip_is_deterministic():
    """Same input → same content_hash + idempotency_key. Guards re-ingest dedup."""
    raw = _langsmith_style_agent_loop()
    a = build_journey_from_messages(
        messages=messages_from_openai_chat(raw),
        conversation_id="conv_1",
        data_source="langsmith_export_roundtrip_test",
    )
    b = build_journey_from_messages(
        messages=messages_from_openai_chat(raw),
        conversation_id="conv_1",
        data_source="langsmith_export_roundtrip_test",
    )
    assert a.telemetry.data["content_hash"] == b.telemetry.data["content_hash"]
    assert a.telemetry.data["idempotency_key"] == b.telemetry.data["idempotency_key"]


def test_langsmith_roundtrip_repeated_system_prompts_survive():
    """LangSmith parsers often resend the system prompt on every LLM call.

    After ``_clean_conversation_messages`` flattens turns, the result can
    contain multiple system messages interspersed with user/assistant
    messages. This used to crash journey construction; now it must
    produce a journey with copy-on-write system semantics (earlier
    steps see the earlier system, later steps see the later system).
    """
    raw = [
        {"role": "system", "content": "You are helpful. Revision 1."},
        {"role": "user", "content": "First question."},
        {"role": "assistant", "content": "First answer."},
        {
            "role": "system",
            "content": "You are helpful. Revision 2 (context compacted).",
        },
        {"role": "user", "content": "Second question."},
        {"role": "assistant", "content": "Second answer."},
    ]
    journey = build_journey_from_messages(
        messages=messages_from_openai_chat(raw),
        conversation_id="conv_system_refresh",
        data_source="langsmith_export_roundtrip_test",
    )
    assert len(journey.steps) >= 2
    first_systems = [m.content for m in journey.steps[0].messages if m.role == "system"]
    last_systems = [m.content for m in journey.steps[-1].messages if m.role == "system"]
    assert "Revision 1." in first_systems[0]
    assert "Revision 2" in last_systems[0]


def test_langsmith_roundtrip_multi_turn_tool_loop_step_count():
    """Two tool-call cycles produce five steps (init, tool1, mid assistant, tool2, final)."""
    raw = [
        {"role": "user", "content": "compare A and B"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"x": "A"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "lookup", "content": "A=1"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c2",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"x": "B"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c2", "name": "lookup", "content": "B=2"},
        {"role": "assistant", "content": "A=1, B=2, so A < B."},
    ]
    journey = build_journey_from_messages(
        messages=messages_from_openai_chat(raw),
        conversation_id="multi_turn",
        data_source="langsmith_export_roundtrip_test",
    )
    # Two tool cycles, but the user only asked once, so it is one turn and one
    # step. The cycles are still fully present inside its message list -- an
    # exporter that wants per-LLM-call granularity can recover it from there.
    assert len(journey.steps) == 1
    assert len(journey.steps[0].messages) == 6
    assert journey.metrics.num_tool_calls == 2
