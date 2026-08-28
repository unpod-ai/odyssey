"""Tests for the message-format recipe book."""

from __future__ import annotations

import pytest

from odyssey.builders.messages import (
    messages_from_anthropic_messages,
    messages_from_openai_chat,
    messages_from_prompt_response,
    messages_from_role_content_pairs,
    messages_from_vercel_ai_sdk,
)


def test_openai_simple_chat():
    raw = [
        {"role": "system", "content": "be helpful"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello", "finish_reason": "stop"},
    ]
    msgs = messages_from_openai_chat(raw)
    assert [m.role for m in msgs] == ["system", "user", "assistant"]
    assert msgs[-1].finish_reason == "stop"
    assert msgs[0].content == "be helpful"


def test_openai_multimodal_content_flattens_to_text():
    raw = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe this"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,..."},
                },
                {"type": "text", "text": "in detail"},
            ],
        }
    ]
    msgs = messages_from_openai_chat(raw)
    assert msgs[0].content == "describe this\nin detail"


def test_openai_tool_calls_modern_format():
    raw = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"id": 42}'},
                }
            ],
        }
    ]
    msgs = messages_from_openai_chat(raw)
    assert msgs[0].tool_calls is not None
    assert msgs[0].tool_calls[0].name == "lookup"
    assert msgs[0].tool_calls[0].arguments == {"id": 42}
    assert msgs[0].tool_calls[0].id == "call_1"


def test_openai_legacy_function_call():
    raw = [
        {
            "role": "assistant",
            "content": None,
            "function_call": {"name": "search", "arguments": '{"q": "cats"}'},
        }
    ]
    msgs = messages_from_openai_chat(raw)
    assert msgs[0].tool_calls is not None
    assert msgs[0].tool_calls[0].name == "search"
    assert msgs[0].tool_calls[0].arguments == {"q": "cats"}


def test_openai_legacy_function_response_without_tool_call_id():
    """Legacy ChatCompletions ``role: "function"`` follow-up carries only
    ``name`` + ``content``; tool_call_id didn't exist in the pre-parallel-
    tool-calls API. This must ingest, not raise.
    """
    raw = [
        {
            "role": "assistant",
            "content": None,
            "function_call": {"name": "search", "arguments": '{"q": "cats"}'},
        },
        {
            "role": "function",
            "name": "search",
            "content": "found: Felis catus",
        },
    ]
    msgs = messages_from_openai_chat(raw)
    assert [m.role for m in msgs] == ["assistant", "tool"]
    assert msgs[1].tool_response is not None
    assert msgs[1].tool_response.name == "search"
    assert msgs[1].tool_response.id == ""
    assert msgs[1].tool_response.response == "found: Felis catus"


def test_openai_legacy_function_roundtrip_builds_journey():
    """End-to-end: assistant function_call → function response without
    tool_call_id survives build_journey_from_messages.
    """
    from odyssey.builders.journey import build_journey_from_messages

    raw = [
        {"role": "user", "content": "search for cats"},
        {
            "role": "assistant",
            "content": None,
            "function_call": {"name": "search", "arguments": '{"q": "cats"}'},
        },
        {"role": "function", "name": "search", "content": "found: Felis catus"},
        {"role": "assistant", "content": "Cats are Felis catus."},
    ]
    journey = build_journey_from_messages(
        messages=messages_from_openai_chat(raw),
        conversation_id="legacy_fn_conv",
        data_source="langsmith_legacy_function_call",
    )
    metrics = journey.metrics
    assert metrics is not None
    assert metrics.num_tool_calls == 1
    assert metrics.num_tool_failures == 0
    # One exchange -- ask, look up, answer -- so one cumulative step holding all
    # four messages.
    assert len(journey.steps) == 1
    assert len(journey.steps[0].messages) == 4


def test_openai_tool_response_role():
    raw = [
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "lookup",
            "content": "42",
        }
    ]
    msgs = messages_from_openai_chat(raw)
    assert msgs[0].role == "tool"
    assert msgs[0].tool_response is not None
    assert msgs[0].tool_response.name == "lookup"
    assert msgs[0].tool_response.id == "call_1"
    assert msgs[0].tool_response.response == "42"


