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
from odyssey.context import current
from odyssey.integrations._timing import stamp
from odyssey.integrations.providers import (
    Adapter,
    apply_adapter,
    normalize_reasoning,
)
from odyssey.primitives import Message, ToolDefinition

# Reported only when the client exposes no `base_url` -- a drop-in wrapper
# around a stub, say. A real client always has one, and
# `integrations.providers.resolve` names the provider from it.
PROVIDER = "openai"

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


def _state_key(base: str, key: str) -> str:
    """Per-client bookkeeping inside one journey.

    A voice call's `.llm` journey hears several conversations at once -- the
    main LLM, a filler model, a tool-call adapter -- each resending its own
    history. One shared offset would have each call resync against the
    others' and record nothing; one offset per client keeps every tail exact.
    """
    return base if key == "default" else f"{base}:{key}"


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


def capture_request(kwargs: Dict[str, Any], *, key: str = "default") -> None:
    """Record the parts of a request that have not been recorded yet."""
    ctx = current()
    if ctx is None:
        return

    entries = kwargs.get("messages") or []
    if not isinstance(entries, (list, tuple)):
        entries = []
    consumed_key = _state_key(_STATE_CONSUMED, key)
    tools_state_key = _state_key(_STATE_TOOLS, key)
    consumed = int(ctx.state.get(consumed_key, 0))

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
        ctx.state[consumed_key] = len(entries)
        return

    new_entries = list(entries[consumed:])
    messages, unknown = _safe_openai_messages(new_entries)

    # Tool definitions are resent on every call exactly like the rest of the
    # history, so they are recorded on the turn that introduces or changes
    # them and nowhere else — stamping them on every message would repeat the
    # whole schema once per turn.
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
) -> None:
    """Record the assistant turn a non-streamed response carried."""
    if current() is None:
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
        entry["usage"] = _int_usage(usage)

    record_assistant(
        entry,
        raw=response,
        response_id=payload.get("id"),
        model=payload.get("model") or model,
        latency_ms=latency_ms,
        ttft_ms=ttft_ms,
        provider=provider,
        adapt=adapt,
        key=key,
    )


def record_assistant(
    entry: Dict[str, Any],
    *,
    raw: Any = None,
    response_id: Optional[str] = None,
    model: Optional[str] = None,
    latency_ms: Optional[float] = None,
    ttft_ms: Optional[float] = None,
    provider: Optional[str] = None,
    adapt: Optional[Adapter] = None,
    key: str = "default",
    extra_meta: Optional[Dict[str, Any]] = None,
) -> None:
    """Record one assistant turn, from a response or from a drained stream."""
    ctx = current()
    if ctx is None:
        return

    entry = normalize_reasoning(entry)
    if adapt is not None:
        entry = apply_adapter(adapt, entry, raw, label=f"provider.adapt:{provider}")

    messages, unknown = _safe_openai_messages([entry])
    meta: Dict[str, Any] = {"direction": "response"}
    if response_id:
        meta["provider_message_id"] = response_id
    if unknown:
        meta["unknown_blocks"] = unknown
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
            model_id=str(model) if model else None,
            metadata=meta,
        )

    # The caller will append this turn to its own message list before the
    # next call. Account for it now so the next delta starts at the new
    # user turn.
    if messages:
        key_name = _state_key(_STATE_CONSUMED, key)
        ctx.state[key_name] = int(ctx.state.get(key_name, 0)) + 1


def _int_usage(usage: Dict[str, Any]) -> Dict[str, int]:
    return {k: v for k, v in usage.items() if isinstance(v, int)}


