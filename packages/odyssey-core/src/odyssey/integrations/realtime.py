"""Speech-to-speech capture — OpenAI/Azure Realtime and Gemini Live.

A realtime session is a websocket, not a call. Nothing issues a
``chat.completions.create`` for a patch to sit under: audio goes up, audio comes
down, and the conversation only exists as the events the server sends back. So
capture comes from those events, exactly the way
``integrations/livekit.py`` and ``integrations/pipecat.py`` take it from session
events and pipeline frames — and it produces the same corpus shape they do:
turns, tool calls, interruptions, latency. Not a raw event log; an event log is
not a training example.

Inside LiveKit or Pipecat there is nothing to do here. ``AgentSession`` emits
``conversation_item_added`` whether its LLM is ``openai.realtime.RealtimeModel``
or a chat model, and a Pipecat pipeline pushes the same frames whether the LLM
slot holds ``GeminiMultimodalLiveLLMService`` or ``OpenAILLMService`` — both
recorders are provider-agnostic by construction. This module is for the app that
owns the websocket itself::

    from odyssey.integrations import realtime

    async with client.beta.realtime.connect(model="gpt-realtime") as conn:
        rec = realtime.attach(journey_id=call_id)
        async for event in conn:
            rec.event(event)
            ...                      # the app's own handling, unchanged
        rec.close()

or, when the app just iterates the session and wants nothing else to change::

    async for event in realtime.observe(conn, journey_id=call_id):
        ...

Both vendors are read by the same recorder. OpenAI and Azure send discriminated
events (``{"type": "response.audio_transcript.done", ...}``); Gemini Live sends
messages with ``server_content``/``tool_call`` attributes and no type at all, so
the shape itself selects the reader. Everything is read by attribute *or* key,
because the same session is a pydantic object through the SDK and a plain dict
through a bare websocket, and an event newer than this build must not raise.

What is recorded
----------------

- **User turns** from input-audio transcription — the committed transcript
  only. OpenAI's ``...transcription.delta`` and Gemini's partial
  ``input_transcription`` fragments are accumulated, never emitted on their own:
  a corpus of prefixes of one sentence is not a corpus of turns.
- **Assistant turns** from the output transcript, one message per response
  rather than one per delta, for the same reason.
- **Tool calls**, and the results the app sends back through
  :meth:`RealtimeRecorder.tool_result` — a result travels *up* the socket, so
  it is the one part of the conversation the server never tells us about.
- **Interruptions.** Barge-in truncates the reply, so the turn is marked
  ``interrupted`` and is not a training target.
- **Latency.** ``ttft_ms`` is measured from the moment the caller stopped
  talking to the first word back, which is what a voice deployment is tuned
  against, and ``latency_ms`` to the end of the reply.
- **The system prompt**, off ``session.instructions`` in the session events, and
  again whenever it changes mid-call.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

from odyssey.capture import JourneyHandle, _jsonable
from odyssey.client import require_client
from odyssey.context import JourneyContext, SeqAllocator, bind
from odyssey.integrations._streams import ObservedAsyncStream
from odyssey.integrations._timing import Timer
from odyssey.primitives import (
    Message,
    Role,
    TerminationReason,
    ToolCall,
    ToolResponse,
)

__all__ = ["RealtimeRecorder", "attach", "observe"]


def _throwaway_allocator() -> SeqAllocator:
    return SeqAllocator(lambda _jid: None)


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` off an object or a dict. Never raises."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _arguments(raw: Any) -> Dict[str, Any]:
    """A tool call's arguments, whether they arrived as JSON text or a dict."""
    if isinstance(raw, dict):
        return _jsonable(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {"_unparsed": raw}
        return parsed if isinstance(parsed, dict) else {"value": _jsonable(parsed)}
    return {}


class _Turn:
    """A turn being assembled from the fragments it arrived in."""

    __slots__ = ("role", "parts", "final", "interrupted")

    def __init__(self, role: Role) -> None:
        self.role: Role = role
        self.parts: List[str] = []
        # The whole transcript, when the server sends one at the end. It wins
        # over the fragments: a vendor that also sent deltas sent the same text
        # twice, and one that corrected itself corrected it here.
        self.final: Optional[str] = None
        self.interrupted = False

    @property
    def content(self) -> Optional[str]:
        if self.final is not None:
            return self.final or None
        # Joined with no separator: these are pieces of one sentence, and the
        # vendor's own fragments already carry their spaces.
        return "".join(self.parts) or None

    def is_empty(self) -> bool:
        return not self.content


class RealtimeRecorder:
    """Records one realtime session into one journey.

    Created by :func:`attach`. Holds the journey explicitly rather than in the
    ambient context, for the same reason the LiveKit recorder does: one process
    runs many concurrent calls, and callbacks fire from whichever task owns the
    socket.
    """

    def __init__(
        self,
        *,
        journey_id: str,
        provider: Optional[str] = None,
        record_instructions: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.journey_id = journey_id
        self._provider = provider
        self._record_instructions = record_instructions
        self._closed = False
        self._pending: Optional[_Turn] = None
        self._user: Optional[_Turn] = None
        self._instructions: Optional[str] = None
        # Started when the caller stops talking, read when the reply lands.
        self._timer: Optional[Timer] = None
        # call_id -> the tool call recorded, so a result can name its call.
        self._calls: Dict[str, str] = {}

        client = require_client()
        self._enabled = client is not None and client.config.enabled
        self._ctx = JourneyContext(
            journey_id=journey_id,
            allocator=(
                client.allocator if client is not None else _throwaway_allocator()
            ),
            metadata=_jsonable(dict(metadata or {})),
            data_source="realtime",
            framework="realtime",
        )
        if client is not None:
            client.count_journey()
            # So process shutdown ends this journey if the socket died without
            # anything closing it -- a dropped call reaches no `close()`, and a
            # journey with no terminal event is refused by `fold()` forever.
            client.register_journey(self)

    # -- plumbing ---------------------------------------------------------

    @property
    def context(self) -> JourneyContext:
        return self._ctx

    def _handle(self) -> JourneyHandle:
        return JourneyHandle(self._ctx)

    def _guard(self, label: str, fn: Callable[[], None]) -> None:
        """Run a capture step. Never raises: an exception here would propagate
        into the app's own event loop and can take the call down. Losing a
        recorded turn is acceptable; dropping a live call to record it is not."""
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - see docstring
            client = require_client()
            if client is not None:
                client.note_error(f"realtime.{label}", exc)

    # -- the entry point ---------------------------------------------------

    def event(self, event: Any) -> None:
        """Feed one server event. The whole integration funnels through here."""
        if not self._enabled or self._closed or event is None:
            return
        self._guard("event", lambda: self._dispatch(event))

    def _dispatch(self, event: Any) -> None:
        kind = _get(event, "type")
        if isinstance(kind, str):
            self._openai(kind, event)
            return
        # Gemini Live: no discriminator, the populated attribute is the event.
        self._gemini(event)

    # -- OpenAI and Azure --------------------------------------------------

    def _openai(self, kind: str, event: Any) -> None:
        if kind.startswith("conversation.item.input_audio_transcription"):
            self._user_text(
                _text(_get(event, "delta")),
                final=_text(_get(event, "transcript")),
                done=kind.endswith(".completed"),
            )
        elif kind in ("session.created", "session.updated"):
            self._sync_instructions(_get(_get(event, "session"), "instructions"))
        elif kind == "response.created":
            self._start_response()
        elif kind in ("response.audio_transcript.delta", "response.text.delta"):
            self._assistant_text(_text(_get(event, "delta")))
        elif kind in ("response.audio_transcript.done", "response.text.done"):
            self._assistant_text(
                None,
                final=_text(_get(event, "transcript")) or _text(_get(event, "text")),
            )
        elif kind == "response.function_call_arguments.done":
            self._record_call(
                call_id=_get(event, "call_id"),
                name=_get(event, "name"),
                arguments=_get(event, "arguments"),
            )
        elif kind == "response.done":
            self._response_done(_get(event, "response"))
        elif kind == "input_audio_buffer.speech_started":
            self._interrupt()
        elif kind == "error":
            self._note_error(_get(event, "error"))

    def _response_done(self, response: Any) -> None:
        """The reply is complete — or was cut off, which the status says."""
        status = _get(response, "status")
        if isinstance(status, str) and status in ("cancelled", "incomplete"):
            self._interrupt()
        for item in _get(response, "output") or []:
            if _get(item, "type") == "function_call":
                self._record_call(
                    call_id=_get(item, "call_id") or _get(item, "id"),
                    name=_get(item, "name"),
                    arguments=_get(item, "arguments"),
                )
        self.flush(usage=_usage(_get(response, "usage")))

    def _note_error(self, error: Any) -> None:
        client = require_client()
        if client is None:
            return
        message = _get(error, "message") or error
        client.note_error("realtime.session", RuntimeWarning(str(message)))

    # -- Gemini Live -------------------------------------------------------

    def _gemini(self, event: Any) -> None:
        content = _get(event, "server_content") or _get(event, "serverContent")
        if content is not None:
            self._gemini_content(content)
        tool_call = _get(event, "tool_call") or _get(event, "toolCall")
        if tool_call is not None:
            for call in (
                _get(tool_call, "function_calls")
                or _get(tool_call, "functionCalls")
                or []
            ):
                self._record_call(
                    call_id=_get(call, "id"),
                    name=_get(call, "name"),
                    arguments=_get(call, "args"),
                )

    def _gemini_content(self, content: Any) -> None:
        heard = _get(content, "input_transcription") or _get(
            content, "inputTranscription"
        )
        if heard is not None:
            self._user_text(_text(_get(heard, "text")))
        said = _get(content, "output_transcription") or _get(
            content, "outputTranscription"
        )
        if said is not None:
            self._assistant_text(_text(_get(said, "text")))
        turn = _get(content, "model_turn") or _get(content, "modelTurn")
        if turn is not None:
            for part in _get(turn, "parts") or []:
                self._assistant_text(_text(_get(part, "text")))
        if _get(content, "interrupted"):
            self._interrupt()
        if _get(content, "turn_complete") or _get(content, "turnComplete"):
            self.flush()

    # -- turns -------------------------------------------------------------

    def _user_text(
        self, delta: Optional[str], *, final: Optional[str] = None, done: bool = True
    ) -> None:
        """A fragment of what the caller said, and whether that was all of it.

        Gemini streams the input transcript with no completion event, so a user
        turn is flushed by what follows it: the reply, or the end of the call.
        """
        if delta is None and final is None:
            return
        # The caller speaking again ends whatever the model was still saying.
        self.flush()
        if self._user is None:
            self._user = _Turn("user")
        if delta is not None:
            self._user.parts.append(delta)
        if final is not None:
            self._user.final = final
        if done and final is not None:
            self._flush_user()

    def _flush_user(self) -> None:
        turn, self._user = self._user, None
        if turn is None or turn.is_empty():
            return
        with bind(self._ctx):
            self._handle().message(
                Message(role="user", content=turn.content, provider=self._provider)
            )
        # The caller has stopped talking; everything after this is the wait.
        self._timer = Timer()

    def _start_response(self) -> None:
        self._flush_user()
        if self._timer is None:
            # A greeting: nothing preceded it, so the reply is its own clock.
            self._timer = Timer()
        if self._pending is None:
            self._pending = _Turn("assistant")

    def _assistant_text(
        self, delta: Optional[str], *, final: Optional[str] = None
    ) -> None:
        if delta is None and final is None:
            return
        self._start_response()
        assert self._pending is not None
        if self._timer is not None:
            self._timer.first_token()
        if delta is not None:
            self._pending.parts.append(delta)
        if final is not None:
            self._pending.final = final

    def _interrupt(self) -> None:
        """Barge-in. Only counts against a reply that was actually in flight."""
        turn = self._pending
        if turn is None or turn.is_empty():
            return
        turn.interrupted = True
        with bind(self._ctx):
            self._handle().voice("barge_in", text=turn.content)
        self.flush()

    def flush(self, *, usage: Optional[Dict[str, int]] = None) -> None:
        """Write the reply being assembled, if any. Idempotent.

        Public for the same reason LiveKit's is: a caller reading the journey
        mid-call would otherwise be one turn behind.
        """
        turn, self._pending = self._pending, None
        if turn is None or turn.is_empty():
            return
        timer, self._timer = self._timer, None
        meta: Dict[str, Any] = {}
        if turn.interrupted:
            # What stops the fold treating a cut-off reply as a training target.
            meta["interrupted"] = True
        with bind(self._ctx):
            self._handle().message(
                Message(
                    role="assistant",
                    content=turn.content,
                    metadata=meta or None,
                    usage=usage,
                    provider=self._provider,
                    latency_ms=timer.latency_ms if timer is not None else None,
                    ttft_ms=timer.ttft_ms if timer is not None else None,
                )
            )

    # -- tools -------------------------------------------------------------

    def _record_call(self, *, call_id: Any, name: Any, arguments: Any) -> None:
        if not name:
            return
        cid = str(call_id or name)
        if cid in self._calls:
            # The same call announced twice -- `response.done` repeats what
            # `response.function_call_arguments.done` already carried.
            return
        self._calls[cid] = str(name)
        # Speech that preceded the call belongs to the same generation, so it
        # rides on the tool-call message rather than becoming a turn of its own.
        turn, self._pending = self._pending, None
        timer, self._timer = self._timer, None
        with bind(self._ctx):
            self._handle().message(
                Message(
                    role="assistant",
                    content=turn.content if turn is not None else None,
                    tool_calls=[
                        ToolCall(
                            id=cid, name=str(name), arguments=_arguments(arguments)
                        )
                    ],
                    provider=self._provider,
                    latency_ms=timer.latency_ms if timer is not None else None,
                    ttft_ms=timer.ttft_ms if timer is not None else None,
                )
            )

    def tool_result(
        self, call_id: Any, response: Any, *, name: Optional[str] = None
    ) -> None:
        """Record what the app sent back for a tool call.

        A result travels *up* the socket, so it is the one half of a tool turn
        the server never reports — without this the journey has calls that were
        never answered, and a fold cannot tell that apart from a tool that
        failed.
        """
        if self._closed or not self._enabled:
            return

        def go() -> None:
            cid = str(call_id)
            with bind(self._ctx):
                self._handle().message(
                    Message(
                        role="tool",
                        content=_result_text(response),
                        tool_response=ToolResponse(
                            id=cid,
                            name=name or self._calls.get(cid, ""),
                            arguments={},
                            response=_result_text(response),
                        ),
                    )
                )

        self._guard("tool_result", go)

    # -- the system prompt -------------------------------------------------

    def _sync_instructions(self, instructions: Any) -> None:
        """Record the prompt the session was configured with, and each change.

        A journey without it is not a training example: the step would hold the
        exchange but not what the model was told to do, and the same caller turn
        under two different prompts would look identical.
        """
        text = _text(instructions)
        if not self._record_instructions or text is None or text == self._instructions:
            return
        self._instructions = text
        self.flush()
        with bind(self._ctx):
            self._handle().message(Message(role="system", content=text))

    # -- lifecycle ---------------------------------------------------------

    def close(
        self,
        *,
        reason: TerminationReason = "ENV_DONE",
        error: Optional[str] = None,
    ) -> None:
        """End the journey. Idempotent, and safe to call by hand.

        Until this lands the journey has no terminal event, so ``fold()``
        reports it as possibly-still-running and refuses to export it.
        """
        if self._closed:
            return
        self._closed = True
        # The sign-off is the last turn of the call, and nothing else is coming
        # along to flush it.
        self._guard("close.flush", self.flush)
        self._guard("close.user", self._flush_user)
        with bind(self._ctx):
            self._handle().close(reason=reason, error=error)
        client = require_client()
        if client is not None:
            client.unregister_journey(self)

    # -- signals, for the app to call --------------------------------------

    def signal(self, kind: str, **kw: Any) -> Optional[int]:
        """Attach caller feedback — a thumbs-up, a rating, a regeneration."""
        self.flush()
        with bind(self._ctx):
            return self._handle().signal(kind, **kw)  # type: ignore[arg-type]

    def reward(self, value: Any) -> Optional[int]:
        self.flush()
        with bind(self._ctx):
            return self._handle().reward(value)


def _usage(usage: Any) -> Optional[Dict[str, int]]:
    """Token counts, in the spelling the rest of the corpus uses."""
    if usage is None:
        return None
    out: Dict[str, int] = {}
    for name, keys in (
        ("input_tokens", ("input_tokens", "prompt_token_count", "promptTokenCount")),
        (
            "output_tokens",
            ("output_tokens", "candidates_token_count", "candidatesTokenCount"),
        ),
        ("total_tokens", ("total_tokens", "total_token_count", "totalTokenCount")),
    ):
        for key in keys:
            value = _get(usage, key)
            if isinstance(value, int):
                out[name] = value
                break
    return out or None


def _result_text(response: Any) -> Optional[str]:
    if response is None or isinstance(response, str):
        return response
    try:
        return json.dumps(_jsonable(response))
    except (TypeError, ValueError):
        return str(response)


def attach(
    *,
    journey_id: str,
    provider: Optional[str] = None,
    record_instructions: bool = True,
    **metadata: Any,
) -> RealtimeRecorder:
    """Record one realtime session into ``journey_id``.

    Returns the recorder; feed it every server event with
    :meth:`RealtimeRecorder.event` and end it with
    :meth:`RealtimeRecorder.close`. ``journey_id`` is the caller's to choose
    because a journey boundary is domain knowledge — the platform's own call id
    is usually right, and it also makes recording idempotent across a restart.

    ``provider`` names who served the session (``"openai"``, ``"azure"``,
    ``"gemini"``) for ``Message.provider``. The events themselves do not say:
    Azure speaks OpenAI's protocol verbatim, and only the endpoint differs.

    Requires :func:`odyssey.init` to have run. Without it, recording is a no-op
    and one warning is emitted; the session is unaffected either way.
    """
    return RealtimeRecorder(
        journey_id=journey_id,
        provider=provider,
        record_instructions=record_instructions,
        metadata=metadata,
    )


def observe(
    events: Any,
    *,
    journey_id: str,
    provider: Optional[str] = None,
    record_instructions: bool = True,
    **metadata: Any,
) -> Any:
    """The session's event stream, recorded as the app iterates it.

    For the common loop, where the app reads events and nothing else has to
    change::

        async for event in realtime.observe(conn, journey_id=call_id):
            ...

    The journey is closed when the stream ends — drained, cancelled (a caller
    hanging up mid-reply) or failed, with the reason each of those implies. The
    recorder is available as ``.recorder`` for :meth:`RealtimeRecorder.signal`
    and :meth:`RealtimeRecorder.tool_result`.
    """
    recorder = attach(
        journey_id=journey_id,
        provider=provider,
        record_instructions=record_instructions,
        **metadata,
    )

    def ended(exc: Optional[BaseException]) -> None:
        if exc is None:
            recorder.close()
        else:
            recorder.close(reason="TRUNCATION", error=f"{type(exc).__name__}: {exc}")

    stream = ObservedAsyncStream(events, recorder.event, ended)
    stream.recorder = recorder  # type: ignore[attr-defined]
    return stream