def test_openai_role_normalization():
    raw = [
        {"role": "human", "content": "hi"},
        {"role": "ai", "content": "hello"},
        {"role": "chatbot", "content": "again"},
    ]
    msgs = messages_from_openai_chat(raw)
    assert [m.role for m in msgs] == ["user", "assistant", "assistant"]


def test_openai_bad_arguments_string_raises():
    raw = [
        {
            "role": "assistant",
            "tool_calls": [
                {"function": {"name": "weird", "arguments": "not json at all"}},
            ],
        }
    ]
    with pytest.raises(ValueError, match="not valid JSON"):
        messages_from_openai_chat(raw)


def test_openai_non_dict_entry_raises():
    with pytest.raises(TypeError, match="must be a dict"):
        # pyrefly: ignore[bad-argument-type]  — deliberately the wrong shape,
        # exactly what this test exists to prove is rejected.
        messages_from_openai_chat([{"role": "user", "content": "ok"}, "not a dict"])


def test_openai_tool_call_missing_name_raises():
    raw = [
        {
            "role": "assistant",
            "tool_calls": [{"function": {"arguments": "{}"}}],
        }
    ]
    with pytest.raises(ValueError, match="missing 'name'"):
        messages_from_openai_chat(raw)


def test_openai_modern_tool_role_missing_id_raises():
    """Modern ``role: "tool"`` entries must carry ``tool_call_id`` -- that's
    how OpenAI links the tool response back to the assistant's parallel
    tool_call. Legacy ``role: "function"`` is handled separately (see
    test_openai_legacy_function_response_without_tool_call_id).
    """
    raw = [{"role": "tool", "name": "x", "content": "42"}]
    with pytest.raises(ValueError, match="tool_call_id"):
        messages_from_openai_chat(raw)


def test_openai_multimodal_unknown_block_type_raises():
    raw = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe"},
                {"type": "mystery_block", "payload": 1},
            ],
        }
    ]
    with pytest.raises(ValueError, match="unsupported type"):
        messages_from_openai_chat(raw)


def test_anthropic_text_blocks():
    raw = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "text", "text": "world"},
            ],
            "stop_reason": "end_turn",
        },
    ]
    msgs = messages_from_anthropic_messages(raw)
    assert msgs[0].content == "hi"
    assert msgs[1].content == "hello\nworld"
    assert msgs[1].finish_reason == "end_turn"


def test_anthropic_tool_use_and_result():
    raw = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "let me check"},
                {
                    "type": "tool_use",
                    "id": "tu_1",
                    "name": "search",
                    "input": {"q": "x"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "found: x=5"},
            ],
        },
    ]
    msgs = messages_from_anthropic_messages(raw)
    assert [m.role for m in msgs] == ["assistant", "tool"]
    assert msgs[0].content == "let me check"
    assert msgs[0].tool_calls is not None
    assert msgs[0].tool_calls[0].name == "search"
    assert msgs[0].tool_calls[0].id == "tu_1"
    assert msgs[0].tool_calls[0].arguments == {"q": "x"}

    # Tool-result block emitted as a separate ``role="tool"`` message so
    # downstream step builders and failure metrics see it.
    assert msgs[1].role == "tool"
    assert msgs[1].tool_response is not None
    assert msgs[1].tool_response.id == "tu_1"
    assert msgs[1].tool_response.response == "found: x=5"
    assert msgs[1].tool_response.error is None


def test_anthropic_tool_result_with_error_flag():
    raw = [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_2",
                    "content": "boom",
                    "is_error": True,
                }
            ],
        }
    ]
    msgs = messages_from_anthropic_messages(raw)
    assert msgs[0].role == "tool"
    assert msgs[0].tool_response is not None
    assert msgs[0].tool_response.error == "tool_error"


