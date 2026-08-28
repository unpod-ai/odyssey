"""OpenTelemetry span → journey bridge — items 0.11 / 0'.3.

::

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry import trace
    from odyssey.integrations.otel import OdysseySpanProcessor

    provider = TracerProvider()
    provider.add_span_processor(OdysseySpanProcessor())
    trace.set_tracer_provider(provider)

Every OTel-instrumented app now records for free — no per-framework wrapper,
because the framework already emits spans and this just listens for them.

**One journey per trace.** OTel's own ``trace_id`` is already the flattening
``integrations/langchain.py``'s callback handler has to build by hand out of
``run_id``/``parent_run_id`` (see that module's docstring) — every span
sharing a ``trace_id`` belongs to the same journey, no root-tracking of our
own needed. The journey closes when the span with no parent (the trace's own
root, in this process) ends; its status (``StatusCode.ERROR`` or not)
becomes the journey's termination reason.

**What becomes a turn, and what doesn't.** A span only contributes a
``Message`` when it carries GenAI content — everything else (an
orchestration span, a retrieval span with no ``gen_ai.*`` attributes, ...)
only affects journey lifecycle, exactly the way a LangChain chain span
carries no turn of its own. Content is read in this priority order, matching
what is actually emitted in practice today:

1. ``gen_ai.input.messages`` / ``gen_ai.output.messages`` span
   **attributes** (the current, still-changing official semantic
   convention) — a JSON-encoded array of ``{"role", "content"}``-shaped
   objects, since span attributes cannot hold nested structures directly.
2. ``gen_ai.content.prompt`` / ``gen_ai.content.completion`` span
   **events**, each carrying a ``gen_ai.prompt``/``gen_ai.completion`` JSON
   string attribute — the older, still widely-implemented pattern
   (OpenLLMetry/Traceloop, and the official semconv before content moved
   off spans). Verified against a real span produced by the installed
   ``opentelemetry-sdk``, not guessed.
3. ``gen_ai.prompt`` / ``gen_ai.completion`` span attributes directly, the
   oldest shape, rarely seen now but cheap to also accept.

**What is explicitly not covered.** The GenAI semantic conventions are still
actively changing upstream — the installed ``opentelemetry-semantic-conventions``
package's own ``gen_ai_attributes`` module marks every single constant
"Deprecated: moved to the OpenTelemetry GenAI semantic conventions
repository" as of this writing, i.e. even the canonical attribute *names*
are mid-migration — and several instrumentation libraries emit their own,
incompatible vocabulary instead of the official one (OpenInference/Arize
Phoenix, used by LlamaIndex's own OTel integration, names things
``llm.input_messages.{i}.message.role``, for example). Only the ``gen_ai.*``
shapes above are handled; a span in another vocabulary still gets correct
journey lifecycle (open/close/error), just no turn content — a documented
scope cut, not silent data loss, since the span itself is still on record
wherever the OTel backend of choice keeps it. Token usage
(``gen_ai.usage.*``) and finish reasons are not lifted onto
``Message.usage``/``Message.finish_reason`` in this pass either, same
reasoning — visible on the span, not duplicated into the journey.

Requires ``opentelemetry-sdk`` (an optional extra: ``odyssey[otel]``),
imported lazily inside :func:`OdysseySpanProcessor` — never at module scope,
same discipline as every other integration in this package.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from odyssey.builders.messages import normalize_role
from odyssey.capture import JourneyHandle, _jsonable
from odyssey.client import require_client
from odyssey.context import JourneyContext, SeqAllocator, bind
from odyssey.primitives import Message, ToolCall

__all__ = ["OdysseySpanProcessor"]

_GEN_AI_SYSTEM_KEYS = ("gen_ai.system", "gen_ai.provider.name")
_GEN_AI_MODEL_KEYS = ("gen_ai.request.model", "gen_ai.response.model")


def _throwaway_allocator() -> SeqAllocator:
    return SeqAllocator(lambda _jid: None)


def _first(attributes: Dict[str, Any], keys: Sequence[str]) -> Optional[Any]:
    for key in keys:
        value = attributes.get(key)
        if value is not None:
            return value
    return None


def _input_messages_json(
    attributes: Dict[str, Any], events: Sequence[Tuple[str, Dict[str, Any]]]
) -> Optional[str]:
    value = attributes.get("gen_ai.input.messages")
    if isinstance(value, str):
        return value
    for name, ev_attrs in events:
        if name == "gen_ai.content.prompt":
            prompt = ev_attrs.get("gen_ai.prompt")
            if isinstance(prompt, str):
                return prompt
    value = attributes.get("gen_ai.prompt")
    return value if isinstance(value, str) else None


def _output_messages_json(
    attributes: Dict[str, Any], events: Sequence[Tuple[str, Dict[str, Any]]]
) -> Optional[str]:
    value = attributes.get("gen_ai.output.messages")
    if isinstance(value, str):
        return value
    for name, ev_attrs in events:
        if name == "gen_ai.content.completion":
            completion = ev_attrs.get("gen_ai.completion")
            if isinstance(completion, str):
                return completion
    value = attributes.get("gen_ai.completion")
    return value if isinstance(value, str) else None


def _message_from_gen_ai_entry(entry: Any) -> Optional[Message]:
    """One ``{"role", "content", ["tool_calls"]}``-shaped dict → a
    :class:`Message`, tolerant of the content-shape variance different
    instrumentation libraries produce (a plain string, OpenAI-style content
    blocks, or an arbitrary JSON value dumped back to text).
    """
    if not isinstance(entry, dict):
        return None
    try:
        role = normalize_role(entry.get("role"))
    except ValueError:
        role = "assistant"

    content = entry.get("content")
    if isinstance(content, list):
        text_parts: List[str] = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text") or part.get("content")
                if isinstance(text, str):
                    text_parts.append(text)
            elif isinstance(part, str):
                text_parts.append(part)
        content = "\n".join(text_parts) if text_parts else None
    elif content is not None and not isinstance(content, str):
        content = json.dumps(content)

    tool_calls: Optional[List[ToolCall]] = None
    raw_calls = entry.get("tool_calls")
    if isinstance(raw_calls, list):
        parsed: List[ToolCall] = []
        for tc in raw_calls:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
            name = fn.get("name") if isinstance(fn, dict) else None
            if not name:
                continue
            args = fn.get("arguments") if isinstance(fn, dict) else None
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (TypeError, ValueError):
                    args = {"raw": args}
            parsed.append(ToolCall(name=name, arguments=args or {}, id=tc.get("id")))
        tool_calls = parsed or None

    if content is None and not tool_calls:
        return None
    return Message(role=role, content=content, tool_calls=tool_calls)


def _messages_from_gen_ai_json(raw: str) -> List[Message]:
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        return []
    out: List[Message] = []
    for entry in parsed:
        message = _message_from_gen_ai_entry(entry)
        if message is not None:
            out.append(message)
    return out


class _Recorder:
    """The capture logic, kept independent of the real ``SpanProcessor``
    base class so it is unit-testable without the optional
    ``opentelemetry-sdk`` dependency — only :func:`OdysseySpanProcessor`
    needs it, mirroring ``integrations/langchain.py``'s ``_Recorder``."""

    def __init__(self, *, data_source: str, metadata: Optional[Dict[str, Any]]) -> None:
        self._data_source = data_source
        self._metadata = metadata or {}
        self._journeys: Dict[str, JourneyContext] = {}

    def _guard(self, label: str, fn: Callable[[], None]) -> None:
        """Run a capture step from inside an OTel callback. Never raises —
        an exception here must not break the app being observed."""
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - capture is best-effort
            client = require_client()
            if client is not None:
                client.note_error(f"otel.{label}", exc)

    def _ctx_for(self, trace_id: str) -> JourneyContext:
        ctx = self._journeys.get(trace_id)
        if ctx is not None:
            return ctx
        client = require_client()
        ctx = JourneyContext(
            journey_id=trace_id,
            allocator=(
                client.allocator if client is not None else _throwaway_allocator()
            ),
            metadata=_jsonable(dict(self._metadata)),
            data_source=self._data_source,
            trace_id=trace_id,
        )
        self._journeys[trace_id] = ctx
        if client is not None:
            client.count_journey()
        return ctx

    def on_start(self, trace_id: str) -> None:
        # Eagerly open the journey (not lazily on first message) so a trace
        # that ends without any GenAI content still gets a diagnosable
        # terminal event, the same way `with odyssey.journey():` always
        # opens a real context whether or not the block records anything.
        self._guard("on_start", lambda: self._ctx_for(trace_id))

    def on_end(
        self,
        *,
        trace_id: str,
        is_root: bool,
        attributes: Dict[str, Any],
        events: Sequence[Tuple[str, Dict[str, Any]]],
        ok: bool,
        description: Optional[str],
    ) -> None:
        def go() -> None:
            ctx = self._ctx_for(trace_id)
            with bind(ctx):
                self._emit_gen_ai(JourneyHandle(ctx), attributes, events)
            if is_root:
                self._end(trace_id, ok=ok, description=description)

        self._guard("on_end", go)

    def _emit_gen_ai(
        self,
        handle: JourneyHandle,
        attributes: Dict[str, Any],
        events: Sequence[Tuple[str, Dict[str, Any]]],
    ) -> None:
        system = _first(attributes, _GEN_AI_SYSTEM_KEYS)
        model = _first(attributes, _GEN_AI_MODEL_KEYS)
        if system is None and model is None:
            return  # not a GenAI span -- lifecycle only, no turn to record

        meta = {"gen_ai.system": system} if system is not None else None
        model_id = str(model) if model is not None else None

        input_json = _input_messages_json(attributes, events)
        if input_json:
            for message in self._parse(input_json):
                handle.message(message, model_id=model_id, metadata=meta)

        output_json = _output_messages_json(attributes, events)
        if output_json:
            for message in self._parse(output_json):
                handle.message(message, model_id=model_id, metadata=meta)

    def _parse(self, raw_json: str) -> List[Message]:
        try:
            return _messages_from_gen_ai_json(raw_json)
        except (TypeError, ValueError) as exc:
            client = require_client()
            if client is not None:
                client.note_error("otel.parse_gen_ai_content", exc)
            return []

    def _end(self, trace_id: str, *, ok: bool, description: Optional[str]) -> None:
        ctx = self._journeys.pop(trace_id, None)
        if ctx is None or ctx.terminated:
            return
        with bind(ctx):
            JourneyHandle(ctx).close(
                reason="ENV_DONE" if ok else "ERROR",
                error=None if ok else description,
            )


