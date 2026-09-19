"""AWS Bedrock capture: Converse, ConverseStream and the InvokeModel pair.

No botocore install: the patch targets ``BaseClient._make_api_call``, so a fake
module exposing that class is the whole harness — and proves the integration
never imports boto3 until something asks it to patch.

The shapes replayed here are AWS's own (``modelId``, ``inferenceConfig``,
``toolUse``/``toolResult``, ``contentBlockDelta``, ``metadata.usage``), because
every bug this file exists to catch is a spelling mismatch with them.
"""

from __future__ import annotations

import asyncio
import json
import types
from typing import Any, Dict, List

import pytest

import odyssey
from odyssey.primitives import JourneyEvent, Message


@pytest.fixture(autouse=True)
def _clean():
    odyssey.shutdown()
    yield
    from odyssey.integrations import bedrock

    bedrock.uninstrument()
    odyssey.shutdown()


def start(tmp_path, **kw):
    kw.setdefault("instrument", "none")
    return odyssey.init(
        spool_dir=tmp_path / "spool",
        out_dir=tmp_path / "out",
        drain_interval=None,
        **kw,
    )


def client() -> Any:
    c = odyssey.get_client()
    assert c is not None
    return c


def turns(jid: str) -> List[JourneyEvent]:
    return [e for e in client().spool.read(jid) if e.kind == "message" and e.message]


def msg(event: JourneyEvent) -> Message:
    assert event.message is not None
    return event.message


def meta(event: JourneyEvent) -> Dict[str, Any]:
    return event.metadata or {}


def roles(jid: str) -> List[str]:
    return [msg(e).role for e in turns(jid)]


def reply(jid: str) -> JourneyEvent:
    replies = [e for e in turns(jid) if meta(e).get("direction") == "response"]
    assert replies, f"no response turn in {jid}"
    return replies[-1]


def journey_ids() -> List[str]:
    return sorted(client().spool.journey_ids())


# --------------------------------------------------------------------------
# A fake botocore
# --------------------------------------------------------------------------


class _ServiceModel:
    def __init__(self, name):
        self.service_name = name


class _Meta:
    def __init__(self, name):
        self.service_model = _ServiceModel(name)


def boto_module() -> Any:
    """A stand-in for ``botocore.client`` and ``aiobotocore.client``."""
    module = types.ModuleType("fake_botocore_client")

    class BaseClient:
        def __init__(self, script=None, service="bedrock-runtime"):
            self.meta = _Meta(service)
            self._script = list(script or [])

        def _make_api_call(self, operation_name, api_params):
            if not self._script:
                raise AssertionError(f"no scripted reply for {operation_name}")
            item = self._script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item

        # boto3 generates these from the service model; shape is what matters.
        def converse(self, **params):
            return self._make_api_call("Converse", params)

        def converse_stream(self, **params):
            return self._make_api_call("ConverseStream", params)

        def invoke_model(self, **params):
            return self._make_api_call("InvokeModel", params)

        def invoke_model_with_response_stream(self, **params):
            return self._make_api_call("InvokeModelWithResponseStream", params)

        def list_buckets(self, **params):
            return self._make_api_call("ListBuckets", params)

    class AioBaseClient(BaseClient):
        async def _make_api_call(self, operation_name, api_params):  # type: ignore[override]
            return BaseClient._make_api_call(self, operation_name, api_params)

        async def converse(self, **params):  # type: ignore[override]
            return await self._make_api_call("Converse", params)

        async def converse_stream(self, **params):  # type: ignore[override]
            return await self._make_api_call("ConverseStream", params)

    module.BaseClient = BaseClient  # type: ignore[attr-defined]
    module.AioBaseClient = AioBaseClient  # type: ignore[attr-defined]
    return module


def patch_boto() -> Any:
    from odyssey.integrations.bedrock import instrument

    module = boto_module()
    instrument(module, async_target=module)
    return module


class AsyncEvents:
    """Shaped like an aiobotocore event stream."""

    def __init__(self, items):
        self._items = list(items)

    async def _gen(self):
        for item in self._items:
            yield item

    def __aiter__(self):
        return self._gen()


class Body:
    """Shaped like ``botocore.response.StreamingBody``: reads once."""

    def __init__(self, payload):
        self._data = json.dumps(payload).encode()
        self.reads = 0

    def read(self, amt=None):
        self.reads += 1
        data, self._data = self._data, b""
        return data


USER = [{"role": "user", "content": [{"text": "what are your hours?"}]}]


