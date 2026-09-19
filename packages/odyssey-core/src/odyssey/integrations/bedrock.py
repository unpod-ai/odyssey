"""AWS Bedrock capture — LiveKit's ``aws.LLM``, Pipecat's ``AWSBedrockLLMService``.

Bedrock is not an SDK that can be wrapped the way ``openai`` and ``anthropic``
are. boto3 builds a client's methods at runtime from a service model, so there
is no ``Messages.create`` to patch and no class to subclass — but every one of
those generated methods funnels into
``botocore.client.BaseClient._make_api_call(operation_name, api_params)``. That
is the single seam this patches, and it filters on the way in: an operation
this module does not capture, or a client for any service other than
``bedrock-runtime``, is one dict lookup away from the untouched call. The patch
is process-wide and therefore sits under every boto3 call in the process —
S3, DynamoDB, SQS — which is why that fast path comes first and why nothing
here inspects a request it is not going to record.

Four operations carry a conversation:

``Converse`` / ``ConverseStream``
    The Converse API is Anthropic's messages API with AWS spellings
    (``modelId``, ``content: [{"text": ...}]``, ``toolUse``, ``stopReason``),
    and every model on Bedrock speaks it. Both are translated into the shapes
    ``integrations/_base.py`` already parses rather than parsed a second time
    here. These two are what LiveKit's and Pipecat's Bedrock services call.

``InvokeModel`` / ``InvokeModelWithResponseStream``
    The older per-model-family API: the body is a JSON blob in whatever format
    the family defines. The Anthropic family's *is* the messages API, so it
    passes straight through; the text-completion families (Titan, Llama,
    Cohere) contribute the prompt and the completion and nothing else, which is
    the whole turn they have.

A non-streamed ``InvokeModel`` response body is an HTTP stream that can only be
read once, so it is read here and replaced with a replayable one before
anything else happens — the caller still gets bytes, and the two statements
that do it cannot fail between them. Everything else is passed through
untouched, and a provider exception propagates unchanged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from odyssey.capture import _jsonable
from odyssey.client import require_client
from odyssey.integrations._base import (
    MessageAccumulator,
    capture_request,
    capture_response,
    record_stream,
)
from odyssey.integrations._call import Capture, capture_async, capture_sync
from odyssey.integrations._streams import ObservedAsyncStream, ObservedStream, OnEnd
from odyssey.integrations.providers import Provider

# What lands on `Message.provider`. Bedrock serves many model families, and
# which one answered is `model_id`; who served it is always Bedrock.
PROVIDER = "bedrock"

# The boto3 service whose calls are conversations. Every other service the
# process talks to goes through the same patched method and must not be read.
SERVICE = "bedrock-runtime"

# Set by instrument(); cleared by uninstrument(). Module-level because patching
# is a process-wide act and must be reversible exactly once.
_patched: Dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Converse: AWS spellings of the messages API
# ---------------------------------------------------------------------------

# Bedrock's inference parameters, in `_base._PARAM_KEYS` spelling.
_INFERENCE: Dict[str, str] = {
    "maxTokens": "max_tokens",
    "temperature": "temperature",
    "topP": "top_p",
    "stopSequences": "stop_sequences",
}

_USAGE: Dict[str, str] = {
    "inputTokens": "input_tokens",
    "outputTokens": "output_tokens",
    "totalTokens": "total_tokens",
}


def _content(blocks: Any) -> List[Dict[str, Any]]:
    """Converse content blocks → the blocks ``_base`` parses.

    A block this does not know becomes ``{"type": <its key>}``, which
    ``_base.split_blocks`` drops from the parse and names in the event
    metadata — an image or a document is visible as having been there rather
    than silently costing the turn it was part of.
    """
    out: List[Dict[str, Any]] = []
    for block in blocks if isinstance(blocks, (list, tuple)) else []:
        block = _jsonable(block)
        if not isinstance(block, dict):
            continue
        if "text" in block:
            out.append({"type": "text", "text": str(block.get("text") or "")})
        elif "toolUse" in block:
            use = block.get("toolUse") or {}
            out.append(
                {
                    "type": "tool_use",
                    "id": use.get("toolUseId"),
                    "name": use.get("name"),
                    "input": use.get("input") or {},
                }
            )
        elif "toolResult" in block:
            result = block.get("toolResult") or {}
            out.append(
                {
                    "type": "tool_result",
                    "tool_use_id": result.get("toolUseId"),
                    "content": _content(result.get("content")),
                    "is_error": result.get("status") == "error",
                }
            )
        elif "reasoningContent" in block:
            reasoning = (block.get("reasoningContent") or {}).get("reasoningText") or {}
            out.append({"type": "thinking", "thinking": reasoning.get("text") or ""})
        else:
            out.append({"type": next(iter(block), "unknown")})
    return out


def _system(system: Any) -> Optional[str]:
    """Converse's ``system`` is a list of blocks; ``_base`` wants the text."""
    parts: List[str] = []
    for block in system if isinstance(system, (list, tuple)) else []:
        block = _jsonable(block)
        if isinstance(block, dict) and block.get("text"):
            parts.append(str(block["text"]))
    return "\n".join(parts) or None