def test_anthropic_parallel_tool_results_all_preserved():
    """Multiple ``tool_result`` blocks in one user message each become their
    own ``role="tool"`` Message. Regression guard for the bug where only the
    last result survived because ``tool_response`` was overwritten.
    """
    raw = [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "result 1"},
                {"type": "tool_result", "tool_use_id": "tu_2", "content": "result 2"},
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_3",
                    "content": "boom",
                    "is_error": True,
                },
            ],
        }
    ]
    msgs = messages_from_anthropic_messages(raw)
    assert len(msgs) == 3
    assert all(m.role == "tool" for m in msgs)
    assert all(m.tool_response is not None for m in msgs)
    assert [m.tool_response.id for m in msgs if m.tool_response] == [
        "tu_1",
        "tu_2",
        "tu_3",
    ]
    assert [m.tool_response.response for m in msgs if m.tool_response] == [
        "result 1",
        "result 2",
        "boom",
    ]
    assert msgs[2].tool_response is not None
    assert msgs[2].tool_response.error == "tool_error"


def test_anthropic_mixed_text_and_tool_results_in_one_message():
    raw = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "please also..."},
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "r1"},
                {"type": "tool_result", "tool_use_id": "tu_2", "content": "r2"},
            ],
            "usage": {"input_tokens": 5},
        }
    ]
    msgs = messages_from_anthropic_messages(raw)
    # Text becomes a user message, each tool_result becomes its own tool message.
    assert [m.role for m in msgs] == ["user", "tool", "tool"]
    assert msgs[0].content == "please also..."
    # Usage/finish_reason attach to the primary message, not the tool ones.
    assert msgs[0].usage == {"input_tokens": 5}
    assert msgs[1].usage is None
    assert msgs[2].usage is None


def test_anthropic_assistant_parallel_tool_use():
    raw = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu_1",
                    "name": "search",
                    "input": {"q": "a"},
                },
                {
                    "type": "tool_use",
                    "id": "tu_2",
                    "name": "search",
                    "input": {"q": "b"},
                },
            ],
        }
    ]
    msgs = messages_from_anthropic_messages(raw)
    assert len(msgs) == 1
    assert msgs[0].role == "assistant"
    assert msgs[0].tool_calls is not None
    assert len(msgs[0].tool_calls) == 2
    assert [tc.id for tc in msgs[0].tool_calls] == ["tu_1", "tu_2"]


def test_vercel_core_message_string_content():
    raw = [
        {"role": "system", "content": "be helpful"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    msgs = messages_from_vercel_ai_sdk(raw)
    assert [m.role for m in msgs] == ["system", "user", "assistant"]
    assert msgs[2].content == "hello"


def test_vercel_ui_message_parts():
    raw = [
        {
            "role": "user",
            "parts": [{"type": "text", "text": "search for cats"}],
        },
        {
            "role": "assistant",
            "parts": [
                {"type": "reasoning", "text": "I should call the search tool"},
                {
                    "type": "tool-invocation",
                    "toolInvocation": {
                        "toolCallId": "call_1",
                        "toolName": "search",
                        "args": {"q": "cats"},
                    },
                },
            ],
        },
    ]
    msgs = messages_from_vercel_ai_sdk(raw)
    assert msgs[0].content == "search for cats"
    assert msgs[1].reasoning == "I should call the search tool"
    assert msgs[1].tool_calls is not None
    assert msgs[1].tool_calls[0].name == "search"
    assert msgs[1].tool_calls[0].id == "call_1"
    assert msgs[1].tool_calls[0].arguments == {"q": "cats"}


def test_vercel_tool_result_expands_to_tool_role():
    raw = [
        {
            "role": "tool",
            "parts": [
                {
                    "type": "tool-result",
                    "toolCallId": "call_1",
                    "toolName": "search",
                    "result": "found: cats",
                }
            ],
        }
    ]
    msgs = messages_from_vercel_ai_sdk(raw)
    assert msgs[0].role == "tool"
    assert msgs[0].tool_response is not None
    assert msgs[0].tool_response.id == "call_1"
    assert msgs[0].tool_response.response == "found: cats"
    assert msgs[0].tool_response.error is None


def test_vercel_structured_dict_result_is_json_encoded():
    """Vercel tool results are commonly structured objects. They must NOT be
    routed through flatten_text_content (which would reject dicts without a
    'text' key); they must be preserved as a JSON string on
    ToolResponse.response.
    """
    raw = [
        {
            "role": "tool",
            "parts": [
                {
                    "type": "tool-result",
                    "toolCallId": "c1",
                    "toolName": "get_weather",
                    "result": {"temp": 72, "units": "F"},
                }
            ],
        }
    ]
    msgs = messages_from_vercel_ai_sdk(raw)
    assert msgs[0].role == "tool"
    # Full payload preserved as JSON; key order is sort_keys=True.
    assert msgs[0].tool_response is not None
    assert msgs[0].tool_response.response == '{"temp": 72, "units": "F"}'


def test_vercel_numeric_result_is_stringified():
    raw = [
        {
            "role": "tool",
            "parts": [
                {"type": "tool-result", "toolCallId": "c1", "result": 42},
            ],
        }
    ]
    msgs = messages_from_vercel_ai_sdk(raw)
    assert msgs[0].tool_response is not None
    assert msgs[0].tool_response.response == "42"


def test_vercel_list_result_is_json_encoded():
    raw = [
        {
            "role": "tool",
            "parts": [
                {"type": "tool-result", "toolCallId": "c1", "result": [1, 2, 3]},
            ],
        }
    ]
    msgs = messages_from_vercel_ai_sdk(raw)
    assert msgs[0].tool_response is not None
    assert msgs[0].tool_response.response == "[1, 2, 3]"


def test_anthropic_structured_dict_tool_result_is_json_encoded():
    raw = [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_1",
                    "content": {"temp": 72, "units": "F"},
                }
            ],
        }
    ]
    msgs = messages_from_anthropic_messages(raw)
    assert msgs[0].role == "tool"
    assert msgs[0].tool_response is not None
    assert msgs[0].tool_response.response == '{"temp": 72, "units": "F"}'