def converse_reply(text="9 to 5", **extra) -> Dict[str, Any]:
    return {
        "ResponseMetadata": {"RequestId": "req-1"},
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 8, "outputTokens": 3, "totalTokens": 11},
        "metrics": {"latencyMs": 120},
        **extra,
    }


# --------------------------------------------------------------------------
# Converse
# --------------------------------------------------------------------------


def test_a_converse_call_is_recorded_as_two_turns(tmp_path):
    start(tmp_path)
    boto = patch_boto()
    aws = boto.BaseClient([converse_reply()])

    with odyssey.journey(id="j"):
        result = aws.converse(modelId="anthropic.claude-3-5-sonnet-v1:0", messages=USER)

    assert result["stopReason"] == "end_turn", "the response is passed through"
    assert roles("j") == ["user", "assistant"]
    got = reply("j")
    assert msg(got).content == "9 to 5"
    assert msg(got).finish_reason == "end_turn"
    assert msg(got).usage == {"input_tokens": 8, "output_tokens": 3, "total_tokens": 11}
    assert msg(got).provider == "bedrock"
    assert msg(got).latency_ms is not None
    assert got.model_id == "anthropic.claude-3-5-sonnet-v1:0"
    assert meta(got)["provider_message_id"] == "req-1"


def test_the_system_prompt_and_inference_config_are_recorded(tmp_path):
    start(tmp_path)
    boto = patch_boto()
    aws = boto.BaseClient([converse_reply(), converse_reply("still 9 to 5")])

    with odyssey.journey(id="j"):
        for _ in range(2):
            aws.converse(
                modelId="m",
                messages=USER,
                system=[{"text": "you book slots"}],
                inferenceConfig={"maxTokens": 512, "temperature": 0.2, "topP": 0.9},
            )

    assert (
        roles("j").count("system") == 1
    ), "the system prompt is resent, not re-recorded"
    asked = turns("j")[0]
    assert msg(asked).role == "system"
    params = meta(turns("j")[1])["params"]
    assert params == {"max_tokens": 512, "temperature": 0.2, "top_p": 0.9}


def test_a_tool_call_and_its_result_survive_the_round_trip(tmp_path):
    start(tmp_path)
    boto = patch_boto()
    answer = converse_reply()
    answer["output"]["message"]["content"] = [
        {
            "toolUse": {
                "toolUseId": "tu_1",
                "name": "check_slots",
                "input": {"day": "tuesday"},
            }
        }
    ]
    answer["stopReason"] = "tool_use"
    boto_client = boto.BaseClient([answer, converse_reply("tuesday at 3 works")])

    with odyssey.journey(id="j"):
        boto_client.converse(modelId="m", messages=USER)
        boto_client.converse(
            modelId="m",
            messages=[
                *USER,
                {
                    "role": "assistant",
                    "content": [
                        {
                            "toolUse": {
                                "toolUseId": "tu_1",
                                "name": "check_slots",
                                "input": {"day": "tuesday"},
                            }
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "toolResult": {
                                "toolUseId": "tu_1",
                                "content": [{"text": "3pm free"}],
                                "status": "success",
                            }
                        }
                    ],
                },
            ],
        )

    called = [e for e in turns("j") if msg(e).tool_calls]
    assert [c.name for c in msg(called[0]).tool_calls or []] == ["check_slots"]
    assert (msg(called[0]).tool_calls or [])[0].arguments == {"day": "tuesday"}
    results = [e for e in turns("j") if msg(e).role == "tool"]
    answered = msg(results[0]).tool_response
    assert answered is not None and answered.response == "3pm free"


def test_a_tool_definition_is_recorded_once(tmp_path):
    start(tmp_path)
    boto = patch_boto()
    aws = boto.BaseClient([converse_reply()])
    tools = {
        "tools": [
            {
                "toolSpec": {
                    "name": "check_slots",
                    "description": "free slots for a day",
                    "inputSchema": {"json": {"type": "object"}},
                }
            }
        ]
    }

    with odyssey.journey(id="j"):
        aws.converse(modelId="m", messages=USER, toolConfig=tools)

    defined = [e for e in turns("j") if msg(e).tool_definitions]
    assert [d.name for d in msg(defined[0]).tool_definitions or []] == ["check_slots"]
    assert (msg(defined[0]).tool_definitions or [])[0].parameters == {"type": "object"}


