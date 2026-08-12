"""Convert common third-party message formats into SDK :class:`Message` objects.

Recipes shipped:

- :func:`messages_from_openai_chat` -- OpenAI ChatCompletion (modern ``tool_calls`` or legacy ``function_call``)
- :func:`messages_from_anthropic_messages` -- Anthropic Messages API content blocks
- :func:`messages_from_vercel_ai_sdk` -- Vercel AI SDK ``UIMessage`` / ``CoreMessage`` shapes
- :func:`messages_from_prompt_response` -- flat prompt/response strings
- :func:`messages_from_role_content_pairs` -- ``[(role, content), ...]``

Philosophy: **fail loud on unexpected input.** These adapters are run over
customer data at ingest time and silent fallbacks turn parse bugs into
silent data-quality bugs. Every unrecognized shape raises ``ValueError`` /
``TypeError`` with a descriptive message so the caller can fix the input
(or write a custom recipe).

To support a format we don't ship a recipe for, write your own
``messages_from_myformat(raw) -> list[Message]`` using the public helpers
below -- :func:`normalize_role`, :func:`flatten_text_content`, and
:func:`parse_tool_arguments`.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional, cast

from odyssey.primitives import Message, Role, ToolCall, ToolResponse

_CANONICAL_ROLES = frozenset({"system", "user", "assistant", "tool"})
_ROLE_ALIASES: Dict[str, str] = {
    "human": "user",
    "ai": "assistant",
    "chatbot": "assistant",
    "function": "tool",
    "tool_use": "assistant",
    "tool_result": "tool",
    "model": "assistant",
}


def normalize_role(role_str: Optional[str]) -> Role:
    """Canonicalize a provider role string to one of the SDK roles.

    Raises ``ValueError`` for empty input or an unknown role. Recognized
    aliases are in ``_ROLE_ALIASES`` (e.g. ``human → user``, ``ai → assistant``).
    """
    if not role_str or not role_str.strip():
        raise ValueError("role is required (got empty/None)")
    lower = role_str.strip().lower()
    if lower in _CANONICAL_ROLES:
        return cast(Role, lower)
    mapped = _ROLE_ALIASES.get(lower)
    if mapped is None:
        raise ValueError(
            f"unknown role {role_str!r}; expected one of {sorted(_CANONICAL_ROLES)} "
            f"or alias in {sorted(_ROLE_ALIASES)}"
        )
    return cast(Role, mapped)


_NON_TEXT_MEDIA_TYPES = frozenset(
    {"image_url", "image", "input_audio", "audio", "refusal", "video"}
)


def flatten_text_content(content: Any) -> Optional[str]:
    """Collapse a provider's multi-modal content into plain text.

    Top-level dispatch:
      - ``None`` → ``None``
      - ``str`` → returned as-is
      - ``list`` of blocks → join ``type="text"`` blocks with newlines
      - ``dict`` → return ``content["text"]`` (raises if the key is missing or not a string)

    Known non-text media blocks (``image_url``, ``input_audio``, etc.) are
    silently dropped since this function's purpose is to produce a text view;
    use :class:`ToolCall` / :class:`ToolResponse` for structured payloads.
    Raises on structurally malformed input: non-str/dict block, text-typed
    block missing the ``text`` key, unknown block type, or top-level types
    other than None/str/list/dict.
    """
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            text
            for text in (
                _flatten_block(block, index=i) for i, block in enumerate(content)
            )
            if text is not None
        ]
        return "\n".join(parts) if parts else None
    if isinstance(content, dict):
        text = content.get("text")
        if not isinstance(text, str):
            raise ValueError(
                f"dict content must have a string 'text' key (got keys={sorted(content.keys())!r})"
            )
        return text
    raise TypeError(f"unsupported content type: {type(content).__name__}")


def _flatten_block(block: Any, *, index: int) -> Optional[str]:
    """Extract text from one block, or ``None`` if it's known non-text media."""
    if isinstance(block, str):
        return block
    if not isinstance(block, dict):
        raise TypeError(
            f"content block {index} must be str or dict, got {type(block).__name__}"
        )
    btype = block.get("type")
    if btype in _NON_TEXT_MEDIA_TYPES:
        return None
    if btype != "text":
        raise ValueError(
            f"content block {index} has unsupported type {btype!r}; "
            f"expected 'text' or a known non-text media type {sorted(_NON_TEXT_MEDIA_TYPES)}"
        )
    text = block.get("text")
    if not isinstance(text, str):
        raise ValueError(
            f"content block {index} is type='text' but 'text' key is missing or not a string"
        )
    return text


