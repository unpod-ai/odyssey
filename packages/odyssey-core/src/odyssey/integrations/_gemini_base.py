"""Shared capture logic for Gemini (``google-genai``) via ``client.models.generate_content``.

Google's own SDK shape, not OpenAI/Anthropic-compatible: ``contents`` is a
list of ``Content`` (``role`` + ``parts``), not ``messages``; ``role`` is
``"user"``/``"model"``, not ``"user"``/``"assistant"``; the system prompt and
tool definitions live under ``config`` (``system_instruction``, ``tools``),
not as separate top-level kwargs. Everything else — dedup the resent
conversation by tracking how much of ``contents`` has already been recorded,
record the system prompt/tools only when they change — is the same problem
``_base.py``'s docstring describes, just against this shape.

Parsing goes through :func:`odyssey.builders.messages.messages_from_gemini`,
the batch-import parser, reused rather than reimplemented. It raises on a
shape it does not recognise (fail loud, for a human-watched import); this
module degrades that to per-entry graceful failure the same way
``_openai_base.py``'s ``_safe_openai_messages`` does, so one malformed part
does not lose the whole call.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, List, Optional, Tuple

from odyssey.builders.messages import messages_from_gemini, normalize_role
from odyssey.capture import _emit, _jsonable
from odyssey.client import require_client
from odyssey.context import current
from odyssey.primitives import Message, ToolDefinition

# Request parameters worth keeping. The schema has no field for sampling
# settings, so they ride along in metadata — enough to reproduce a call later.
_PARAM_KEYS = (
    "temperature",
    "top_p",
    "top_k",
    "max_output_tokens",
    "stop_sequences",
    "candidate_count",
    "seed",
)

_STATE_CONSUMED = "_gemini_consumed"
_STATE_SYSTEM = "_gemini_system"
_STATE_TOOLS = "_gemini_tools"


def _normalize_contents(contents: Any) -> List[Dict[str, Any]]:
    """Coerce any of ``contents``'s accepted shapes into a list of
    ``{"role", "parts"}`` dicts — the one shape :func:`messages_from_gemini`
    understands.

    The real SDK accepts a bare string, a single ``Content``, a single
    ``Part``, or a list mixing any of those, normalizing internally
    (``google.genai._transformers.t_contents``) before sending the request.
    Capture only needs the shape a conversation-history caller actually sends
    back on turn 2+: a list of already-shaped ``Content`` entries. Anything
    else is one fresh user turn.
    """
    contents = _jsonable(contents)
    if isinstance(contents, str):
        return [{"role": "user", "parts": [{"text": contents}]}]
    if isinstance(contents, dict):
        if "parts" in contents:
            return [contents]
        return [{"role": "user", "parts": [contents]}]
    if isinstance(contents, list):
        if contents and all(isinstance(c, dict) and "parts" in c for c in contents):
            return contents
        parts = [
            {"text": c} if isinstance(c, str) else c
            for c in contents
            if isinstance(c, (str, dict))
        ]
        return [{"role": "user", "parts": parts}] if parts else []
    return []


def _safe_gemini_messages(
    entries: List[Dict[str, Any]],
) -> Tuple[List[Message], List[str]]:
    """``messages_from_gemini``, degraded gracefully instead of raising.

    Tries the whole batch first — the common, fast case. A single malformed
    entry falls back to per-entry parsing so the other entries in the same
    request are not lost with it; the one that fails is recorded as a bare
    message with the parse error named in metadata rather than dropped.
    """
    try:
        return messages_from_gemini(entries), []
    except (TypeError, ValueError):
        pass

    out: List[Message] = []
    unknown: List[str] = []
    for i, entry in enumerate(entries):
        try:
            out.extend(messages_from_gemini([entry]))
        except (TypeError, ValueError) as exc:
            unknown.append(f"entry {i}: {type(exc).__name__}: {exc}")
            raw_role = entry.get("role") if isinstance(entry, dict) else None
            try:
                role = normalize_role(raw_role)
            except ValueError:
                role = "user"
            out.append(Message(role=role))
    return out, unknown


def _system_messages(system_instruction: Any) -> List[Message]:
    """``config.system_instruction`` is a string, a ``Content``, or a ``Part``."""
    if system_instruction is None:
        return []
    value = _jsonable(system_instruction)
    if isinstance(value, str):
        return [Message(role="system", content=value)] if value else []
    if isinstance(value, dict):
        if isinstance(value.get("parts"), list):
            text = "\n".join(
                p.get("text", "") for p in value["parts"] if isinstance(p, dict)
            ).strip()
        else:
            text = str(value.get("text") or "")
        return [Message(role="system", content=text)] if text else []
    return []


def _tool_definitions(tools: Any) -> Optional[List[ToolDefinition]]:
    tools = _jsonable(tools)
    if not isinstance(tools, list):
        return None
    out: List[ToolDefinition] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        decls = tool.get("function_declarations") or tool.get("functionDeclarations")
        if not isinstance(decls, list):
            continue
        for decl in decls:
            if not isinstance(decl, dict):
                continue
            name = decl.get("name")
            if not name:
                continue
            out.append(
                ToolDefinition(
                    name=str(name),
                    description=str(decl.get("description") or ""),
                    parameters=decl.get("parameters")
                    or decl.get("parameters_json_schema")
                    or {},
                )
            )
    return out or None


def _params(config: Dict[str, Any]) -> Dict[str, Any]:
    return {k: _jsonable(config[k]) for k in _PARAM_KEYS if config.get(k) is not None}


def capture_request(kwargs: Dict[str, Any]) -> None:
    """Record the parts of a request that have not been recorded yet."""
    ctx = current()
    if ctx is None:
        return

    entries = _normalize_contents(kwargs.get("contents"))
    consumed = int(ctx.state.get(_STATE_CONSUMED, 0))

    if len(entries) < consumed:
        # The caller rebuilt or truncated its contents list, so our offset is
        # meaningless. Resync rather than re-record — see _base.py's twin.
        client = require_client()
        if client is not None:
            client.note_error(
                "capture_request",
                RuntimeWarning(
                    f"contents shrank from {consumed} to {len(entries)}; "
                    "resyncing without re-recording"
                ),
            )
        ctx.state[_STATE_CONSUMED] = len(entries)
        return

    new_entries = entries[consumed:]
    messages, unknown = _safe_gemini_messages(new_entries)

    config = _jsonable(kwargs.get("config"))
    if not isinstance(config, dict):
        config = {}

    # The system prompt and tools are resent on every call, exactly like
    # Anthropic's `system`/`tools` kwargs, just nested under `config` here.
    system_instruction = config.get("system_instruction")
    system_key = repr(system_instruction) if system_instruction is not None else None
    if system_instruction is not None and ctx.state.get(_STATE_SYSTEM) != system_key:
        ctx.state[_STATE_SYSTEM] = system_key
        messages = _system_messages(system_instruction) + messages

    tools = _tool_definitions(config.get("tools"))
    tools_key = repr([(t.name, t.parameters) for t in tools]) if tools else None
    tools_changed = tools is not None and ctx.state.get(_STATE_TOOLS) != tools_key
    if tools_changed:
        ctx.state[_STATE_TOOLS] = tools_key

    meta: Dict[str, Any] = {"direction": "request"}
    params = _params(config)
    if params:
        meta["params"] = params
    if unknown:
        meta["unknown_parts"] = sorted(set(unknown))

    model = kwargs.get("model")
    for i, msg in enumerate(messages):
        if tools_changed and i == 0:
            msg = dataclasses.replace(msg, tool_definitions=tools)
        _emit(
            "message",
            message=msg,
            model_id=str(model) if model else None,
            metadata=meta,
        )

    ctx.state[_STATE_CONSUMED] = len(entries)


def capture_response(response: Any, *, model: Optional[str] = None) -> None:
    """Record the assistant turn a provider returned.

    Unlike Anthropic/OpenAI, ``finish_reason``/usage live on the response's
    first ``candidate``, not inside the ``Content`` itself, so they are
    stitched onto the parsed message(s) here rather than inside
    :func:`odyssey.builders.messages.messages_from_gemini`.
    """
    ctx = current()
    if ctx is None:
        return

    payload = _jsonable(response)
    if not isinstance(payload, dict):
        return

    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return
    candidate = candidates[0]
    if not isinstance(candidate, dict):
        return
    content = candidate.get("content")
    if not isinstance(content, dict):
        return

    entry = dict(content)
    entry.setdefault("role", "model")

    messages, unknown = _safe_gemini_messages([entry])

    finish_reason = candidate.get("finish_reason")
    usage_metadata = payload.get("usage_metadata")
    usage = (
        {k: v for k, v in usage_metadata.items() if isinstance(v, int)}
        if isinstance(usage_metadata, dict)
        else None
    )

    meta: Dict[str, Any] = {"direction": "response"}
    if payload.get("response_id"):
        meta["provider_message_id"] = payload["response_id"]
    if unknown:
        meta["unknown_parts"] = sorted(set(unknown))

    model_id = str(payload.get("model_version") or model or "") or None

    for msg in messages:
        if finish_reason is not None or usage is not None:
            msg = dataclasses.replace(
                msg,
                finish_reason=msg.finish_reason or finish_reason,
                usage=msg.usage or usage,
            )
        _emit("message", message=msg, model_id=model_id, metadata=meta)

    # The caller will append this turn to its own contents list before the
    # next call. Account for it now so the next delta starts at the new turn.
    if messages:
        ctx.state[_STATE_CONSUMED] = int(ctx.state.get(_STATE_CONSUMED, 0)) + 1