def test_a_content_block_bedrock_added_is_named_not_dropped(tmp_path):
    """An image block costs its turn if the parser refuses the whole message."""
    start(tmp_path)
    boto = patch_boto()
    aws = boto.BaseClient([converse_reply()])

    with odyssey.journey(id="j"):
        aws.converse(
            modelId="m",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"image": {"format": "png"}},
                        {"text": "what is this?"},
                    ],
                }
            ],
        )

    asked = turns("j")[0]
    assert msg(asked).content == "what is this?"
    assert meta(asked)["unknown_blocks"] == ["image"]


def test_reasoning_is_kept_off_the_answer(tmp_path):
    start(tmp_path)
    boto = patch_boto()
    answer = converse_reply()
    answer["output"]["message"]["content"] = [
        {"reasoningContent": {"reasoningText": {"text": "they asked about hours"}}},
        {"text": "9 to 5"},
    ]
    aws = boto.BaseClient([answer])

    with odyssey.journey(id="j"):
        aws.converse(modelId="m", messages=USER)

    assert msg(reply("j")).content == "9 to 5"
    assert msg(reply("j")).reasoning == "they asked about hours"


# --------------------------------------------------------------------------
# ConverseStream
# --------------------------------------------------------------------------


def stream_events(text="9 to 5"):
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"delta": {"text": text[:3]}, "contentBlockIndex": 0}},
        {"contentBlockDelta": {"delta": {"text": text[3:]}, "contentBlockIndex": 0}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "end_turn"}},
        {
            "metadata": {
                "usage": {"inputTokens": 8, "outputTokens": 3, "totalTokens": 11},
                "metrics": {"latencyMs": 120},
            }
        },
    ]


def test_a_converse_stream_is_one_turn_once_drained(tmp_path):
    """LiveKit's and Pipecat's Bedrock call shape."""
    start(tmp_path)
    boto = patch_boto()
    aws = boto.BaseClient([{"stream": stream_events()}])

    with odyssey.journey(id="j"):
        result = aws.converse_stream(modelId="m", messages=USER)
        assert roles("j") == ["user"], "nothing to record until it is drained"
        assert len(list(result["stream"])) == 6, "every event still reaches the caller"

    got = reply("j")
    assert msg(got).content == "9 to 5"
    assert msg(got).finish_reason == "end_turn"
    assert msg(got).usage == {"input_tokens": 8, "output_tokens": 3, "total_tokens": 11}
    assert msg(got).provider == "bedrock"
    assert msg(got).ttft_ms is not None and msg(got).latency_ms is not None
    assert meta(got)["streamed"] is True and "incomplete" not in meta(got)


def test_a_streamed_tool_call_is_assembled_from_partial_json(tmp_path):
    start(tmp_path)
    boto = patch_boto()
    events = [
        {"messageStart": {"role": "assistant"}},
        {
            "contentBlockStart": {
                "start": {"toolUse": {"toolUseId": "tu_1", "name": "check_slots"}},
                "contentBlockIndex": 0,
            }
        },
        {
            "contentBlockDelta": {
                "delta": {"toolUse": {"input": '{"day":'}},
                "contentBlockIndex": 0,
            }
        },
        {
            "contentBlockDelta": {
                "delta": {"toolUse": {"input": '"tuesday"}'}},
                "contentBlockIndex": 0,
            }
        },
        {"messageStop": {"stopReason": "tool_use"}},
    ]
    aws = boto.BaseClient([{"stream": events}])

    with odyssey.journey(id="j"):
        list(aws.converse_stream(modelId="m", messages=USER)["stream"])

    calls = msg(reply("j")).tool_calls or []
    assert [(c.name, c.arguments) for c in calls] == [
        ("check_slots", {"day": "tuesday"})
    ]


def test_a_stream_cut_off_early_records_what_arrived(tmp_path):
    """A barge-in cancels the reply mid-sentence."""
    start(tmp_path)
    boto = patch_boto()
    aws = boto.BaseClient([{"stream": stream_events()}])

    with odyssey.journey(id="j"):
        stream = aws.converse_stream(modelId="m", messages=USER)["stream"]
        it = iter(stream)
        next(it)
        next(it)
        stream.close()

    got = reply("j")
    assert msg(got).content == "9 t"
    assert meta(got)["incomplete"] is True