class ChunkAccumulator:
    """Folds a Chat Completions stream back into the one message it streamed.

    Every chunk is snapshotted as it passes, because a consumer may rewrite a
    chunk in place after reading it -- LiveKit strips `<think>` tags out of
    `delta.content` -- and the corpus should hold what the provider sent, not
    what one consumer did with it. Only choice 0 is kept, matching the
    non-streamed path.
    """

    __slots__ = (
        "content",
        "reasoning",
        "tool_calls",
        "finish_reason",
        "usage",
        "model",
        "id",
    )

    def __init__(self) -> None:
        self.content: List[str] = []
        self.reasoning: List[str] = []
        self.tool_calls: Dict[int, Dict[str, Any]] = {}
        self.finish_reason: Optional[str] = None
        self.usage: Optional[Dict[str, int]] = None
        self.model: Optional[str] = None
        self.id: Optional[str] = None

    @property
    def finished(self) -> bool:
        return self.finish_reason is not None

    def add(self, chunk: Any) -> bool:
        """Fold one chunk in. True when it carried model output (the TTFT mark)."""
        d = _jsonable(chunk)
        if not isinstance(d, dict):
            return False
        if d.get("id") and self.id is None:
            self.id = str(d["id"])
        if d.get("model"):
            self.model = str(d["model"])
        if isinstance(d.get("usage"), dict):
            self.usage = _int_usage(d["usage"])

        carried = False
        for choice in d.get("choices") or []:
            if not isinstance(choice, dict) or (choice.get("index") or 0) != 0:
                continue
            if choice.get("finish_reason"):
                self.finish_reason = str(choice["finish_reason"])
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            text = delta.get("content")
            if isinstance(text, str) and text:
                self.content.append(text)
                carried = True
            for field in ("reasoning_content", "reasoning"):
                trace = delta.get(field)
                if isinstance(trace, str) and trace:
                    self.reasoning.append(trace)
                    carried = True
            for call in delta.get("tool_calls") or []:
                if isinstance(call, dict):
                    self._add_tool_call(call)
                    carried = True
        return carried

    def _add_tool_call(self, call: Dict[str, Any]) -> None:
        index = call.get("index")
        slot = self.tool_calls.setdefault(
            index if isinstance(index, int) else len(self.tool_calls),
            {"id": None, "type": "function", "function": {"name": "", "arguments": ""}},
        )
        if call.get("id"):
            slot["id"] = str(call["id"])
        fn = call.get("function")
        if not isinstance(fn, dict):
            return
        name = fn.get("name")
        # Most providers send the name once; a few repeat it on every chunk.
        if isinstance(name, str) and name and name != slot["function"]["name"]:
            slot["function"]["name"] += name
        args = fn.get("arguments")
        if isinstance(args, str):
            slot["function"]["arguments"] += args

    def entry(self) -> Optional[Dict[str, Any]]:
        """The assembled assistant message, or None when nothing arrived."""
        if not (self.content or self.reasoning or self.tool_calls):
            return None
        entry: Dict[str, Any] = {
            "role": "assistant",
            "content": "".join(self.content) or None,
        }
        if self.tool_calls:
            entry["tool_calls"] = [self.tool_calls[i] for i in sorted(self.tool_calls)]
        if self.reasoning:
            entry["reasoning"] = "".join(self.reasoning)
        if self.finish_reason is not None:
            entry["finish_reason"] = self.finish_reason
        if self.usage:
            entry["usage"] = self.usage
        return entry


def record_stream(
    acc: ChunkAccumulator,
    *,
    model: Optional[str] = None,
    latency_ms: Optional[float] = None,
    ttft_ms: Optional[float] = None,
    provider: Optional[str] = None,
    adapt: Optional[Adapter] = None,
    key: str = "default",
    extra_meta: Optional[Dict[str, Any]] = None,
) -> None:
    """Record the turn a drained stream assembled. Nothing when nothing arrived."""
    entry = acc.entry()
    if entry is None:
        return
    record_assistant(
        entry,
        response_id=acc.id,
        model=acc.model or model,
        latency_ms=latency_ms,
        ttft_ms=ttft_ms,
        provider=provider,
        adapt=adapt,
        key=key,
        extra_meta=extra_meta,
    )
