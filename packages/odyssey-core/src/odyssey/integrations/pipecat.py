"""Pipecat capture — one ``attach()`` per pipeline task, provider-agnostic.

Why an observer rather than a provider wrapper
----------------------------------------------

A Pipecat voice agent does not call an LLM SDK in a way a client wrapper can
see. The pipeline is a chain of ``FrameProcessor``s, and which service sits in
the LLM slot is a deployment choice — ``OpenAILLMService``, ``AnthropicLLMService``,
``GeminiMultimodalLiveLLMService``, an inference gateway. Patching a provider SDK
would capture some deployments and silently miss others, which is the worst
possible outcome for a corpus.

Pipecat instead exposes the pipeline itself. ``BaseObserver`` sees every frame
that moves between processors without a processor being inserted into the chain,
so this captures the conversation regardless of what is behind the LLM and keeps
working when the deployment swaps services. That is the single integration
point::

    from pipecat.pipeline.task import PipelineTask
    import odyssey.integrations.pipecat as odyssey_pipecat

    task = PipelineTask(pipeline, params=params)
    odyssey_pipecat.attach(task, journey_id=call_id)
    #                     ^ nothing else in the agent changes

``attach`` also accepts the ``observers=`` construction path, for a deployment
that builds its task in one expression::

    task = PipelineTask(pipeline, observers=[odyssey_pipecat.observer(journey_id=call_id)])

Frames consumed
---------------

- ``TranscriptionFrame`` — the user's turn. ``InterimTranscriptionFrame`` is
  deliberately **not** consumed: it fires per partial hypothesis while the
  caller is still speaking, so consuming it would flood the spool with prefixes
  of one sentence, and its final text is what ``TranscriptionFrame`` delivers.
- ``LLMFullResponseStartFrame`` / ``LLMTextFrame`` / ``LLMFullResponseEndFrame``
  — the assistant's turn, assembled from the token chunks it streamed in as.
  One message per turn, never one per chunk: a corpus of token fragments is not
  a corpus of turns.
- ``FunctionCallInProgressFrame`` / ``FunctionCallResultFrame`` — tool calls
  paired with their outputs, correlated by ``tool_call_id``.
- ``MetricsFrame`` — the latency budget Pipecat already measures. ``TTFB`` is
  what the caller waited for before hearing anything, and it is the number a
  voice deployment is actually tuned against.
- ``StartInterruptionFrame`` / ``InterruptionFrame`` — barge-in, which marks the
  interrupted reply as a truncated one rather than a training target.
- ``EndFrame`` / ``CancelFrame`` — ends the journey, which is what makes it
  foldable.

The same frame is seen many times
---------------------------------

``on_push_frame`` fires once per *hop*, so a single ``TranscriptionFrame``
travelling through a six-processor pipeline is observed six times. Recording on
every observation would multiply the corpus by the pipeline's length — a
corruption that looks like a busy call rather than like a bug. Every frame is
therefore recorded at most once, keyed on ``frame.id``, and the ids seen are
held in a bounded ring so a long call cannot grow the set without limit.

Nothing here imports Pipecat
----------------------------

Frames are dispatched on class name and read by attribute, the same discipline
``integrations/livekit.py`` follows, so ``odyssey-core``'s ``dependencies = []``
stays true and a frame type newer than this build does not raise. The one
optional import is ``BaseObserver`` itself, attempted inside :func:`observer`
and falling back to a plain object — Pipecat's ``add_observer`` appends to a
list, so the base class is a type-checking convenience rather than a
requirement.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional, Tuple

from odyssey.capture import JourneyHandle, _jsonable
from odyssey.client import require_client
from odyssey.context import JourneyContext, SeqAllocator, bind
from odyssey.primitives import (
    Message,
    Role,
    TerminationReason,
    ToolCall,
    ToolResponse,
)

# How many frame ids to remember. A frame is observed once per pipeline hop, so
# the window only has to outlive one frame's trip through the chain -- this is
# three orders of magnitude more than that, and still bounded for a call that
# runs for hours.
_SEEN_LIMIT = 4096

# Frames that end the journey, and what they mean. `EndFrame` is the graceful
# path; `CancelFrame` is the caller or the platform pulling the plug.
_CLOSE_FRAMES: Dict[str, TerminationReason] = {
    "EndFrame": "ENV_DONE",
    "CancelFrame": "TRUNCATION",
}

# Barge-in. Pipecat has renamed this frame across versions and both names are
# still in the wild, so both are accepted rather than picking one and silently
# capturing nothing on the other.
_INTERRUPT_FRAMES = frozenset(
    {"StartInterruptionFrame", "InterruptionFrame", "BotInterruptionFrame"}
)

# What a metrics data object is measuring, decided by its class name. Pipecat
# ships TTFB/TTFA/Processing/LLMUsage/TTSUsage/TextAggregation/Turn data and the
# set grows; an unrecognised one falls through to `_stage_from_type`.
_METRIC_STAGES: Dict[str, str] = {
    "TTFBMetricsData": "ttfb",
    "TTFAMetricsData": "ttfa",
    "ProcessingMetricsData": "processing",
    "TextAggregationMetricsData": "aggregation",
}

# Usage data carries token counts rather than a duration, so it is recorded as
# metadata on the turn rather than as a latency reading with no latency in it.
_USAGE_DATA = frozenset({"LLMUsageMetricsData", "TTSUsageMetricsData"})


class _Turn:
    """An assistant turn being assembled from the chunks it streamed in as."""

    __slots__ = ("texts", "interrupted")

    def __init__(self) -> None:
        self.texts: List[str] = []
        self.interrupted = False

    @property
    def content(self) -> Optional[str]:
        # Joined with no separator: these are token fragments of one sentence,
        # not utterances. Pipecat's own chunks already carry their spaces.
        return "".join(self.texts) or None

    def is_empty(self) -> bool:
        return not self.texts


class PipecatRecorder:
    """Records one ``PipelineTask`` into one journey.

    Holds an explicit :class:`~odyssey.context.JourneyContext` and enters it
    with :func:`odyssey.context.bind` around each recorded call, rather than the
    ambient ``with journey():`` block — a pipeline's frames arrive on the task's
    own asyncio task, which is not the one that called ``attach``.
    """

    def __init__(
        self,
        *,
        journey_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.journey_id = journey_id
        self._closed = False
        self._pending: Optional[_Turn] = None
        # Frame ids already recorded. An OrderedDict used as a bounded set:
        # cheap membership, and the oldest id is the one to evict.
        self._seen: "OrderedDict[Any, None]" = OrderedDict()
        # Tool calls waiting for their result, keyed by `tool_call_id`.
        self._calls: Dict[str, Tuple[str, Dict[str, Any]]] = {}
        # Time-to-first-byte from the most recent LLM `MetricsFrame`, waiting
        # for the assistant turn it belongs to. Pipecat emits the metric when
        # the service produces its first token, which is before the response
        # ends, so it is available by the time the turn is flushed. Cleared on
        # consume, so a turn never inherits an older generation's number.
        self._pending_ttft: Optional[float] = None
        self._usage: Optional[Dict[str, int]] = None

        client = require_client()
        self._enabled = client is not None and client.config.enabled
        self._ctx = JourneyContext(
            journey_id=journey_id,
            allocator=(
                client.allocator if client is not None else _throwaway_allocator()
            ),
            # Sanitized at the door: these tags are snapshotted into the shard
            # header, which is json-dumped directly, and an unserializable one
            # would raise while opening the shard and drop every frame of the
            # call.
            metadata=_jsonable(dict(metadata or {})),
            data_source="pipecat",
            framework="pipecat",
        )
        if client is not None:
            client.count_journey()

    # -- plumbing ---------------------------------------------------------

    @property
    def context(self) -> JourneyContext:
        return self._ctx

    def _handle(self) -> JourneyHandle:
        return JourneyHandle(self._ctx)

    def _guard(self, label: str, fn: Callable[[], None]) -> None:
        """Run a capture step inside a Pipecat callback. Never raises.

        An exception escaping an observer propagates into the pipeline's task
        group, where it can tear down the call. Losing a recorded turn is
        acceptable; dropping a live call to record it is not.
        """
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - see docstring
            client = require_client()
            if client is not None:
                client.note_error(f"pipecat.{label}", exc)

    def _first_sighting(self, frame: Any) -> bool:
        """True the first time this frame is seen, False on every later hop.

        A frame with no readable id is treated as unseen every time rather than
        collapsed under a shared key: two distinct frames would otherwise be
        deduplicated into one, which loses a turn. Duplicating is recoverable on
        read; losing is not.
        """
        fid = getattr(frame, "id", None)
        if fid is None:
            return True
        if fid in self._seen:
            return False
        self._seen[fid] = None
        while len(self._seen) > _SEEN_LIMIT:
            self._seen.popitem(last=False)
        return True

    # -- the observer entry point ------------------------------------------

    def on_frame(self, frame: Any) -> None:
        """Dispatch one frame. The whole integration funnels through here."""
        if not self._enabled or self._closed or frame is None:
            return
        if not self._first_sighting(frame):
            return
        name = type(frame).__name__
        self._guard(name, lambda: self._dispatch(name, frame))

    def _dispatch(self, name: str, frame: Any) -> None:
        if name == "TranscriptionFrame":
            self._record_user(frame)
        elif name == "LLMFullResponseStartFrame":
            # A new generation. Flush whatever was still open rather than
            # merging two replies into one turn.
            self.flush()
            self._pending = _Turn()
        elif name in ("LLMTextFrame", "TTSTextFrame"):
            self._absorb(frame)
        elif name == "LLMFullResponseEndFrame":
            self.flush()
        elif name == "FunctionCallInProgressFrame":
            self._record_call(frame)
        elif name == "FunctionCallResultFrame":
            self._record_result(frame)
        elif name == "MetricsFrame":
            self._record_metrics(frame)
        elif name in _INTERRUPT_FRAMES:
            self._record_interruption(frame)
        elif name in _CLOSE_FRAMES:
            self.close(reason=_CLOSE_FRAMES[name])

    # -- turns -------------------------------------------------------------

    def _record_user(self, frame: Any) -> None:
        """The caller's turn. Only final transcripts reach here.

        ``finalized=False`` is a hypothesis the STT has not committed to, and
        recording one produces a turn the caller never finished saying.
        """
        if getattr(frame, "finalized", True) is False:
            return
        text = getattr(frame, "text", None)
        if not isinstance(text, str) or not text.strip():
            return
        # The user speaking ends whatever the bot was still saying.
        self.flush()
        meta: Dict[str, Any] = {}
        user_id = getattr(frame, "user_id", None)
        if user_id:
            meta["user_id"] = str(user_id)
        language = getattr(frame, "language", None)
        if language is not None:
            meta["language"] = str(getattr(language, "value", language))
        self._emit_message("user", text, meta or None)

    def _absorb(self, frame: Any) -> None:
        text = getattr(frame, "text", None)
        if not isinstance(text, str) or not text:
            return
        if self._pending is None:
            # A service that emits text without the response-start frame. Open
            # a turn anyway rather than dropping the reply.
            self._pending = _Turn()
        self._pending.texts.append(text)

    def flush(self) -> None:
        """Write the assistant turn being assembled, if any. Idempotent.

        Public for the same reason LiveKit's is: a caller reading the journey
        mid-call would otherwise be one turn behind.
        """
        turn = self._pending
        self._pending = None
        if turn is None or turn.is_empty():
            return
        ttft, self._pending_ttft = self._pending_ttft, None
        usage, self._usage = self._usage, None
        meta: Dict[str, Any] = {}
        if turn.interrupted:
            # What stops the fold treating a cut-off reply as a training target.
            meta["interrupted"] = True
        with bind(self._ctx):
            handle = self._handle()
            handle.message(
                Message(
                    role="assistant",
                    content=turn.content,
                    metadata=meta or None,
                    ttft_ms=ttft,
                    usage=usage,
                ),
            )
            if turn.interrupted:
                handle.voice("barge_in", text=turn.content)

    def _emit_message(
        self, role: Role, text: str, metadata: Optional[Dict[str, Any]]
    ) -> None:
        with bind(self._ctx):
            self._handle().message(Message(role=role, content=text, metadata=metadata))

    # -- tools -------------------------------------------------------------

    def _record_call(self, frame: Any) -> None:
        """Hold a tool call until its result arrives.

        Emitted as a pair rather than on sight, because a call with no outcome
        is not something a model should be trained to produce.
        """
        call_id = getattr(frame, "tool_call_id", None)
        name = getattr(frame, "function_name", None)
        if not call_id or not name:
            return
        self._calls[str(call_id)] = (str(name), _arguments(frame))

    def _record_result(self, frame: Any) -> None:
        call_id = str(getattr(frame, "tool_call_id", "") or "")
        name = getattr(frame, "function_name", None)
        held = self._calls.pop(call_id, None)
        if held is not None:
            name = name or held[0]
            arguments = held[1]
        else:
            arguments = _arguments(frame)
        if not name:
            return
        # The tool ran between the bot's words; the turn before it is finished.
        self.flush()
        result = _jsonable(getattr(frame, "result", None))
        with bind(self._ctx):
            handle = self._handle()
            handle.message(
                Message(
                    role="assistant",
                    tool_calls=[
                        ToolCall(
                            name=str(name),
                            arguments=arguments,
                            id=call_id or None,
                        )
                    ],
                ),
            )
            handle.message(
                Message(
                    role="tool",
                    tool_response=ToolResponse(
                        id=call_id or str(name),
                        name=str(name),
                        arguments=arguments,
                        response=result,
                    ),
                ),
            )

    # -- metrics -----------------------------------------------------------

    def _record_metrics(self, frame: Any) -> None:
        """Record Pipecat's own latency readings.

        This is the reason to consume ``MetricsFrame`` at all: Pipecat already
        measures the pipeline's latency budget, and without this every one of
        those numbers is computed and thrown away.

        Recorded as ``voice`` events rather than as messages — a latency reading
        is not a turn, and folding it into one would put a number the model
        never said into a training example.
        """
        data = getattr(frame, "data", None)
        if not isinstance(data, (list, tuple)):
            return
        for item in data:
            self._record_metric(item)

    def _record_metric(self, item: Any) -> None:
        name = type(item).__name__
        processor = getattr(item, "processor", None)
        model = getattr(item, "model", None)
        if name in _USAGE_DATA:
            self._absorb_usage(item)
            return
        value = getattr(item, "value", None)
        latency_ms = _seconds_to_ms(value)
        if latency_ms is None:
            return
        stage = _METRIC_STAGES.get(name) or _stage_from_type(item)
        meta: Dict[str, Any] = {"stage": stage}
        if processor:
            # Which processor produced it (`OpenAILLMService`, `CartesiaTTSService`).
            # The one thing that says *whose* latency this is when a deployment
            # swaps vendors mid-experiment.
            meta["processor"] = str(processor)
        if model:
            meta["model"] = str(model)
        if stage == "ttfb" and _is_llm(processor):
            # Time to first byte out of the LLM is the turn's TTFT. TTFB from a
            # TTS service measures a different thing and must not be attached to
            # the model's turn.
            self._pending_ttft = latency_ms
        with bind(self._ctx):
            self._handle().voice("latency", latency_ms=latency_ms, metadata=meta)

    def _absorb_usage(self, item: Any) -> None:
        """Token counts onto the turn, not into a latency event with no latency."""
        value = getattr(item, "value", None)
        counts = {
            key: getattr(value, key)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            if isinstance(getattr(value, key, None), int)
        }
        if counts:
            self._usage = {**(self._usage or {}), **counts}

    # -- barge-in ----------------------------------------------------------

    def _record_interruption(self, frame: Any) -> None:
        """Mark the reply in flight as cut off, and record the barge-in.

        A reply interrupted in its third sentence is a truncated reply, and
        training on it teaches the model to stop mid-thought.
        """
        if self._pending is not None and not self._pending.is_empty():
            self._pending.interrupted = True
            self.flush()
            return
        with bind(self._ctx):
            self._handle().voice("barge_in")

    # -- lifecycle ---------------------------------------------------------

    def close(
        self,
        *,
        reason: Optional[TerminationReason] = None,
        error: Optional[str] = None,
    ) -> None:
        """End the journey. Idempotent, and safe to call by hand.

        Until this lands the journey has no terminal event, so ``fold()``
        reports it as possibly-still-running and refuses to export it — which is
        why every recorder registers with the client and process shutdown closes
        whatever a killed pipeline left open.
        """
        if self._closed:
            return
        self._closed = True
        # The bot's sign-off is the last turn of the call, and nothing else will
        # come along to flush it.
        self._guard("close.flush", self.flush)
        with bind(self._ctx):
            self._handle().close(reason=reason or "NONE", error=error)
        client = require_client()
        if client is not None:
            client.unregister_journey(self)

    # -- signals, for the app to call --------------------------------------

    def signal(self, kind: str, **kw: Any) -> Optional[int]:
        """Attach caller feedback — a thumbs-up, a regeneration, an edit.

        Cannot be inferred from the pipeline: Pipecat knows what was said, not
        whether it was any good. This is what turns a transcript into
        preference data.
        """
        self.flush()
        with bind(self._ctx):
            return self._handle().signal(kind, **kw)  # type: ignore[arg-type]

    def reward(self, value: Any) -> Optional[int]:
        """Attach a scalar or structured reward to the journey."""
        self.flush()
        with bind(self._ctx):
            return self._handle().reward(value)


def observer(
    *,
    journey_id: str,
    **metadata: Any,
) -> Any:
    """A Pipecat observer that records into ``journey_id``.

    Pass it to ``PipelineTask(..., observers=[...])`` when the task is built in
    one expression; :func:`attach` is the same thing for a task that already
    exists.

    Returns an instance of a class defined here rather than at module scope,
    because it subclasses Pipecat's ``BaseObserver`` when Pipecat is installed
    — an import that cannot happen at class-definition time without making
    ``odyssey-core`` depend on Pipecat. With Pipecat absent it subclasses
    ``object`` instead and still works: ``add_observer`` appends to a list, and
    the pipeline calls ``on_push_frame`` on whatever is in it.
    """
    try:
        from pipecat.observers.base_observer import (  # type: ignore[import-not-found]
            BaseObserver,
        )

        base: Any = BaseObserver
    except Exception:  # noqa: BLE001 - Pipecat is an optional extra
        base = object

    class OdysseyObserver(base):  # type: ignore[misc, valid-type]
        """Records every frame the pipeline pushes into one journey."""

        def __init__(self) -> None:
            try:
                super().__init__()
            except TypeError:
                # `object.__init__` takes no arguments and some base classes
                # are dataclass-shaped. Neither is a reason to fail to record.
                pass
            self.recorder = PipecatRecorder(journey_id=journey_id, metadata=metadata)

        async def on_push_frame(self, *args: Any, **kwargs: Any) -> None:
            """Both observer signatures, old and new.

            Pipecat replaced ``on_push_frame(src, dst, frame, direction,
            timestamp)`` with ``on_push_frame(data)``. Accepting either keeps
            one integration working across the versions actually deployed
            rather than picking one and silently capturing nothing on the other.
            """
            self.recorder.on_frame(_frame_of(args, kwargs))

        async def on_process_frame(self, *args: Any, **kwargs: Any) -> None:
            """Not consumed: every frame that is processed was also pushed, and
            recording both would double the corpus."""

    return OdysseyObserver()


def attach(
    task: Any,
    *,
    journey_id: str,
    **metadata: Any,
) -> PipecatRecorder:
    """Record a ``PipelineTask`` into ``journey_id``. The one line to add.

    Call it once, right after the task is constructed and before it runs.
    ``journey_id`` is the caller's to choose because a journey boundary is
    domain knowledge; for a voice call the platform's own call id is usually
    right, and using it also makes recording idempotent across a worker restart.

    Agent identity is an ordinary tag: ``agent_id=...`` lands in the header's
    ``journey_metadata`` like any other keyword, under whatever meaning the
    deployment gives it.

    Returns the recorder so the app can add what pipeline frames cannot supply —
    :meth:`PipecatRecorder.signal` and :meth:`PipecatRecorder.reward`.

    Requires :func:`odyssey.init` to have run. Without it, recording is a no-op
    and one warning is emitted; the pipeline is unaffected either way.
    """
    obs = observer(journey_id=journey_id, **metadata)
    task.add_observer(obs)
    recorder: PipecatRecorder = obs.recorder
    client = require_client()
    if client is not None:
        # So process shutdown can end this journey if the pipeline never pushes
        # an `EndFrame`. A worker killed mid-call never reaches one, and the
        # journey then has no terminal event -- `fold()` cannot tell "still
        # running" from "lost the tail", and refuses it forever.
        client.register_journey(recorder)
    return recorder


def _frame_of(args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> Any:
    """The frame out of either observer signature."""
    if "frame" in kwargs:
        return kwargs["frame"]
    if "data" in kwargs:
        return getattr(kwargs["data"], "frame", None)
    if not args:
        return None
    if len(args) == 1:
        # The new signature: one `FramePushed`. A bare frame is accepted too,
        # since that is what a hand-written test or a shim is likely to pass.
        return getattr(args[0], "frame", args[0])
    # The deprecated positional signature: (src, dst, frame, direction, ts).
    return args[2] if len(args) > 2 else None


def _arguments(frame: Any) -> Dict[str, Any]:
    """A tool call's arguments as a dict. Never raises, never returns None.

    Pipecat passes through whatever the provider sent, which for a malformed
    call can be a string or nothing at all. An empty dict records the call as
    having happened with no readable arguments, which is true; raising would
    lose the call entirely.
    """
    raw = _jsonable(getattr(frame, "arguments", None))
    if isinstance(raw, dict):
        return raw
    return {"_raw": raw} if raw is not None else {}


def _seconds_to_ms(raw: Any) -> Optional[float]:
    """A Pipecat duration (seconds, float) as milliseconds. None if unusable.

    Negative is treated as unusable rather than clamped: a negative latency
    means the reading is wrong, and a zero would claim it was instant.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    if raw < 0:
        return None
    return round(float(raw) * 1000, 3)


def _is_llm(processor: Any) -> bool:
    """Whether a metrics reading came from the LLM slot of the pipeline.

    Matched on the processor's name because that is all the metrics data
    carries. Pipecat's own convention is ``<Vendor>LLMService``, and a
    deployment that renames its processor loses the TTFT attachment but keeps
    the latency event, which still names the processor.
    """
    return "llm" in str(processor or "").lower()


def _stage_from_type(item: Any) -> str:
    """``ProcessingMetricsData`` -> ``processing``. Fallback for a new type."""
    name = type(item).__name__
    for suffix in ("MetricsData", "Data"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name.lower() or "unknown"


def _throwaway_allocator() -> SeqAllocator:
    return SeqAllocator(lambda _jid: None)