def test_streamed_reasoning_lands_on_reasoning(tmp_path):
    start(tmp_path)
    boto = patch_boto()
    events = [
        {"messageStart": {"role": "assistant"}},
        {
            "contentBlockDelta": {
                "delta": {"reasoningContent": {"text": "hours are posted"}},
                "contentBlockIndex": 0,
            }
        },
        {"contentBlockDelta": {"delta": {"text": "9 to 5"}, "contentBlockIndex": 1}},
        {"messageStop": {"stopReason": "end_turn"}},
    ]
    aws = boto.BaseClient([{"stream": events}])

    with odyssey.journey(id="j"):
        list(aws.converse_stream(modelId="m", messages=USER)["stream"])

    assert msg(reply("j")).content == "9 to 5"
    assert msg(reply("j")).reasoning == "hours are posted"


# --------------------------------------------------------------------------
# InvokeModel
# --------------------------------------------------------------------------


def test_an_anthropic_body_is_recorded_and_still_readable(tmp_path):
    start(tmp_path)
    boto = patch_boto()
    body = Body(
        {
            "id": "msg_1",
            "model": "claude",
            "role": "assistant",
            "content": [{"type": "text", "text": "9 to 5"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 8, "output_tokens": 3},
        }
    )
    aws = boto.BaseClient([{"body": body, "contentType": "application/json"}])

    with odyssey.journey(id="j"):
        result = aws.invoke_model(
            modelId="anthropic.claude-3-haiku-v1:0",
            body=json.dumps(
                {
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 256,
                    "messages": [{"role": "user", "content": "what are your hours?"}],
                }
            ),
        )
        answer = json.loads(result["body"].read())

    assert answer["content"][0]["text"] == "9 to 5", "the caller still gets the body"
    assert body.reads == 1, "the underlying stream is read exactly once"
    assert roles("j") == ["user", "assistant"]
    assert msg(reply("j")).content == "9 to 5"
    assert msg(reply("j")).provider == "bedrock"
    assert msg(reply("j")).usage == {"input_tokens": 8, "output_tokens": 3}


def test_a_text_completion_family_contributes_its_prompt_and_answer(tmp_path):
    """Titan's body is not the messages API; the turn is still a turn."""
    start(tmp_path)
    boto = patch_boto()
    body = Body({"results": [{"outputText": "9 to 5", "completionReason": "FINISH"}]})
    aws = boto.BaseClient([{"body": body}])

    with odyssey.journey(id="j"):
        aws.invoke_model(
            modelId="amazon.titan-text-express-v1",
            body=json.dumps({"inputText": "what are your hours?"}),
        )

    assert roles("j") == ["user", "assistant"]
    assert msg(turns("j")[0]).content == "what are your hours?"
    assert msg(reply("j")).content == "9 to 5"
    assert msg(reply("j")).finish_reason == "FINISH"


def test_an_invoke_stream_is_folded_into_one_turn(tmp_path):
    start(tmp_path)
    boto = patch_boto()
    events = [
        {
            "chunk": {
                "bytes": json.dumps(
                    {
                        "type": "message_start",
                        "message": {"id": "m1", "role": "assistant"},
                    }
                ).encode()
            }
        },
        {
            "chunk": {
                "bytes": json.dumps(
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "9 to 5"},
                    }
                ).encode()
            }
        },
        {
            "chunk": {
                "bytes": json.dumps(
                    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}
                ).encode()
            }
        },
    ]
    aws = boto.BaseClient([{"body": events}])

    with odyssey.journey(id="j"):
        result = aws.invoke_model_with_response_stream(
            modelId="anthropic.claude-3-haiku-v1:0",
            body=json.dumps({"messages": [{"role": "user", "content": "hours?"}]}),
        )
        assert len(list(result["body"])) == 3

    assert msg(reply("j")).content == "9 to 5"
    assert msg(reply("j")).finish_reason == "end_turn"
    assert meta(reply("j"))["streamed"] is True


def test_a_text_family_stream_is_folded_too(tmp_path):
    start(tmp_path)
    boto = patch_boto()
    events = [
        {"chunk": {"bytes": json.dumps({"outputText": "9 to "}).encode()}},
        {"chunk": {"bytes": json.dumps({"outputText": "5"}).encode()}},
        {"chunk": {"bytes": json.dumps({"completionReason": "FINISH"}).encode()}},
    ]
    aws = boto.BaseClient([{"body": events}])

    with odyssey.journey(id="j"):
        list(
            aws.invoke_model_with_response_stream(
                modelId="amazon.titan-text-express-v1",
                body=json.dumps({"inputText": "hours?"}),
            )["body"]
        )

    assert msg(reply("j")).content == "9 to 5"
    assert msg(reply("j")).finish_reason == "FINISH"


# --------------------------------------------------------------------------
# What the patch must leave alone
# --------------------------------------------------------------------------


