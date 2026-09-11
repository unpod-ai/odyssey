"""Shared provider-capture logic: a call's request and response become events.

The drop-in client and the opt-in monkey-patch are two ways of *attaching* to a
provider call. Both funnel through here, so there is one implementation of what
a captured turn means.

Two problems dominate this file, and neither is obvious until you hit it in
production.

**Providers resend the whole conversation.** Turn 3's request contains turns 1
and 2 again. Recording every request verbatim would triple the corpus with
duplicate turns that the fold cannot detect — it deduplicates on ``event_id``,
and re-recorded history carries fresh ids. So the wrapper tracks how much of the
message list it has already recorded and emits only the tail. This is the
write-side twin of the system-prompt handling in ``build_cumulative_steps``.

**Providers add content-block types.** ``messages_from_anthropic_messages``
refuses unknown blocks by design — silent fallbacks turn parse bugs into
data-quality bugs. That is right for a batch import a human is watching, and
wrong on an auto-capture path, where the whole turn would vanish because a new
block type shipped. So unknown blocks are separated before parsing: reasoning
blocks become ``Message.reasoning``, and anything else is dropped from the parse
but named in the event metadata, where it is visible rather than lost.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from odyssey.builders.messages import messages_from_anthropic_messages
from odyssey.capture import _emit, _jsonable
from odyssey.client import require_client
from odyssey.context import JourneyContext, current
from odyssey.integrations._timing import stamp
from odyssey.integrations.providers import Adapter, apply_adapter
from odyssey.primitives import Message

# The SDK behind these calls. Lands on `Message.provider`; distinct from
# `model_id`, which the provider itself reports per response.
PROVIDER = "anthropic"

# Blocks the ported parser understands.
_PARSEABLE_BLOCKS = frozenset({"text", "tool_use", "tool_result"})
# Blocks that are model reasoning: kept, but on Message.reasoning rather than as
# content, because they are not the turn the model should be trained to emit.
_REASONING_BLOCKS = frozenset({"thinking", "redacted_thinking"})

# Request parameters worth keeping. The schema has no field for sampling
# settings, so they ride along in metadata — enough to reproduce a call later.
_PARAM_KEYS = (
    "temperature",
    "top_p",
    "top_k",
    "max_tokens",
    "stop_sequences",
    "service_tier",
)

_STATE_CONSUMED = "_anthropic_consumed"
_STATE_SYSTEM = "_anthropic_system"
_STATE_TOOLS = "_anthropic_tools"


def _state_key(base: str, key: str) -> str:
    """Per-client bookkeeping inside one journey; see `_openai_base._state_key`."""
    return base if key == "default" else f"{base}:{key}"


def split_blocks(content: Any) -> Tuple[Any, Optional[str], List[str]]:
    """Separate a content payload into (parseable, reasoning text, unknown types).

    A plain string passes through untouched — that is already the parser's
    happy path.
    """
    if not isinstance(content, list):
        return content, None, []

    keep: List[Any] = []
    reasoning: List[str] = []
    unknown: List[str] = []
    for block in content:
        block = _jsonable(block)
        if not isinstance(block, dict):
            unknown.append(type(block).__name__)
            continue
        btype = block.get("type")
        if btype in _PARSEABLE_BLOCKS:
            keep.append(block)
        elif btype in _REASONING_BLOCKS:
            text = block.get("thinking") or block.get("data") or ""
            if isinstance(text, str) and text:
                reasoning.append(text)
        else:
            unknown.append(str(btype))
    return keep, ("\n".join(reasoning) or None), unknown


def to_messages(entries: Sequence[Any]) -> Tuple[List[Message], List[str]]:
    """Anthropic-shaped entries → :class:`Message` list, unknown types reported.

    Never raises: a malformed entry is skipped and its shape reported, because
    losing one turn beats losing the journey.
    """
    out: List[Message] = []
    unknown_all: List[str] = []
    for entry in entries:
        entry = _jsonable(entry)
        if not isinstance(entry, dict):
            unknown_all.append(f"entry:{type(entry).__name__}")
            continue
        content, reasoning, unknown = split_blocks(entry.get("content"))
        unknown_all.extend(unknown)
        payload = dict(entry)
        payload["content"] = content
        try:
            parsed = messages_from_anthropic_messages([payload])
        except (TypeError, ValueError) as exc:
            unknown_all.append(f"unparsed:{type(exc).__name__}")
            continue
        if reasoning and parsed:
            parsed[0] = _with_reasoning(parsed[0], reasoning)
        out.extend(parsed)
    return out, unknown_all


def _with_reasoning(message: Message, reasoning: str) -> Message:
    import dataclasses

    return dataclasses.replace(message, reasoning=reasoning)


def _system_messages(system: Any) -> List[Message]:
    """Anthropic's ``system`` is a string or a list of text blocks."""
    if system is None:
        return []
    if isinstance(system, str):
        return [Message(role="system", content=system)] if system else []
    content, _reasoning, _unknown = split_blocks(system)
    if isinstance(content, list):
        text = "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict)
        ).strip()
        return [Message(role="system", content=text)] if text else []
    return []