def test_vercel_parallel_tool_results_each_become_tool_message():
    raw = [
        {
            "role": "tool",
            "parts": [
                {"type": "tool-result", "toolCallId": "c1", "result": "a"},
                {
                    "type": "tool-result",
                    "toolCallId": "c2",
                    "result": "b",
                    "isError": True,
                },
            ],
        }
    ]
    msgs = messages_from_vercel_ai_sdk(raw)
    assert [m.role for m in msgs] == ["tool", "tool"]
    assert all(m.tool_response is not None for m in msgs)
    assert [m.tool_response.id for m in msgs if m.tool_response] == ["c1", "c2"]
    assert msgs[1].tool_response is not None
    assert msgs[1].tool_response.error == "tool_error"


def test_vercel_rejects_both_content_and_parts():
    raw = [{"role": "user", "content": "a", "parts": [{"type": "text", "text": "b"}]}]
    with pytest.raises(ValueError, match="only one of 'content' or 'parts'"):
        messages_from_vercel_ai_sdk(raw)


def test_vercel_unknown_part_type_raises():
    raw = [{"role": "user", "parts": [{"type": "mystery"}]}]
    with pytest.raises(ValueError, match="unsupported type"):
        messages_from_vercel_ai_sdk(raw)


def test_vercel_tool_invocation_missing_name_raises():
    raw = [
        {
            "role": "assistant",
            "parts": [
                {"type": "tool-invocation", "toolInvocation": {"toolCallId": "c"}}
            ],
        }
    ]
    with pytest.raises(ValueError, match="missing 'toolName'"):
        messages_from_vercel_ai_sdk(raw)


def test_prompt_response_two_turn():
    msgs = messages_from_prompt_response("what is 2+2?", "4")
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[0].content == "what is 2+2?"
    assert msgs[1].content == "4"


def test_prompt_response_with_system():
    msgs = messages_from_prompt_response("hi", "hello", system="be helpful")
    assert [m.role for m in msgs] == ["system", "user", "assistant"]


def test_role_content_pairs_normalizes_roles():
    msgs = messages_from_role_content_pairs(
        [("human", "hi"), ("ai", "hello"), ("user", "more")]
    )
    assert [m.role for m in msgs] == ["user", "assistant", "user"]