def OdysseySpanProcessor(
    *, data_source: str = "otel", metadata: Optional[Dict[str, Any]] = None
) -> Any:
    """Build an ``opentelemetry.sdk.trace.SpanProcessor`` that records every
    span's trace as one journey.

    A factory, not a class — see the module docstring for why
    ``opentelemetry-sdk`` can only be imported here, inside this call, rather
    than at module scope. Mirrors ``integrations/langchain.py``'s
    ``OdysseyCallbackHandler()`` for the same reason: the base class itself
    is only importable once the optional dependency is present.
    """
    # pyrefly: ignore[missing-import]  — optional extra, `odyssey[otel]`.
    from opentelemetry.sdk.trace import SpanProcessor

    # pyrefly: ignore[missing-import]  — optional extra, see above.
    from opentelemetry.trace import StatusCode

    recorder = _Recorder(data_source=data_source, metadata=metadata)

    class _Processor(SpanProcessor):
        def on_start(self, span: Any, parent_context: Any = None) -> None:
            trace_id = format(span.get_span_context().trace_id, "032x")
            recorder.on_start(trace_id)

        def on_end(self, span: Any) -> None:
            trace_id = format(span.context.trace_id, "032x")
            attributes = dict(span.attributes or {})
            events = [
                (event.name, dict(event.attributes or {})) for event in span.events
            ]
            status = span.status
            ok = status is None or status.status_code != StatusCode.ERROR
            recorder.on_end(
                trace_id=trace_id,
                is_root=span.parent is None,
                attributes=attributes,
                events=events,
                ok=ok,
                description=None if status is None else status.description,
            )

        def shutdown(self) -> None:
            pass

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return True

    return _Processor()