def test_another_aws_service_is_not_touched(tmp_path):
    """The patch sits under every boto3 call in the process."""
    start(tmp_path)
    boto = patch_boto()
    s3 = boto.BaseClient([{"Buckets": []}], service="s3")

    with odyssey.journey(id="j"):
        assert s3.list_buckets() == {"Buckets": []}

    assert turns("j") == []


def test_an_operation_that_carries_no_conversation_is_not_touched(tmp_path):
    start(tmp_path)
    boto = patch_boto()
    aws = boto.BaseClient([{"modelSummaries": []}])

    with odyssey.journey(id="j"):
        assert aws._make_api_call("ListFoundationModels", {}) == {"modelSummaries": []}

    assert turns("j") == []


def test_a_provider_failure_propagates_and_records_the_request(tmp_path):
    start(tmp_path)
    boto = patch_boto()
    aws = boto.BaseClient([RuntimeError("throttled")])

    with pytest.raises(RuntimeError, match="throttled"):
        with odyssey.journey(id="j"):
            aws.converse(modelId="m", messages=USER)

    assert roles("j") == ["user"], "the prompt is in the corpus, the answer never came"


def test_a_malformed_request_does_not_break_the_call(tmp_path):
    """Reading a request must never be what takes the application down."""
    start(tmp_path)
    boto = patch_boto()
    aws = boto.BaseClient([converse_reply()])

    with odyssey.journey(id="j"):
        assert aws.converse(modelId="m", messages="not a list")["stopReason"]

    assert [msg(e).role for e in turns("j")] == ["assistant"]


def test_uninstrument_puts_the_original_back(tmp_path):
    from odyssey.integrations.bedrock import instrument, is_instrumented, uninstrument

    start(tmp_path)
    module = boto_module()
    original = module.BaseClient._make_api_call
    instrument(module, async_target=module)
    assert is_instrumented()
    uninstrument()
    assert not is_instrumented()
    assert module.BaseClient._make_api_call is original

    aws = module.BaseClient([converse_reply()])
    with odyssey.journey(id="j"):
        aws.converse(modelId="m", messages=USER)
    assert turns("j") == []


def test_instrumenting_twice_patches_once(tmp_path):
    from odyssey.integrations.bedrock import instrument

    start(tmp_path)
    module = boto_module()
    instrument(module, async_target=module)
    patched = module.BaseClient._make_api_call
    instrument(module, async_target=module)
    assert module.BaseClient._make_api_call is patched


# --------------------------------------------------------------------------
# aiobotocore
# --------------------------------------------------------------------------


def test_an_awaited_converse_is_recorded(tmp_path):
    start(tmp_path)
    boto = patch_boto()
    aws = boto.AioBaseClient([converse_reply()])

    async def main():
        with odyssey.journey(id="j"):
            await aws.converse(modelId="m", messages=USER)

    asyncio.run(main())
    assert roles("j") == ["user", "assistant"]
    assert msg(reply("j")).content == "9 to 5"


def test_an_awaited_stream_is_recorded_once_drained(tmp_path):
    start(tmp_path)
    boto = patch_boto()
    aws = boto.AioBaseClient([{"stream": AsyncEvents(stream_events())}])

    async def main():
        with odyssey.journey(id="j"):
            result = await aws.converse_stream(modelId="m", messages=USER)
            assert roles("j") == ["user"]
            return [event async for event in result["stream"]]

    assert len(asyncio.run(main())) == 6
    got = reply("j")
    assert msg(got).content == "9 to 5"
    assert msg(got).ttft_ms is not None
    assert meta(got)["streamed"] is True


def test_two_bedrock_clients_in_one_journey_keep_their_own_history(tmp_path):
    """A voice call's main model and its filler model share the `.llm` journey."""
    start(tmp_path)
    boto = patch_boto()
    main = boto.BaseClient([converse_reply("9 to 5"), converse_reply("tuesday at 3")])
    filler = boto.BaseClient([converse_reply("hmm")])

    with odyssey.journey(id="j"):
        main.converse(modelId="m", messages=USER)
        filler.converse(
            modelId="f", messages=[{"role": "user", "content": [{"text": "say hmm"}]}]
        )
        main.converse(
            modelId="m",
            messages=[
                *USER,
                {"role": "assistant", "content": [{"text": "9 to 5"}]},
                {"role": "user", "content": [{"text": "tuesday?"}]},
            ],
        )

    asked = [
        msg(e).content for e in turns("j") if meta(e).get("direction") == "request"
    ]
    assert asked == ["what are your hours?", "say hmm", "tuesday?"]