def _tool_definitions(tools: Any) -> Optional[List[Any]]:
    from odyssey.primitives import ToolDefinition

    if not isinstance(tools, list):
        return None
    out: List[ToolDefinition] = []
    for tool in tools:
        tool = _jsonable(tool)
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not name:
            continue
        out.append(
            ToolDefinition(
                name=str(name),
                description=str(tool.get("description") or ""),
                parameters=tool.get("input_schema") or tool.get("parameters") or {},
            )
        )
    return out or None


def _params(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    return {k: _jsonable(kwargs[k]) for k in _PARAM_KEYS if kwargs.get(k) is not None}


# ---------------------------------------------------------------------------
# The two capture halves
# ---------------------------------------------------------------------------


def capture_request(kwargs: Dict[str, Any], *, key: str = "default") -> None:
    """Record the parts of a request that have not been recorded yet."""
    ctx = current()
    if ctx is None:
        return
    consumed_key = _state_key(_STATE_CONSUMED, key)
    system_state_key = _state_key(_STATE_SYSTEM, key)
    tools_state_key = _state_key(_STATE_TOOLS, key)

    entries = kwargs.get("messages") or []
    if not isinstance(entries, (list, tuple)):
        entries = []
    consumed = int(ctx.state.get(consumed_key, 0))

    if len(entries) < consumed:
        # The caller rebuilt or truncated its message list, so our offset is
        # meaningless. Resync rather than re-record: a duplicated turn is silent
        # corruption in the corpus, while a skipped one is merely a hole.
        client = require_client()
        if client is not None:
            client.note_error(
                "capture_request",
                RuntimeWarning(
                    f"message list shrank from {consumed} to {len(entries)}; "
                    "resyncing without re-recording"
                ),
            )
        ctx.state[consumed_key] = len(entries)
        return

    new_entries = list(entries[consumed:])
    messages, unknown = to_messages(new_entries)

    # The system prompt is resent on every call. Record it only when it changes,
    # which is exactly the prompt-refresh case the step builder handles on read.
    system = kwargs.get("system")
    system_key = repr(_jsonable(system))
    if system is not None and ctx.state.get(system_state_key) != system_key:
        ctx.state[system_state_key] = system_key
        messages = _system_messages(system) + messages

    # Tool definitions are resent on every call exactly like the system prompt,
    # so they are recorded on the turn that introduces or changes them and
    # nowhere else. Stamping them on every message would repeat the whole schema
    # once per turn.
    tools = _tool_definitions(kwargs.get("tools"))
    tools_key = repr([(t.name, t.parameters) for t in tools]) if tools else None
    tools_changed = tools is not None and ctx.state.get(tools_state_key) != tools_key
    if tools_changed:
        ctx.state[tools_state_key] = tools_key

    meta: Dict[str, Any] = {"direction": "request"}
    params = _params(kwargs)
    if params:
        meta["params"] = params
    if unknown:
        meta["unknown_blocks"] = sorted(set(unknown))

    model = kwargs.get("model")
    for i, msg in enumerate(messages):
        if tools_changed and i == 0:
            import dataclasses

            msg = dataclasses.replace(msg, tool_definitions=tools)
        _emit(
            "message",
            message=msg,
            model_id=str(model) if model else None,
            metadata=meta,
        )

    ctx.state[consumed_key] = len(entries)


def capture_response(
    response: Any,
    *,
    model: Optional[str] = None,
    latency_ms: Optional[float] = None,
    ttft_ms: Optional[float] = None,
    provider: Optional[str] = None,
    adapt: Optional[Adapter] = None,
    key: str = "default",
    extra_meta: Optional[Dict[str, Any]] = None,
) -> None:
    """Record the assistant turn a provider returned."""
    ctx = current()
    if ctx is None:
        return

    payload = _jsonable(response)
    if not isinstance(payload, dict):
        return

    entry: Dict[str, Any] = {
        "role": payload.get("role") or "assistant",
        "content": payload.get("content"),
    }
    if payload.get("stop_reason") is not None:
        entry["stop_reason"] = payload["stop_reason"]
    usage = payload.get("usage")
    if isinstance(usage, dict):
        entry["usage"] = {
            k: v for k, v in usage.items() if isinstance(v, int) and v is not None
        }

    if adapt is not None:
        entry = apply_adapter(
            adapt, entry, response, label=f"provider.adapt:{provider}"
        )
    messages, unknown = to_messages([entry])
    meta: Dict[str, Any] = {"direction": "response"}
    if payload.get("id"):
        meta["provider_message_id"] = payload["id"]
    if unknown:
        meta["unknown_blocks"] = sorted(set(unknown))
    if extra_meta:
        meta.update(extra_meta)

    for msg in messages:
        _emit(
            "message",
            message=stamp(
                msg,
                latency_ms=latency_ms,
                ttft_ms=ttft_ms,
                provider=provider or PROVIDER,
            ),
            model_id=str(payload.get("model") or model or "") or None,
            metadata=meta,
        )

    # The caller will append this turn to its own message list before the next
    # call. Account for it now so the next delta starts at the new user turn.
    if messages:
        _advance_consumed(ctx, 1, key)


def _advance_consumed(ctx: JourneyContext, by: int, key: str = "default") -> None:
    name = _state_key(_STATE_CONSUMED, key)
    ctx.state[name] = int(ctx.state.get(name, 0)) + by


class MessageAccumulator:
    """Folds Anthropic's stream events back into the one message they described.

    ``message_start`` carries the id, model and input usage;
    ``content_block_*`` build each block, a tool call's input arriving as
    partial JSON; ``message_delta`` carries the stop reason and output usage.
    """

    __slots__ = ("id", "model", "role", "blocks", "stop_reason", "usage")

    def __init__(self) -> None:
        self.id: Optional[str] = None
        self.model: Optional[str] = None
        self.role = "assistant"
        self.blocks: Dict[int, Dict[str, Any]] = {}
        self.stop_reason: Optional[str] = None
        self.usage: Dict[str, int] = {}

    @property
    def finished(self) -> bool:
        return self.stop_reason is not None

    def add(self, event: Any) -> bool:
        """Fold one event in. True when it carried model output."""
        d = _jsonable(event)
        if not isinstance(d, dict):
            return False
        kind = d.get("type")
        if kind == "message_start":
            msg = d.get("message")
            if isinstance(msg, dict):
                self.id = msg.get("id") or self.id
                self.model = msg.get("model") or self.model
                self.role = msg.get("role") or self.role
                self._add_usage(msg.get("usage"))
            return False
        if kind == "content_block_start":
            block = d.get("content_block")
            if isinstance(block, dict):
                self.blocks[_index(d)] = {
                    k: v for k, v in block.items() if v is not None
                }
            return False
        if kind == "content_block_delta":
            return self._add_delta(_index(d), d.get("delta"))
        if kind == "message_delta":
            delta = d.get("delta")
            if isinstance(delta, dict) and delta.get("stop_reason"):
                self.stop_reason = str(delta["stop_reason"])
            self._add_usage(d.get("usage"))
        return False

    def _add_delta(self, index: int, delta: Any) -> bool:
        if not isinstance(delta, dict):
            return False
        block = self.blocks.setdefault(index, {"type": "text", "text": ""})
        kind = delta.get("type")
        if kind == "text_delta":
            block["text"] = (block.get("text") or "") + str(delta.get("text") or "")
            return True
        if kind == "input_json_delta":
            block["_json"] = (block.get("_json") or "") + str(
                delta.get("partial_json") or ""
            )
            return True
        if kind == "thinking_delta":
            block["thinking"] = (block.get("thinking") or "") + str(
                delta.get("thinking") or ""
            )
            return True
        return False

    def _add_usage(self, usage: Any) -> None:
        if isinstance(usage, dict):
            self.usage.update({k: v for k, v in usage.items() if isinstance(v, int)})

    def payload(self) -> Optional[Dict[str, Any]]:
        """The assembled message, shaped like a non-streamed response."""
        if not self.blocks:
            return None
        import json

        content: List[Dict[str, Any]] = []
        for i in sorted(self.blocks):
            block = dict(self.blocks[i])
            partial = block.pop("_json", None)
            if partial is not None:
                try:
                    block["input"] = json.loads(partial) if partial else {}
                except ValueError:
                    # Cut off mid-argument: keep what arrived rather than drop
                    # the call.
                    block["input"] = {"_partial_json": partial}
            content.append(block)
        return {
            "id": self.id,
            "model": self.model,
            "role": self.role,
            "content": content,
            "stop_reason": self.stop_reason,
            "usage": self.usage or None,
        }


def _index(d: Dict[str, Any]) -> int:
    index = d.get("index")
    return index if isinstance(index, int) else 0


def record_stream(
    acc: MessageAccumulator,
    *,
    model: Optional[str] = None,
    latency_ms: Optional[float] = None,
    ttft_ms: Optional[float] = None,
    provider: Optional[str] = None,
    adapt: Optional[Adapter] = None,
    key: str = "default",
    extra_meta: Optional[Dict[str, Any]] = None,
) -> None:
    """Record the message a drained event stream assembled."""
    payload = acc.payload()
    if payload is None:
        return
    capture_response(
        payload,
        model=model,
        latency_ms=latency_ms,
        ttft_ms=ttft_ms,
        provider=provider,
        adapt=adapt,
        key=key,
        extra_meta=extra_meta,
    )