def _tools(tool_config: Any) -> Optional[List[Dict[str, Any]]]:
    if not isinstance(tool_config, dict):
        return None
    out: List[Dict[str, Any]] = []
    for tool in tool_config.get("tools") or []:
        spec = (_jsonable(tool) or {}).get("toolSpec")
        if not isinstance(spec, dict):
            continue
        schema = spec.get("inputSchema")
        out.append(
            {
                "name": spec.get("name"),
                "description": spec.get("description"),
                "input_schema": (
                    (schema or {}).get("json") if isinstance(schema, dict) else None
                ),
            }
        )
    return out or None


def converse_request(params: Dict[str, Any]) -> Dict[str, Any]:
    """``converse``/``converse_stream`` arguments → what ``_base`` records."""
    kwargs: Dict[str, Any] = {
        "model": params.get("modelId"),
        "messages": [
            {
                "role": entry.get("role") or "user",
                "content": _content(entry.get("content")),
            }
            for entry in (params.get("messages") or [])
            if isinstance(entry, dict)
        ],
        "system": _system(params.get("system")),
        "tools": _tools(params.get("toolConfig")),
    }
    inference = params.get("inferenceConfig")
    if isinstance(inference, dict):
        for aws_name, name in _INFERENCE.items():
            if inference.get(aws_name) is not None:
                kwargs[name] = inference[aws_name]
    return kwargs