def _tool_result_to_response_text(raw: Any) -> Optional[str]:
    """Serialize a provider tool-result payload into ``ToolResponse.response`` text.

    Tool results are opaque by design -- providers frequently return
    structured JSON (``{"temp": 72}``), numbers, booleans, or lists of
    values. We preserve the full payload as a JSON string when it isn't
    already text, rather than forcing it through the multimodal-text
    flattener (which is for assistant/user message content and rightly
    rejects non-text shapes). No silent drops: every byte the provider
    emitted ends up on ``ToolResponse.response``.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        # Anthropic-style content-block list: try the multimodal flattener so
        # human-readable text wins over a JSON dump; fall back to JSON if the
        # list isn't made of known content blocks.
        if raw and all(isinstance(b, dict) and b.get("type") == "text" for b in raw):
            return flatten_text_content(raw)
        return json.dumps(raw, sort_keys=True, default=str)
    if isinstance(raw, dict):
        # Prefer a 'text' field if present (common convention), else serialize
        # the whole object so nothing is lost.
        text = raw.get("text")
        if isinstance(text, str):
            return text
        return json.dumps(raw, sort_keys=True, default=str)
    # bool / int / float / anything with a str representation.
    return str(raw)


def parse_tool_arguments(raw: Any) -> Dict[str, Any]:
    """Normalize provider tool-call arguments into a ``dict``.

    Accepts:
      - ``None`` → ``{}``
      - ``dict`` → returned as-is
      - JSON ``str`` that parses to a ``dict`` → the parsed dict

    Anything else (unparseable string, JSON that parses to a list/scalar,
    non-str/dict type) raises ``ValueError``. No silent ``{"_raw": ...}``
    escape hatch: if the caller's tool arguments aren't a dict, that's a data
    quality problem the caller needs to see.
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"tool arguments string is not valid JSON: {raw!r}"
            ) from exc
        if not isinstance(parsed, dict):
            raise ValueError(
                f"tool arguments JSON must parse to a dict, got {type(parsed).__name__}: {raw!r}"
            )
        return parsed
    raise TypeError(
        f"tool arguments must be None / dict / JSON str, got {type(raw).__name__}"
    )


def messages_from_openai_chat(raw: List[Dict[str, Any]]) -> List[Message]:
    """Convert OpenAI ChatCompletion-format dicts to :class:`Message` objects.

    Handles:
      - role normalization
      - ``content`` as string, multi-modal array, or dict
      - ``tool_calls`` (modern function-call shape)
      - legacy ``function_call`` (single-tool API) -- still present in older
        LangSmith exports, so we keep the branch until those age out
      - ``name`` on ``tool`` role → ``ToolResponse.name``
      - ``usage``, ``finish_reason`` pass-through

    Raises ``TypeError`` on a non-dict entry. Helpers raise on malformed
    tool-call payloads so ingestion fails fast rather than silently dropping
    tool information.
    """
    out: List[Message] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise TypeError(
                f"openai message {i} must be a dict, got {type(entry).__name__}"
            )
        raw_role = entry.get("role")
        role = normalize_role(raw_role)
        content = flatten_text_content(entry.get("content"))

        tool_calls: Optional[List[ToolCall]] = None
        raw_calls = entry.get("tool_calls")
        if raw_calls is not None:
            if not isinstance(raw_calls, list):
                raise TypeError(
                    f"openai message {i}: tool_calls must be a list, got {type(raw_calls).__name__}"
                )
            tool_calls = [
                _parse_openai_tool_call(c, index=i) for c in raw_calls
            ] or None
        elif "function_call" in entry and entry["function_call"] is not None:
            tool_calls = [_parse_openai_function_call(entry["function_call"], index=i)]

        tool_response: Optional[ToolResponse] = None
        if role == "tool":
            # Legacy ``role: "function"`` messages don't carry ``tool_call_id`` --
            # that field only exists in the modern parallel-tool-calls API. They
            # link to the assistant's ``function_call`` by position/name only.
            legacy_function_message = (
                isinstance(raw_role, str) and raw_role.strip().lower() == "function"
            )
            tool_response = _parse_openai_tool_response(
                entry, content, legacy=legacy_function_message
            )

        usage = entry.get("usage")
        if usage is not None and not isinstance(usage, dict):
            raise TypeError(
                f"openai message {i}: usage must be a dict if present, got {type(usage).__name__}"
            )
        metadata = entry.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            raise TypeError(
                f"openai message {i}: metadata must be a dict if present, got {type(metadata).__name__}"
            )

        reasoning = entry.get("reasoning")
        if reasoning is not None and not isinstance(reasoning, str):
            raise TypeError(
                f"openai message {i}: reasoning must be a str if present, got {type(reasoning).__name__}"
            )

        out.append(
            Message(
                role=role,
                content=content,
                tool_calls=tool_calls,
                tool_response=tool_response,
                usage=usage,
                finish_reason=entry.get("finish_reason"),
                metadata=metadata,
                reasoning=reasoning,
            )
        )
    return out


