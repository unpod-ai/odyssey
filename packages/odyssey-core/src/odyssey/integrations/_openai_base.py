"""Shared capture logic for OpenAI, and anything shaped like it.

"OpenAI-compatible" providers (Groq, Together, local vLLM/Ollama servers,
DeepSeek, and others) speak the exact same Chat Completions request and
response JSON that OpenAI does — that compatibility is the whole point of the
term. ``odyssey.integrations.openai.OpenAI`` wraps ``openai.OpenAI``
untouched, so pointing it at a different ``base_url`` (``OpenAI(base_url=...,
api_key=...)``) captures those providers for free; nothing here is OpenAI-
specific beyond the shape of the JSON.

Simpler than ``_base.py``'s Anthropic twin in one respect: Anthropic's system
prompt is a separate top-level kwarg the wrapper has to dedup by hand.
OpenAI's is just ``messages[0]`` with ``role="system"`` — part of the same
resent, ever-growing array every other turn is in, so the existing "track how
much of the list is already recorded, emit only the tail" logic (the same
problem ``_base.py``'s docstring describes) covers it with no special case.

Parsing goes through :func:`odyssey.builders.messages.messages_from_openai_chat`
— the batch-import parser, reused rather than reimplemented. That parser
raises on a shape it does not recognise, which is right for a human-watched
import and wrong on an auto-capture path where the whole turn would vanish
because of one malformed entry. :func:`_safe_openai_messages` is the
degrade-gracefully wrapper around it for that reason.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from odyssey.builders.messages import messages_from_openai_chat, normalize_role
from odyssey.capture import _emit, _jsonable
from odyssey.client import require_client
from odyssey.context import JourneyContext, current
from odyssey.primitives import Message, ToolDefinition

# Request parameters worth keeping. The schema has no field for sampling
# settings, so they ride along in metadata — enough to reproduce a call later.
_PARAM_KEYS = (
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "frequency_penalty",
    "presence_penalty",
    "seed",
    "stop",
)

_STATE_CONSUMED = "_openai_consumed"
_STATE_TOOLS = "_openai_tools"


def _safe_openai_messages(entries: List[Any]) -> Tuple[List[Message], List[str]]:
    """``messages_from_openai_chat``, degraded gracefully instead of raising.

    Tries the whole batch first — the common, fast case. A single malformed
    entry falls back to per-entry parsing so the other entries in the same
    request are not lost with it; the one that fails is recorded as a bare
    message with the parse error named in metadata rather than dropped.
    """
    try:
        return messages_from_openai_chat(entries), []
    except (TypeError, ValueError):
        pass

    out: List[Message] = []
    unknown: List[str] = []
    for i, entry in enumerate(entries):
        try:
            out.extend(messages_from_openai_chat([entry]))
        except (TypeError, ValueError) as exc:
            unknown.append(f"entry {i}: {type(exc).__name__}: {exc}")
            raw_role = entry.get("role") if isinstance(entry, dict) else None
            try:
                role = normalize_role(raw_role)
            except ValueError:
                role = "user"
            out.append(
                Message(
                    role=role,
                    content=repr(entry)[:2000],
                    metadata={"unparsed": True},
                )
            )
    return out, unknown


def _tool_definitions(tools: Any) -> Optional[List[ToolDefinition]]:
    """OpenAI wraps each tool as ``{"type": "function", "function": {...}}``."""
    if not isinstance(tools, list):
        return None
    out: List[ToolDefinition] = []
    for tool in tools:
        tool = _jsonable(tool)
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if tool.get("type") == "function" else tool
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not name:
            continue
        out.append(
            ToolDefinition(
                name=str(name),
                description=str(fn.get("description") or ""),
                parameters=fn.get("parameters") or {},
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
        # meaningless. Resync rather than re-record: a duplicated turn is
        # silent corruption in the corpus, while a skipped one is only a hole.
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
    messages, unknown = _safe_openai_messages(new_entries)

    # Tool definitions are resent on every call exactly like the rest of the
    # history, so they are recorded on the turn that introduces or changes
    # them and nowhere else — stamping them on every message would repeat the
    # whole schema once per turn.
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
        meta["unknown_blocks"] = unknown

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
    choices = payload.get("choices") or []
    if not choices:
        return
    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message")
    if not isinstance(message, dict):
        return

    entry: Dict[str, Any] = dict(message)
    if choice.get("finish_reason") is not None:
        entry["finish_reason"] = choice["finish_reason"]
    usage = payload.get("usage")
    if isinstance(usage, dict):
        entry["usage"] = {
            k: v for k, v in usage.items() if isinstance(v, int) and v is not None
        }

    messages, unknown = _safe_openai_messages([entry])
    meta: Dict[str, Any] = {"direction": "response"}
    if payload.get("id"):
        meta["provider_message_id"] = payload["id"]
    if unknown:
        meta["unknown_blocks"] = unknown

    resolved_model = payload.get("model") or model
    for msg in messages:
        _emit(
            "message",
            message=msg,
            model_id=str(resolved_model) if resolved_model else None,
            metadata=meta,
        )

    # The caller will append this turn to its own message list before the
    # next call. Account for it now so the next delta starts at the new
    # user turn.
    if messages:
        _advance_consumed(ctx, 1)


def _advance_consumed(ctx: JourneyContext, by: int) -> None:
    ctx.state[_STATE_CONSUMED] = int(ctx.state.get(_STATE_CONSUMED, 0)) + by
