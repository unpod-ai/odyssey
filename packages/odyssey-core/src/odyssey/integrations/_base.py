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
from odyssey.primitives import Message

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


def capture_request(kwargs: Dict[str, Any]) -> None:
    """Record the parts of a request that have not been recorded yet."""
    ctx = current()
    if ctx is None:
        return

    entries = kwargs.get("messages") or []
    if not isinstance(entries, (list, tuple)):
        entries = []
    consumed = int(ctx.state.get(_STATE_CONSUMED, 0))

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
        ctx.state[_STATE_CONSUMED] = len(entries)
        return

    new_entries = list(entries[consumed:])
    messages, unknown = to_messages(new_entries)

    # The system prompt is resent on every call. Record it only when it changes,
    # which is exactly the prompt-refresh case the step builder handles on read.
    system = kwargs.get("system")
    system_key = repr(_jsonable(system))
    if system is not None and ctx.state.get(_STATE_SYSTEM) != system_key:
        ctx.state[_STATE_SYSTEM] = system_key
        messages = _system_messages(system) + messages

    # Tool definitions are resent on every call exactly like the system prompt,
    # so they are recorded on the turn that introduces or changes them and
    # nowhere else. Stamping them on every message would repeat the whole schema
    # once per turn.
    tools = _tool_definitions(kwargs.get("tools"))
    tools_key = repr([(t.name, t.parameters) for t in tools]) if tools else None
    tools_changed = tools is not None and ctx.state.get(_STATE_TOOLS) != tools_key
    if tools_changed:
        ctx.state[_STATE_TOOLS] = tools_key

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

    ctx.state[_STATE_CONSUMED] = len(entries)


def capture_response(response: Any, *, model: Optional[str] = None) -> None:
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

    messages, unknown = to_messages([entry])
    meta: Dict[str, Any] = {"direction": "response"}
    if payload.get("id"):
        meta["provider_message_id"] = payload["id"]
    if unknown:
        meta["unknown_blocks"] = sorted(set(unknown))

    for msg in messages:
        _emit(
            "message",
            message=msg,
            model_id=str(payload.get("model") or model or "") or None,
            metadata=meta,
        )

    # The caller will append this turn to its own message list before the next
    # call. Account for it now so the next delta starts at the new user turn.
    if messages:
        _advance_consumed(ctx, 1)


def _advance_consumed(ctx: JourneyContext, by: int) -> None:
    ctx.state[_STATE_CONSUMED] = int(ctx.state.get(_STATE_CONSUMED, 0)) + by