def _parse_openai_tool_call(raw: Any, *, index: int) -> ToolCall:
    if not isinstance(raw, dict):
        raise TypeError(
            f"openai message {index}: tool_call entry must be a dict, got {type(raw).__name__}"
        )
    function = raw.get("function")
    if function is not None and not isinstance(function, dict):
        raise TypeError(
            f"openai message {index}: tool_call.function must be a dict, got {type(function).__name__}"
        )
    if isinstance(function, dict):
        name = function.get("name") or raw.get("name")
        args = parse_tool_arguments(function.get("arguments"))
    else:
        name = raw.get("name")
        args = parse_tool_arguments(raw.get("arguments"))
    if not name:
        raise ValueError(f"openai message {index}: tool_call missing 'name'")
    return ToolCall(name=name, arguments=args, id=raw.get("id"))


def _parse_openai_function_call(raw: Any, *, index: int) -> ToolCall:
    if not isinstance(raw, dict):
        raise TypeError(
            f"openai message {index}: function_call must be a dict, got {type(raw).__name__}"
        )
    name = raw.get("name")
    if not name:
        raise ValueError(f"openai message {index}: function_call missing 'name'")
    return ToolCall(
        name=name,
        arguments=parse_tool_arguments(raw.get("arguments")),
        id=None,
    )


def _parse_openai_tool_response(
    entry: Dict[str, Any],
    content: Optional[str],
    *,
    legacy: bool = False,
) -> ToolResponse:
    """Build a :class:`ToolResponse` from a ``role=tool`` (or legacy ``role=function``) entry.

    Modern OpenAI tool-role messages must carry ``tool_call_id`` to link
    back to the assistant's ``tool_calls`` entry, so we raise if it's
    missing. Legacy ``role="function"`` messages predate that field and
    are linked by ``name``; we accept them with an empty ``id``.
    """
    tool_call_id = entry.get("tool_call_id") or entry.get("id") or ""
    if not legacy and not tool_call_id:
        raise ValueError(
            "openai tool-role message must have 'tool_call_id' or 'id'; "
            "if this is a legacy function-role message, use role='function' instead of 'tool'"
        )
    return ToolResponse(
        id=tool_call_id,
        name=entry.get("name") or "",
        arguments={},
        response=content,
        error=None,
    )