def _converse_payload(response: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    message = (response.get("output") or {}).get("message")
    if not isinstance(message, dict):
        return None
    payload: Dict[str, Any] = {
        "role": message.get("role") or "assistant",
        "content": _content(message.get("content")),
        "stop_reason": response.get("stopReason"),
        "usage": _usage(response.get("usage")),
    }
    request_id = (response.get("ResponseMetadata") or {}).get("RequestId")
    if request_id:
        # Converse has no message id of its own; the request id is what an AWS
        # support ticket or a CloudWatch log is keyed on.
        payload["id"] = request_id
    return payload


def _usage(usage: Any) -> Optional[Dict[str, int]]:
    if not isinstance(usage, dict):
        return None
    out = {
        name: usage[aws_name]
        for aws_name, name in _USAGE.items()
        if isinstance(usage.get(aws_name), int)
    }
    return out or None


def converse_response(response: Any, **kw: Any) -> None:
    payload = _converse_payload(_jsonable(response) or {})
    if payload is not None:
        capture_response(payload, **kw)


class ConverseAccumulator(MessageAccumulator):
    """``converse_stream``'s events, in the spelling ``MessageAccumulator`` folds.

    The two streams describe the same thing — a message built block by block,
    a tool call's arguments arriving as partial JSON — so the events are
    renamed rather than accumulated a second time here.
    """

    def add(self, event: Any) -> bool:
        translated = _converse_event(_jsonable(event))
        self._start_reasoning(translated)
        return super().add(translated)

    def _start_reasoning(self, translated: Dict[str, Any]) -> None:
        """Open a reasoning block before its first delta lands in one.

        Converse announces a tool-use block (``contentBlockStart``) but never a
        reasoning one, and a block nobody typed defaults to text -- which would
        put the model's thinking into the answer it is trained on.
        """
        delta = translated.get("delta")
        if translated.get("type") != "content_block_delta":
            return
        if isinstance(delta, dict) and delta.get("type") == "thinking_delta":
            index = translated.get("index")
            self.blocks.setdefault(
                index if isinstance(index, int) else 0,
                {"type": "thinking", "thinking": ""},
            )


def _converse_event(event: Any) -> Dict[str, Any]:
    """One ``converse_stream`` event as its Anthropic equivalent, ``{}`` if none."""
    if not isinstance(event, dict):
        return {}
    if "messageStart" in event:
        role = (event["messageStart"] or {}).get("role") or "assistant"
        return {"type": "message_start", "message": {"role": role}}
    if "contentBlockStart" in event:
        start = (event["contentBlockStart"] or {}).get("start") or {}
        use = start.get("toolUse")
        if not isinstance(use, dict):
            return {}
        return {
            "type": "content_block_start",
            "index": _index(event["contentBlockStart"]),
            "content_block": {
                "type": "tool_use",
                "id": use.get("toolUseId"),
                "name": use.get("name"),
            },
        }
    if "contentBlockDelta" in event:
        body = event["contentBlockDelta"] or {}
        delta = _delta(body.get("delta"))
        if delta is None:
            return {}
        return {
            "type": "content_block_delta",
            "index": _index(body),
            "delta": delta,
        }
    if "messageStop" in event:
        stop = (event["messageStop"] or {}).get("stopReason")
        return {"type": "message_delta", "delta": {"stop_reason": stop}}
    if "metadata" in event:
        usage = _usage((event["metadata"] or {}).get("usage"))
        return {"type": "message_delta", "delta": {}, "usage": usage or {}}
    return {}


def _delta(delta: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(delta, dict):
        return None
    if "text" in delta:
        return {"type": "text_delta", "text": delta.get("text") or ""}
    if "toolUse" in delta:
        partial = (delta.get("toolUse") or {}).get("input") or ""
        return {"type": "input_json_delta", "partial_json": partial}
    if "reasoningContent" in delta:
        reasoning = delta.get("reasoningContent") or {}
        if "text" not in reasoning:
            # A signature or a redacted block: no text to fold in.
            return None
        return {"type": "thinking_delta", "thinking": reasoning.get("text") or ""}
    return None


def _index(body: Any) -> int:
    index = (body or {}).get("contentBlockIndex")
    return index if isinstance(index, int) else 0


# ---------------------------------------------------------------------------
# InvokeModel: one JSON body per model family
# ---------------------------------------------------------------------------

# Where a text-completion family puts its prompt, and its answer.
_PROMPT_KEYS = ("prompt", "inputText")
_COMPLETION_KEYS = ("completion", "outputText", "generation", "text")
_STOP_KEYS = ("stop_reason", "completionReason", "finish_reason")


def _body_json(body: Any) -> Dict[str, Any]:
    """A request or response body as a dict. ``{}`` for anything else."""
    if isinstance(body, (bytes, bytearray, str)):
        try:
            body = json.loads(body)
        except ValueError:
            return {}
    body = _jsonable(body)
    return body if isinstance(body, dict) else {}


def invoke_request(params: Dict[str, Any]) -> Dict[str, Any]:
    """``invoke_model`` arguments → what ``_base`` records.

    The Anthropic family's body is already the messages API, so it is used as
    it stands. A text-completion family's prompt is the user turn.
    """
    body = _body_json(params.get("body"))
    kwargs: Dict[str, Any] = {"model": params.get("modelId")}
    if isinstance(body.get("messages"), list):
        kwargs.update(body)
        kwargs["model"] = params.get("modelId") or body.get("model")
        return kwargs
    prompt = _first(body, _PROMPT_KEYS)
    kwargs["messages"] = (
        [{"role": "user", "content": str(prompt)}] if prompt is not None else []
    )
    return kwargs


def _first(body: Dict[str, Any], keys: Any) -> Optional[Any]:
    for key in keys:
        if body.get(key) is not None:
            return body[key]
    return None


def _invoke_payload(body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """A response body → an Anthropic-shaped message, whatever the family."""
    if body.get("content") is not None:
        return body
    # Titan nests its answer; Llama and Cohere put it at the top level.
    results = body.get("results") or body.get("generations")
    source = results[0] if isinstance(results, list) and results else body
    source = source if isinstance(source, dict) else body
    text = _first(source, _COMPLETION_KEYS)
    if text is None:
        return None
    return {
        "role": "assistant",
        "content": str(text),
        "stop_reason": _first(source, _STOP_KEYS) or _first(body, _STOP_KEYS),
    }


def invoke_response(response: Any, **kw: Any) -> None:
    """Record an ``invoke_model`` response, leaving its body readable.

    The body is an HTTP stream that reads once, so it is replaced with a
    replayable one *before* anything is parsed: between the read and the
    replacement there is nothing that can fail, and the caller reads the same
    bytes it would have.
    """
    if not isinstance(response, dict):
        return
    body = response.get("body")
    if body is None or not hasattr(body, "read"):
        return
    data = body.read()
    response["body"] = _replayable(data)
    payload = _invoke_payload(_body_json(data))
    if payload is not None:
        capture_response(payload, **kw)


def _replayable(data: Any) -> Any:
    """A body that reads like the one just consumed."""
    import io

    try:
        from botocore.response import StreamingBody  # type: ignore[import-not-found]

        return StreamingBody(io.BytesIO(data), len(data))
    except Exception:  # noqa: BLE001 - no botocore (a test, a shim) or a shape change
        return _Body(data)


class _Body:
    """The part of ``botocore.response.StreamingBody`` a caller uses."""

    def __init__(self, data: Any) -> None:
        self._data = data

    def read(self, amt: Optional[int] = None) -> Any:
        if amt is None:
            data, self._data = self._data, self._data[:0]
            return data
        data, self._data = self._data[:amt], self._data[amt:]
        return data

    def close(self) -> None:
        pass


class InvokeAccumulator(MessageAccumulator):
    """``invoke_model_with_response_stream``'s chunks, folded into one message.

    Each chunk is a JSON blob in the model family's own format: the Anthropic
    family's *is* an Anthropic stream event, and a text-completion family's
    carries the next piece of text.
    """

    def add(self, event: Any) -> bool:
        payload = _chunk_json(event)
        if payload.get("type"):
            return super().add(payload)
        stop = _first(payload, _STOP_KEYS)
        if stop is not None:
            self.stop_reason = str(stop)
        text = _first(payload, _COMPLETION_KEYS)
        if text is None:
            return False
        block = self.blocks.setdefault(0, {"type": "text", "text": ""})
        block["text"] = (block.get("text") or "") + str(text)
        return True


def _chunk_json(event: Any) -> Dict[str, Any]:
    """The JSON one streamed chunk carries.

    Read before ``_jsonable`` touches it: a chunk's payload is raw bytes, and
    the coercion that makes an arbitrary object safe to encode turns those into
    their ``repr``, which no longer parses.
    """
    if isinstance(event, dict):
        chunk = event.get("chunk")
        if isinstance(chunk, dict):
            return _body_json(chunk.get("bytes"))
    return _body_json(event)


# ---------------------------------------------------------------------------
# The patch
# ---------------------------------------------------------------------------


def _stream_under(key: str) -> Callable[[Any, Callable[[Any], None], OnEnd], Any]:
    """Observe the event stream a boto3 response carries under ``key``."""

    def observe(result: Any, on_chunk: Callable[[Any], None], on_end: OnEnd) -> Any:
        if not isinstance(result, dict):
            return None
        stream = result.get(key)
        if stream is None:
            return None
        if hasattr(stream, "__aiter__"):
            result[key] = ObservedAsyncStream(stream, on_chunk, on_end)
        elif hasattr(stream, "__iter__"):
            result[key] = ObservedStream(stream, on_chunk, on_end)
        else:
            return None
        return result

    return observe


def _provider(_resource: Any) -> Provider:
    return Provider(PROVIDER)


CONVERSE = Capture(
    label="bedrock",
    request=capture_request,
    response=converse_response,
    streamed=record_stream,
    accumulator=ConverseAccumulator,
    provider=_provider,
    observe=_stream_under("stream"),
)

INVOKE = Capture(
    label="bedrock",
    request=capture_request,
    response=invoke_response,
    streamed=record_stream,
    accumulator=InvokeAccumulator,
    provider=_provider,
    observe=_stream_under("body"),
)


@dataclass(frozen=True)
class _Operation:
    translate: Callable[[Dict[str, Any]], Dict[str, Any]]
    capture: Capture
    streaming: bool


_OPERATIONS: Dict[str, _Operation] = {
    "Converse": _Operation(converse_request, CONVERSE, False),
    "ConverseStream": _Operation(converse_request, CONVERSE, True),
    "InvokeModel": _Operation(invoke_request, INVOKE, False),
    "InvokeModelWithResponseStream": _Operation(invoke_request, INVOKE, True),
}


class _Resource:
    """What the shared capture keys a conversation's history on.

    One boto3 client, one running conversation: two Bedrock clients sharing a
    journey -- a voice call's main model and its filler model -- must not
    resync each other's message offsets into silence.
    """

    __slots__ = ("_client",)

    def __init__(self, client: Any) -> None:
        self._client = client


def _is_bedrock(client: Any) -> bool:
    service = getattr(getattr(client, "meta", None), "service_model", None)
    return getattr(service, "service_name", None) == SERVICE


def instrument(
    target: Optional[Any] = None, *, async_target: Optional[Any] = None
) -> None:
    """Patch botocore in place so every ``bedrock-runtime`` client records.

    Patches ``BaseClient._make_api_call`` — the one method every generated
    boto3 client method calls — and, when ``aiobotocore`` is importable, its
    async twin. Idempotent. ``target``/``async_target`` override the modules,
    which is what makes this testable without botocore installed; an explicit
    ``target`` patches only what it is given.
    """
    if _patched:
        return
    explicit = target is not None
    if target is None:
        import botocore.client as target  # type: ignore[no-redef]

    base = getattr(target, "BaseClient", None)
    if base is None or not hasattr(base, "_make_api_call"):
        raise AttributeError(
            "botocore.client.BaseClient._make_api_call not found; "
            "this botocore version is not supported by instrument()"
        )

    patches = [(base, base._make_api_call, False)]
    if async_target is None and not explicit:
        try:
            import aiobotocore.client as async_module  # type: ignore

            async_target = async_module
        except ImportError:
            async_target = None
    async_base = getattr(async_target, "AioBaseClient", None)
    if async_base is not None and hasattr(async_base, "_make_api_call"):
        patches.append((async_base, async_base._make_api_call, True))

    for cls, original, is_async in patches:
        cls._make_api_call = _patched_call(original, is_async=is_async)
    _patched["patches"] = patches


def _patched_call(original: Any, *, is_async: bool) -> Any:
    if is_async:

        async def patched_async(
            self: Any, operation_name: str, api_params: Any, *args: Any, **kwargs: Any
        ) -> Any:
            def call() -> Any:
                return original(self, operation_name, api_params, *args, **kwargs)

            captured = _captured(self, operation_name, api_params)
            if captured is None:
                return await call()
            op, request = captured
            return await capture_async(
                op.capture, _Resource(self), request, call, streaming=op.streaming
            )

        patched_async.__wrapped__ = original  # type: ignore[attr-defined]
        return patched_async

    def patched(
        self: Any, operation_name: str, api_params: Any, *args: Any, **kwargs: Any
    ) -> Any:
        def call() -> Any:
            return original(self, operation_name, api_params, *args, **kwargs)

        captured = _captured(self, operation_name, api_params)
        if captured is None:
            return call()
        op, request = captured
        return capture_sync(
            op.capture, _Resource(self), request, call, streaming=op.streaming
        )

    patched.__wrapped__ = original  # type: ignore[attr-defined]
    return patched


def _captured(
    client: Any, operation_name: str, api_params: Any
) -> Optional[Tuple[_Operation, Dict[str, Any]]]:
    """This call's capture and its translated request, or ``None`` to leave the
    call alone.

    Every boto3 call in the process arrives here, so all but four operations on
    one service are answered before anything is read. A request shape the
    translation cannot make sense of is counted and the call runs unrecorded:
    reading a request must never be what breaks it.
    """
    op = _OPERATIONS.get(operation_name)
    if op is None or not isinstance(api_params, dict) or not _is_bedrock(client):
        return None
    try:
        return op, op.translate(api_params)
    except Exception as exc:  # noqa: BLE001 - capture is best-effort by contract
        recorder = require_client()
        if recorder is not None:
            recorder.note_error("bedrock.request", exc)
        return None


def uninstrument() -> None:
    """Undo :func:`instrument`. Safe to call when nothing was patched."""
    for cls, original, _is_async in _patched.pop("patches", []):
        cls._make_api_call = original
    _patched.clear()


def is_instrumented() -> bool:
    return bool(_patched)