def messages_from_anthropic_messages(raw: List[Dict[str, Any]]) -> List[Message]:
    """Convert Anthropic Messages API format to :class:`Message` objects.

    Each Anthropic input message may expand to multiple output
    :class:`Message` objects because Anthropic packs several logical
    messages into one:

    - A ``user`` message containing one or more ``tool_result`` blocks
      expands into one :class:`Message` per block with ``role="tool"`` so
      downstream step builders and tool-failure metrics see each result
      individually. This matters for parallel tool calls, where Anthropic
      returns several ``tool_result`` blocks in the same user message.
    - Any ``text`` blocks in a message carrying tool results are emitted
      first as a separate user message so the text isn't lost.
    - ``tool_use`` blocks stay on the original assistant message as
      ``Message.tool_calls`` (parallel tool calls → list with multiple
      entries).
    """
    out: List[Message] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise TypeError(
                f"anthropic message {i} must be a dict, got {type(entry).__name__}"
            )
        role = normalize_role(entry.get("role"))
        content = entry.get("content")
        finish_reason = entry.get("stop_reason") or entry.get("finish_reason")
        usage = entry.get("usage")
        if usage is not None and not isinstance(usage, dict):
            raise TypeError(
                f"anthropic message {i}: usage must be a dict if present, got {type(usage).__name__}"
            )

        if isinstance(content, str):
            out.append(
                Message(
                    role=role, content=content, finish_reason=finish_reason, usage=usage
                )
            )
            continue

        if not isinstance(content, list):
            raise TypeError(
                f"anthropic message {i}: content must be str or list of blocks, "
                f"got {type(content).__name__}"
            )

        text_parts: List[str] = []
        tool_calls: List[ToolCall] = []
        tool_results: List[ToolResponse] = []

        for j, block in enumerate(content):
            if not isinstance(block, dict):
                raise TypeError(
                    f"anthropic message {i} block {j} must be a dict, got {type(block).__name__}"
                )
            btype = block.get("type")
            if btype == "text":
                text = block.get("text")
                if not isinstance(text, str):
                    raise ValueError(
                        f"anthropic message {i} block {j} is type='text' but 'text' is missing or not a string"
                    )
                text_parts.append(text)
            elif btype == "tool_use":
                name = block.get("name")
                if not name:
                    raise ValueError(
                        f"anthropic message {i} block {j} is type='tool_use' but 'name' is missing"
                    )
                tool_calls.append(
                    ToolCall(
                        name=name,
                        arguments=block.get("input") or {},
                        id=block.get("id"),
                    )
                )
            elif btype == "tool_result":
                tool_results.append(
                    ToolResponse(
                        id=block.get("tool_use_id") or block.get("id") or "",
                        name=block.get("name") or "",
                        arguments={},
                        response=_tool_result_to_response_text(block.get("content")),
                        error="tool_error" if block.get("is_error") else None,
                    )
                )
            else:
                raise ValueError(
                    f"anthropic message {i} block {j} has unsupported type {btype!r}; "
                    "expected 'text', 'tool_use', or 'tool_result'"
                )

        primary_content = "\n".join(text_parts) if text_parts else None
        has_primary = primary_content is not None or tool_calls
        attributed = False

        if has_primary:
            out.append(
                Message(
                    role=role,
                    content=primary_content,
                    tool_calls=tool_calls or None,
                    finish_reason=finish_reason,
                    usage=usage,
                )
            )
            attributed = True

        # Each tool_result becomes its own role="tool" message so downstream
        # step builders and failure metrics (which key on role == "tool") see
        # every result, including parallel ones.
        for tr in tool_results:
            out.append(
                Message(
                    role="tool",
                    content=tr.response,
                    tool_response=tr,
                    finish_reason=None if attributed else finish_reason,
                    usage=None if attributed else usage,
                )
            )
            attributed = True

        if not has_primary and not tool_results:
            # Preserve empty-content messages (rare but possible) so we don't
            # silently drop turns.
            out.append(
                Message(
                    role=role, content=None, finish_reason=finish_reason, usage=usage
                )
            )

    return out


def messages_from_vercel_ai_sdk(raw: List[Dict[str, Any]]) -> List[Message]:
    """Convert Vercel AI SDK messages to :class:`Message` objects.

    Vercel's ``UIMessage`` / ``CoreMessage`` shapes both represent content as
    either a plain string (``CoreMessage``) or a list of typed parts
    (``UIMessage``). Recognized part types:

    - ``text`` → concatenated into the message content
    - ``tool-invocation`` / ``tool-call`` → :class:`ToolCall` on the message
    - ``tool-result`` → separate ``role="tool"`` :class:`Message` (same fan-out
      behavior as Anthropic multi-part messages)
    - ``reasoning`` → attached to ``Message.reasoning``

    Clay's ingest currently uses this shape, so this is the primary
    customer-facing recipe today.
    """
    out: List[Message] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise TypeError(
                f"vercel message {i} must be a dict, got {type(entry).__name__}"
            )
        role = normalize_role(entry.get("role"))

        content_val = entry.get("content")
        parts_val = entry.get("parts")
        if content_val is not None and parts_val is not None:
            raise ValueError(
                f"vercel message {i}: only one of 'content' or 'parts' may be set"
            )

        if isinstance(content_val, str):
            out.append(Message(role=role, content=content_val))
            continue

        blocks = parts_val if parts_val is not None else content_val
        if blocks is None:
            out.append(Message(role=role, content=None))
            continue
        if not isinstance(blocks, list):
            raise TypeError(
                f"vercel message {i}: 'content'/'parts' must be str or list, got {type(blocks).__name__}"
            )

        text_parts: List[str] = []
        reasoning_parts: List[str] = []
        tool_calls: List[ToolCall] = []
        tool_results: List[ToolResponse] = []

        for j, block in enumerate(blocks):
            if not isinstance(block, dict):
                raise TypeError(
                    f"vercel message {i} part {j} must be a dict, got {type(block).__name__}"
                )
            btype = block.get("type")
            if btype == "text":
                text = block.get("text")
                if not isinstance(text, str):
                    raise ValueError(
                        f"vercel message {i} part {j} is type='text' but 'text' is missing or not a string"
                    )
                text_parts.append(text)
            elif btype == "reasoning":
                reasoning = block.get("text") or block.get("reasoning")
                if not isinstance(reasoning, str):
                    raise ValueError(
                        f"vercel message {i} part {j} is type='reasoning' but no 'text'/'reasoning' string"
                    )
                reasoning_parts.append(reasoning)
            elif btype in ("tool-invocation", "tool-call"):
                tool_calls.append(
                    _parse_vercel_tool_call(block, message_index=i, part_index=j)
                )
            elif btype == "tool-result":
                tool_results.append(
                    _parse_vercel_tool_result(block, message_index=i, part_index=j)
                )
            else:
                raise ValueError(
                    f"vercel message {i} part {j} has unsupported type {btype!r}; "
                    "expected 'text', 'reasoning', 'tool-invocation', 'tool-call', or 'tool-result'"
                )

        primary_content = "\n".join(text_parts) if text_parts else None
        primary_reasoning = "\n".join(reasoning_parts) if reasoning_parts else None
        has_primary = (
            primary_content is not None or tool_calls or primary_reasoning is not None
        )

        if has_primary:
            out.append(
                Message(
                    role=role,
                    content=primary_content,
                    tool_calls=tool_calls or None,
                    reasoning=primary_reasoning,
                )
            )

        for tr in tool_results:
            out.append(Message(role="tool", content=tr.response, tool_response=tr))

        if not has_primary and not tool_results:
            out.append(Message(role=role, content=None))

    return out


def _parse_vercel_tool_call(
    block: Dict[str, Any], *, message_index: int, part_index: int
) -> ToolCall:
    """Extract a :class:`ToolCall` from a Vercel ``tool-invocation``/``tool-call`` part.

    Vercel wraps the call either as an ``invocation`` dict with
    ``toolCallId`` / ``toolName`` / ``args``, or as a flat block with those
    keys at the top level (``CoreMessage`` shape). Both are accepted.
    """
    invocation = (
        block.get("toolInvocation")
        if isinstance(block.get("toolInvocation"), dict)
        else block
    )
    name = invocation.get("toolName") or invocation.get("name")
    if not name:
        raise ValueError(
            f"vercel message {message_index} part {part_index}: tool call missing 'toolName'/'name'"
        )
    args_raw = (
        invocation.get("args") if "args" in invocation else invocation.get("input")
    )
    args = args_raw if isinstance(args_raw, dict) else parse_tool_arguments(args_raw)
    return ToolCall(
        name=name,
        arguments=args,
        id=invocation.get("toolCallId") or invocation.get("id"),
    )


def _parse_vercel_tool_result(
    block: Dict[str, Any], *, message_index: int, part_index: int
) -> ToolResponse:
    tool_call_id = block.get("toolCallId") or block.get("id")
    if not tool_call_id:
        raise ValueError(
            f"vercel message {message_index} part {part_index}: tool-result missing 'toolCallId'/'id'"
        )
    result = block.get("result") if "result" in block else block.get("output")
    # Tool results are opaque payloads (often structured JSON like
    # ``{"temp": 72}``); delegate to the dedicated serializer instead of the
    # strict multimodal flattener.
    response_text = _tool_result_to_response_text(result)
    is_error = bool(block.get("isError") or block.get("is_error"))
    return ToolResponse(
        id=tool_call_id,
        name=block.get("toolName") or block.get("name") or "",
        arguments={},
        response=response_text,
        error="tool_error" if is_error else None,
    )


def messages_from_prompt_response(
    prompt: str,
    response: str,
    *,
    system: Optional[str] = None,
) -> List[Message]:
    """Two- or three-turn journey from flat prompt/response strings."""
    msgs: List[Message] = []
    if system:
        msgs.append(Message(role="system", content=system))
    msgs.append(Message(role="user", content=prompt))
    msgs.append(Message(role="assistant", content=response))
    return msgs


def messages_from_role_content_pairs(
    pairs: Iterable[tuple[str, str]],
) -> List[Message]:
    """Build Messages from ``[(role, content), ...]`` tuples.

    Roles are normalized via :func:`normalize_role`.
    """
    return [
        Message(role=normalize_role(role), content=content) for role, content in pairs
    ]
