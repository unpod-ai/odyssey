"""LiveKit Agents capture — one ``attach()`` per session, provider-agnostic.

Why this exists rather than reusing the provider wrappers
---------------------------------------------------------

A LiveKit voice agent does not call an LLM SDK in a way a client wrapper can see.
``AgentSession`` is configured with whatever the deployment chose — an inference
gateway string (``llm="openai/gpt-5-mini"``), a plugin
(``livekit.plugins.openai``), or a realtime speech-to-speech model
(``openai.realtime.RealtimeModel``) that never issues a ``messages.create()`` at
all. Patching a provider SDK would capture some deployments and silently miss
others, which is the worst possible outcome for a corpus.

``AgentSession`` instead emits the conversation itself. Hooking those events
captures every turn regardless of what is behind the LLM, and it keeps working
when the deployment swaps models. That is the real single integration point for
LiveKit::

    from livekit.agents import JobContext

    async def entrypoint(ctx: JobContext):
        session = AgentSession(...)
        odyssey.integrations.livekit.attach(session, journey_id=ctx.room.name)
        ...                        # nothing else in the agent changes

Events consumed
---------------

- ``conversation_item_added`` — user and assistant turns, with STT confidence and
  the ``interrupted`` flag that matters so much for voice.
- ``function_tools_executed`` — tool calls paired with their outputs, correlated
  by ``call_id``. Tool calls do **not** arrive on ``conversation_item_added``, so
  without this hook every tool turn would be missing.
- ``close`` — ends the journey with a reason, which is what makes it foldable.

``AgentSession`` emits ``close`` only at the end of ``_aclose()``, after it has
drained in-flight speech and closed its recorder IO and every toolset. A worker
killed or a job shut down partway through those awaits never reaches the emit, so
the journey ends with no terminal event — and ``fold()`` then cannot tell "still
running" from "lost the tail" and refuses it forever. Every recorder therefore
registers itself with the client, and process shutdown terminates whatever is
still open as ``STALE``. A session that closed itself keeps its own reason;
:meth:`LiveKitRecorder.close` is idempotent, so shutdown never overwrites one.

One message per turn, never a stream
------------------------------------

A voice agent does not speak once per turn. TTS is driven utterance by utterance,
and ``conversation_item_added`` fires for each one, so a single agent reply
arrives as three, four, or six items::

    assistant  "I have found a slot at three PM for three players."
    assistant  "The price is five thousand four hundred rupees."
    assistant  "Would you like to book this slot?"

Recording those as three messages is recording the stream, not the conversation.
It is wrong for a corpus in three separate ways: the SFT target for that turn is
the whole reply, not its third fragment; consecutive same-role messages are
rejected outright by the OpenAI and Anthropic chat formats, so the journey will
not render into a prompt; and it inflated a 16-turn call to 33 near-identical
cumulative steps.

The same happens on the caller's side, where STT emits split finals ("2, 24."
then "3 PM, 3 players.") for one continuous thing the caller said.

So consecutive items of the same role are **coalesced into one message**, flushed
when the role changes, when a tool turn interleaves, when a signal needs a target,
or when the utterance was cut off — barge-in *is* the end of a turn, and gluing
the next reply onto a truncated fragment would corrupt both. Nothing is
discarded: the merged message keeps every
part's item id and STT confidence under ``parts``, reports a length-weighted
``transcript_confidence`` for the whole utterance, and is flagged interrupted if
*any* part was — a reply cut off in its third sentence is still a truncated reply.

Speech that preceded a tool call is folded onto the tool-call message itself,
as ``content`` alongside ``tool_calls`` — one generation, one message, and the
shape OpenAI and Anthropic already use.

Interim transcripts never enter this path at all: ``user_input_transcribed`` is
not consumed (see below), so only committed finals are ever coalesced.

The system prompt
-----------------

``AgentSession`` never emits the instructions as a conversation item, so nothing
in the event stream carries them. A journey without them is not a training
example: the step would hold the exchange but not what the agent was told to do,
and the same user turn under two different prompts would look identical.

They are read off ``session.current_agent.instructions`` instead, and re-read
before every recorded item. Polling rather than reading once at ``attach()`` is
deliberate — the agent is handed to ``session.start(agent=...)``, so at attach
time there may be no agent yet, and ``update_agent()`` / a tool-call handoff /
``Agent.update_instructions()`` all change the prompt mid-call. A changed prompt
emits a new ``system`` message, which ``build_cumulative_steps`` applies
copy-on-write: steps before the handoff keep the old prompt, steps after get the
new one.

Deployments that build instructions somewhere the session cannot see them can
pass ``instructions=`` to :func:`attach` to seed the value instead.

``user_input_transcribed`` is deliberately **not** consumed: its final transcript
is the same text ``conversation_item_added`` delivers, and its interim results
would flood the spool with partial turns.

Context handling
----------------

The journey is held **explicitly on the recorder**, not in the ambient
``ContextVar``. LiveKit fires these callbacks from the session's own task, which
does not carry whatever context the entrypoint was in, and one worker process
runs many concurrent calls. Relying on ambient context here would interleave two
callers' conversations into one journey — the exact corruption
``writer_id`` detection exists to catch.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple
from uuid import uuid4

from odyssey.builders.messages import parse_tool_arguments
from odyssey.capture import JourneyHandle, _jsonable
from odyssey.client import require_client
from odyssey.context import JourneyContext, bind
from odyssey.fold import INTERRUPTED_FLAG
from odyssey.primitives import (
    Message,
    Role,
    TerminationReason,
    ToolCall,
    ToolResponse,
)

# LiveKit's CloseReason → the schema's TerminationReason.
#
# `job_shutdown` is TRUNCATION rather than ENV_DONE on purpose: the platform cut
# the call off, so the conversation is incomplete and training on it would teach
# the model to stop mid-thought. A caller hanging up is a real ending; a worker
# being killed is not.
_CLOSE_REASON: Dict[str, TerminationReason] = {
    "error": "ERROR",
    "job_shutdown": "TRUNCATION",
    "participant_disconnected": "ENV_DONE",
    "user_initiated": "ENV_DONE",
    "task_completed": "ENV_DONE",
}

# LiveKit's ChatRole includes `developer`, which the journey schema does not.
# It is a system-prompt variant, so it maps there rather than being dropped.
_ROLE: Dict[str, Role] = {
    "developer": "system",
    "system": "system",
    "user": "user",
    "assistant": "assistant",
}

_EVENTS = ("conversation_item_added", "function_tools_executed", "close")


class _Turn:
    """One speaker's turn, assembled from the utterances it streamed in as.

    Not a dataclass because ``absorb`` is the whole type: every field is an
    accumulator with its own merge rule, and the rules are the interesting part.
    """

    __slots__ = (
        "role",
        "texts",
        "parts",
        "weights",
        "interrupted",
        "non_text",
        "extra",
    )

    def __init__(self, role: Role) -> None:
        self.role: Role = role
        self.texts: List[str] = []
        # One entry per absorbed utterance, empty dict included, so `parts` and
        # `weights` stay index-aligned even for an item that carried no text.
        self.parts: List[Dict[str, Any]] = []
        self.weights: List[int] = []
        self.interrupted = False
        self.non_text: List[str] = []
        self.extra: Dict[str, Any] = {}

    def absorb(
        self,
        *,
        text: Optional[str],
        item_id: Optional[str],
        interrupted: bool,
        confidence: Optional[float],
        non_text: List[str],
        extra: Optional[Dict[str, Any]],
    ) -> None:
        if text:
            self.texts.append(text)
        part: Dict[str, Any] = {}
        if item_id:
            part["id"] = item_id
        if confidence is not None:
            part["transcript_confidence"] = confidence
        if interrupted:
            part[INTERRUPTED_FLAG] = True
        self.parts.append(part)
        self.weights.append(len(text) if text else 1)
        # Any part cut off means the turn was cut off. A reply interrupted in its
        # third sentence is a truncated reply, and `INTERRUPTED_FLAG` is what
        # stops the fold treating it as a training target.
        self.interrupted = self.interrupted or interrupted
        for name in non_text:
            if name not in self.non_text:
                self.non_text.append(name)
        if extra:
            self.extra.update(extra)

    @property
    def content(self) -> Optional[str]:
        # A space, not a newline: these are sentences of one spoken utterance,
        # not lines of a document.
        return " ".join(self.texts) if self.texts else None

    def metadata(self) -> Optional[Dict[str, Any]]:
        """What this turn carries beyond its text, or None when that is nothing.

        No ``source`` key: the shard header's ``data_source`` says ``livekit``
        once, and repeating it on every message was a constant occupying a third
        of the shorter lines.

        ``None`` rather than ``{}`` for a bare turn — an empty dict survives
        ``_strip_none`` and would encode as ``"metadata":{}``, which is a key
        that costs bytes to say nothing.
        """
        turn: Dict[str, Any] = {}
        ids = [p["id"] for p in self.parts if "id" in p]
        if ids:
            # Always a list, even for a single utterance. A field that is a
            # string on some lines and a list on others forces every consumer to
            # branch on type, and a typed reader rejects whichever shape it did
            # not declare — so the rare coalesced line breaks a parser the common
            # line trained.
            turn["provider_item_ids"] = ids
        if len(self.parts) > 1:
            turn["utterances"] = len(self.parts)
            # Only when a part says something `provider_item_ids` does not already
            # carry. A list of bare ids twice over is noise in every export.
            detailed = [p for p in self.parts if p.keys() - {"id"}]
            if detailed:
                turn["parts"] = [p for p in self.parts if p]
        if self.interrupted:
            turn[INTERRUPTED_FLAG] = True
        confidence = self._confidence()
        if confidence is not None:
            turn["transcript_confidence"] = confidence
        if self.extra:
            turn["extra"] = _jsonable(self.extra)
        if self.non_text:
            turn["non_text_content"] = sorted(self.non_text)
        return turn or None

    def _confidence(self) -> Optional[float]:
        """One confidence for the whole utterance, weighted by how much was said.

        A plain mean lets a two-word fragment the STT was unsure about drag down
        a long confident sentence. Weighting by text length is what a single item
        covering the whole turn would have reported.
        """
        scored = [
            (p["transcript_confidence"], w)
            for p, w in zip(self.parts, self.weights)
            if "transcript_confidence" in p
        ]
        if not scored:
            return None
        weight = sum(w for _, w in scored)
        return sum(c * w for c, w in scored) / weight

    def is_empty(self) -> bool:
        return not self.texts and not self.non_text


class _ToolTurn:
    """One tool call and its outcome, provider-shape already stripped off.

    The seam between "how LiveKit reported it" and "what gets written", so the
    event path and the public :meth:`LiveKitRecorder.tool` API converge before
    anything is emitted rather than each building its own messages.
    """

    __slots__ = ("id", "name", "arguments", "response", "responded", "error")

    def __init__(
        self,
        *,
        id: str,
        name: str,
        arguments: Dict[str, Any],
        response: Any = None,
        responded: bool = True,
        error: Optional[str] = None,
    ) -> None:
        self.id = id
        self.name = name
        self.arguments = arguments
        self.response = response
        self.responded = responded
        self.error = error


class LiveKitRecorder:
    """Records one ``AgentSession`` into one journey.

    Created by :func:`attach`. Holds the journey explicitly so concurrent calls
    in the same worker process cannot bleed into each other.
    """

    def __init__(
        self,
        session: Any,
        *,
        journey_id: str,
        instructions: Optional[str | Callable[[], Optional[str]]] = None,
        record_instructions: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._session = session
        self.journey_id = journey_id
        self._closed = False
        self._handlers: List[Tuple[str, Callable[..., None]]] = []
        # The last system prompt written to the spool. `None` means none yet, so
        # the first recorded item emits one; after that only a *change* does.
        self._instructions: Optional[str] = None
        self._seed_instructions = instructions
        self._record_instructions = record_instructions
        # The turn being assembled from same-role utterances. At most one turn is
        # ever held, and every exit path flushes it, so a crash can lose the turn
        # in flight and nothing before it.
        self._pending: Optional[_Turn] = None

        client = require_client()
        self._enabled = client is not None and client.config.enabled
        self._ctx = JourneyContext(
            journey_id=journey_id,
            allocator=(
                client.allocator if client is not None else _throwaway_allocator()
            ),
            # Sanitized at the door: these tags are snapshotted into the shard
            # header, which is json-dumped directly. LiveKit deployments pass
            # domain enums here (`modality=Modality.TEXT_AUDIO`), and an
            # unserializable one would raise while opening the shard and drop
            # every event of the call.
            metadata=_jsonable(dict(metadata or {})),
            # Journey identity, header-bound. This is the value `fold()` used to
            # make every caller supply by hand, and the reason `source:
            # "livekit"` no longer needs stamping onto all N events.
            data_source="livekit",
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
        """Run a capture step inside a LiveKit callback. Never raises.

        An exception escaping here propagates into LiveKit's event loop, where it
        can take the call down. Losing a recorded turn is acceptable; dropping a
        live phone call to record it is not.
        """
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - see docstring
            client = require_client()
            if client is not None:
                client.note_error(f"livekit.{label}", exc)

    def _register(self) -> None:
        for name in _EVENTS:
            handler = getattr(self, f"_on_{name}")
            self._session.on(name, handler)
            self._handlers.append((name, handler))
        client = require_client()
        if client is not None:
            # So process shutdown can end this journey if the session never
            # fires `close`. `AgentSession._aclose()` emits it only after a
            # series of awaits — draining speech, closing the recorder IO and
            # every toolset — and a worker killed or a job shut down partway
            # through never reaches the emit. The journey then has no terminal
            # event, `fold()` cannot tell "still running" from "lost the tail",
            # and it is refused forever.
            client.register_journey(self)

    # -- turn coalescing --------------------------------------------------

    def flush(self) -> None:
        """Write the turn being assembled, if any. Idempotent.

        Every path that could observe or order events around it calls this
        first: a role change, a tool turn, a system swap, a signal that needs
        ``last_message_seq`` to mean the turn above it, and close.

        Public because a caller that reads the journey mid-call — a live review
        UI, a mid-call CLI ``show`` — would otherwise be one turn behind. Callers
        that only record never need it: the flush points above cover a normal
        session.
        """
        turn = self._pending
        self._pending = None
        if turn is None or turn.is_empty():
            return
        with bind(self._ctx):
            handle = self._handle()
            handle.message(
                Message(role=turn.role, content=turn.content, metadata=turn.metadata()),
            )
            # Voice events (item 0'.4): the same signals `metadata()` already
            # folds into the message's `metadata` dict, recorded a second time
            # as their own `voice` event so a consumer that only wants voice
            # telemetry (not full transcripts) doesn't have to parse message
            # metadata to find it. No new STT/TTS instrumentation beyond what
            # this integration already computes above.
            confidence = turn._confidence()
            if confidence is not None:
                handle.voice("stt_transcript", text=turn.content, confidence=confidence)
            if turn.interrupted:
                handle.voice("barge_in", text=turn.content)

    def detach(self) -> None:
        """Stop recording this session. Does not close the journey."""
        self._guard("detach", self.flush)
        client = require_client()
        if client is not None:
            # Nothing here will record again, so shutdown has no terminal to
            # contribute. A journey detached without closing stays open on
            # purpose — that is what `detach` means, and `health()` still counts
            # it until the recorder is collected.
            client.unregister_journey(self)
        for name, handler in self._handlers:
            try:
                self._session.off(name, handler)
            except Exception:  # noqa: BLE001 - detaching must always succeed
                pass
        self._handlers.clear()

    # -- system prompt ----------------------------------------------------

    def _read_instructions(self) -> Optional[str]:
        """The active agent's instructions, or ``None`` if unreadable.

        Every access is defensive on purpose. ``current_agent`` raises when the
        session has not started, a realtime model may not expose ``instructions``
        at all, and older livekit-agents kept the agent on ``_agent``. None of
        those is a reason to lose the turn being recorded, so this returns
        ``None`` and the conversation is captured without a prompt.
        """
        for attr in ("current_agent", "_agent"):
            try:
                agent = getattr(self._session, attr, None)
            except Exception:  # noqa: BLE001 - `current_agent` raises pre-start
                continue
            if agent is None:
                continue
            try:
                text = getattr(agent, "instructions", None)
            except Exception:  # noqa: BLE001 - property may raise
                continue
            if isinstance(text, str) and text.strip():
                return text
        return self._read_seed()

    def _read_seed(self) -> Optional[str]:
        """The caller-supplied prompt: a fixed string, or a live reader.

        A callable is what makes flow/playbook deployments recordable at all.
        There, ``llm_node`` returns ``None`` — LiveKit's LLM never runs — and the
        agent is constructed as ``Agent(instructions="")`` because the prompt
        lives in the dialog machine and is rebuilt per node. Polling the session
        finds nothing, forever, and the journey has no system message: not a
        training example, since the same user turn under two different prompts
        looks identical.

        Called on every recorded item, like the session read it replaces, so a
        per-node prompt change is captured as a new ``system`` message and
        ``build_cumulative_steps`` applies it copy-on-write.
        """
        seed = self._seed_instructions
        if seed is None or isinstance(seed, str):
            return seed or None
        try:
            text = seed()
        except Exception as exc:  # noqa: BLE001 - a reader must not kill capture
            client = require_client()
            if client is not None:
                client.note_error("livekit.instructions_reader", exc)
            return None
        return text if isinstance(text, str) and text.strip() else None

    def _sync_instructions(self) -> None:
        """Emit a ``system`` message when the prompt appears or changes.

        Does nothing when ``record_instructions=False``: a deployment whose
        prompt is a novel of business rules can be worth many times the
        conversation it produced, and repeating it on every export is not what
        every consumer wants. The conversation is still complete without it.

        Called before every recorded item rather than once at attach: the agent
        arrives with ``session.start()``, and handoffs replace it mid-call. The
        emitted message lands *ahead* of the item that triggered it, so the first
        step carries the prompt it was produced under — and a handoff's new
        prompt supersedes the old one via ``build_cumulative_steps``'
        copy-on-write, leaving pre-handoff steps untouched.
        """
        if not self._record_instructions:
            return
        text = self._read_instructions()
        # Falsy, not just None. A deployment that passes `instructions=""` would
        # otherwise emit an empty `system` message, and `build_cumulative_steps`
        # keeps it as the prefix — so every step would carry a system message
        # saying nothing, which reads as "this agent was given no instructions".
        # That is worse than having no system message at all, because it looks
        # like a real prompt.
        if not text or text == self._instructions:
            return
        # A handoff can land mid-turn. The prompt must not jump ahead of the turn
        # that ran under the *previous* one.
        self.flush()
        first = self._instructions is None
        self._instructions = text
        turn: Dict[str, Any] = {
            # A handoff is the interesting case downstream: it means the steps
            # before this point ran under a different prompt. Naming it beats
            # making a reader diff strings.
            "instructions_origin": "initial" if first else "handoff",
        }
        name = _agent_name(self._session)
        if name:
            turn["agent"] = name
        with bind(self._ctx):
            self._handle().message(
                Message(role="system", content=text, metadata=turn),
            )

    # -- events -----------------------------------------------------------

    def _on_conversation_item_added(self, event: Any) -> None:
        self._guard("conversation_item_added", lambda: self._record_item(event))

    def _record_item(self, event: Any) -> None:
        # Before the item, so the prompt precedes the turn it governed. Runs even
        # for items dropped below — AgentHandoff arrives here, and that is
        # exactly when the prompt changes.
        self._sync_instructions()

        item = getattr(event, "item", None)
        role_raw = getattr(item, "role", None)
        if item is None or role_raw is None:
            # AgentHandoff and other non-message items arrive here too.
            return
        role = _ROLE.get(str(role_raw))
        if role is None:
            return

        text = getattr(item, "text_content", None)
        # Non-text content (audio, images, injected instructions) is named but
        # not inlined — audio bytes have no place in a text corpus.
        non_text = sorted(
            {
                type(c).__name__
                for c in (getattr(item, "content", None) or [])
                if not isinstance(c, str)
            }
        )
        if text is None and not non_text:
            return  # nothing was actually said

        # A different speaker means the previous turn is over.
        if self._pending is not None and self._pending.role != role:
            self.flush()
        if self._pending is None:
            self._pending = _Turn(role)

        # Facts about *this turn* go on the Message, not on the event: that is
        # what `fold.derive_trainable_status` reads, and it is where a corpus
        # stage looks. Journey-level tags live in the shard header; the event
        # carries only the writer id and anything retagged mid-call.
        self._pending.absorb(
            text=text,
            item_id=getattr(item, "id", None),
            # Barge-in. This is load-bearing rather than informational: an
            # assistant turn the caller cut off is half an utterance, and
            # `INTERRUPTED_FLAG` makes the fold refuse to treat it as a training
            # target. Without it the corpus teaches the model to stop mid-sentence.
            interrupted=bool(getattr(item, "interrupted", False)),
            confidence=getattr(item, "transcript_confidence", None),
            non_text=non_text,
            extra=getattr(item, "extra", None) or None,
        )

        # Barge-in *is* the end of a turn: the speaker stopped. Merging what
        # comes next into it would both discard a complete reply (the merged
        # message inherits the interrupted flag) and splice a truncated fragment
        # onto it -- "Let me just check the— Booked for Tuesday 3pm."
        if self._pending.interrupted:
            self.flush()

    def _on_function_tools_executed(self, event: Any) -> None:
        self._guard("function_tools_executed", lambda: self._record_tools(event))

    def _record_tools(self, event: Any) -> None:
        """One assistant message carrying the speech and every call, then one
        message per result.

        The speech is folded into the tool-call message rather than written
        before it. "Three players, got it, let me check availability" and the
        ``check_availability`` call were one generation, and ``content`` +
        ``tool_calls`` on one assistant message is exactly how OpenAI and
        Anthropic represent that. Emitting them as two consecutive assistant
        messages would both misreport the turn and produce a message list those
        formats reject.

        Results stay separate, one per call, which is what those formats require
        and what keeps parallel tool calls legible.
        """
        pairs = _tool_pairs(event)
        if pairs is None:
            # An event shape this build does not understand. Distinct from an
            # empty batch, and reported rather than dropped: a tool turn that
            # never reaches the spool makes a failed booking read as a clean
            # conversation, with `num_tool_calls == 0` to confirm it.
            raise TypeError(
                "function_tools_executed carried neither zipped() nor "
                f"function_calls/function_call_outputs: {type(event).__name__}"
            )
        if not pairs:
            return
        self._sync_instructions()

        # Speech from this same turn joins the call. Anything else -- a user turn
        # still open because the agent called a tool without speaking -- is a
        # different turn and has to land first.
        speech: Optional[_Turn] = None
        if self._pending is not None and self._pending.role == "assistant":
            speech, self._pending = self._pending, None
        else:
            self.flush()

        turns: List[_ToolTurn] = []
        for call, output in pairs:
            turns.append(
                _ToolTurn(
                    id=call.call_id,
                    name=call.name,
                    arguments=_arguments(getattr(call, "arguments", None)),
                    response=getattr(output, "output", None),
                    # `None` output is a real failure mode — the tool never
                    # returned — recorded rather than skipped so
                    # `num_tool_response_none` can count it.
                    responded=output is not None,
                    error=(
                        "tool_error"
                        if output is not None and getattr(output, "is_error", False)
                        else None
                    ),
                )
            )
        self._emit_tool_turn(turns, speech)

    def _emit_tool_turn(
        self, turns: List["_ToolTurn"], speech: Optional[_Turn]
    ) -> Optional[int]:
        """Write one tool turn: the calls on one message, each result on its own.

        Shared by the LiveKit event path and the public :meth:`tool` API so a
        flow-mode deployment recording its own tools produces bytes identical to
        a prompt-mode one. Two producers writing two shapes for the same thing is
        how a corpus stops being one corpus.
        """
        with bind(self._ctx):
            handle = self._handle()
            seq = handle.message(
                Message(
                    role="assistant",
                    content=speech.content if speech is not None else None,
                    tool_calls=[
                        ToolCall(id=t.id, name=t.name, arguments=t.arguments)
                        for t in turns
                    ],
                    metadata=speech.metadata() if speech is not None else None,
                ),
            )
            for t in turns:
                handle.message(
                    Message(
                        role="tool",
                        tool_response=ToolResponse(
                            id=t.id,
                            name=t.name,
                            arguments=t.arguments,
                            response=t.response if t.responded else None,
                            error=t.error,
                        ),
                    ),
                )
        return seq

    def tool(
        self,
        name: str,
        arguments: Optional[Dict[str, Any]] = None,
        *,
        result: Any = None,
        error: Optional[str] = None,
        call_id: Optional[str] = None,
        responded: bool = True,
    ) -> Optional[int]:
        """Record a tool call the session never saw. Returns the call's ``seq``.

        ``function_tools_executed`` only fires for tools LiveKit itself ran. A
        deployment whose ``llm_node`` returns ``None`` — flow and playbook modes,
        where the dialog machine drives the turn and executes its own tools —
        bypasses that machinery entirely, so LiveKit has nothing to emit and the
        journey shows ``num_tool_calls == 0`` no matter how many tools ran. A
        failed booking then reads as a clean conversation.

        Call it right after the tool returns::

            result = await machine.execute_tool(tool_id, args)
            recorder.tool(tool_id, args, result=result.data)

        Pending agent speech is folded onto the call message, exactly as the
        event path does: "let me check availability" and the lookup were one
        turn, and ``content`` + ``tool_calls`` on one message is how OpenAI and
        Anthropic represent that.

        ``responded=False`` records a tool that never returned — the case
        ``num_tool_response_none`` counts, and distinct from one that returned
        ``None``.
        """

        def run() -> None:
            self._sync_instructions()
            speech: Optional[_Turn] = None
            if self._pending is not None and self._pending.role == "assistant":
                speech, self._pending = self._pending, None
            else:
                self.flush()
            self._emit_tool_turn(
                [
                    _ToolTurn(
                        id=call_id or f"call_{uuid4().hex[:12]}",
                        name=name,
                        arguments=_jsonable(dict(arguments or {})),
                        response=_jsonable(result),
                        responded=responded,
                        error=error,
                    )
                ],
                speech,
            )

        self._guard("tool", run)
        return self._ctx.last_message_seq

    def _on_close(self, event: Any) -> None:
        self._guard("close", lambda: self.close(event=event))

    def close(
        self,
        *,
        event: Any = None,
        reason: Optional[TerminationReason] = None,
        error: Optional[str] = None,
    ) -> None:
        """End the journey. Idempotent, and safe to call by hand.

        Until this lands the journey has no terminal event, so ``fold()`` reports
        it as possibly-still-running and refuses to export it.
        """
        if self._closed:
            return
        self._closed = True
        # The agent's sign-off is the last turn of the call, and nothing else
        # will come along to flush it.
        self._guard("close.flush", self.flush)

        resolved = reason
        detail = error
        if resolved is None and event is not None:
            raw = getattr(event, "reason", None)
            key = getattr(raw, "value", raw)
            resolved = _CLOSE_REASON.get(str(key), "NONE")
            err = getattr(event, "error", None)
            if err is not None and detail is None:
                detail = f"{type(err).__name__}: {err}"
        with bind(self._ctx):
            self._handle().close(reason=resolved or "NONE", error=detail)
        self.detach()

    # -- signals, for the app to call ------------------------------------

    def signal(self, kind: str, **kw: Any) -> Optional[int]:
        """Attach caller feedback — a thumbs-up, a regeneration, an edit.

        Cannot be inferred from session events: LiveKit knows what was said, not
        whether it was any good. This is the hook a review UI or a post-call
        rating flow calls, and it is what turns a transcript into preference data.
        """
        # `target_seq` defaults to the most recent message. A turn still being
        # assembled has no seq yet, so without this the rating would land on the
        # turn *before* the one the caller just heard.
        self.flush()
        with bind(self._ctx):
            return self._handle().signal(kind, **kw)  # type: ignore[arg-type]

    def reward(self, value: Any) -> Optional[int]:
        self.flush()
        with bind(self._ctx):
            return self._handle().reward(value)


def attach(
    session: Any,
    *,
    journey_id: str,
    instructions: Optional[str | Callable[[], Optional[str]]] = None,
    record_instructions: bool = True,
    **metadata: Any,
) -> LiveKitRecorder:
    """Record an ``AgentSession`` into ``journey_id``. The one line to add.

    Call it once, right after the session is constructed and before it starts.
    ``journey_id`` is the caller's to choose because a journey boundary is domain
    knowledge; for a voice call ``ctx.room.name`` is usually right, and using the
    platform's own id also makes recording idempotent across a worker restart.

    Returns the recorder so the app can add what session events cannot supply —
    :meth:`LiveKitRecorder.signal`, :meth:`LiveKitRecorder.reward`, and
    :meth:`LiveKitRecorder.tool` for tools LiveKit did not run itself.

    ``instructions`` is a fallback for the system prompt, used only when it
    cannot be read off the session's own agent. Leave it unset for the normal
    case — the recorder reads ``session.current_agent.instructions`` and follows
    handoffs, which a value frozen at attach time cannot do.

    Pass a **callable** when the prompt does not live on the agent at all. Flow
    and playbook deployments construct ``Agent(instructions="")`` because
    ``llm_node`` returns ``None`` and the dialog machine owns the prompt,
    rebuilding it per node. A callable is polled on every recorded item, so each
    node's prompt is captured as it changes::

        attach(
            session,
            journey_id=...,
            instructions=lambda: machine.context.system_prompt,
        )

    Without it such a journey has no ``system`` message at all, which is not a
    training example: the same user turn under two different prompts is
    indistinguishable.

    ``record_instructions=False`` keeps the system prompt out of the journey
    entirely. Some prompts are thousands of tokens of business rules that dwarf
    the call itself, and a deployment may not want that text copied into every
    exported artifact. The conversation, the tool calls and the greeting are
    recorded exactly as before; only the ``system`` message is skipped.

    Requires :func:`odyssey.init` to have run. Without it, recording is a no-op
    and one warning is emitted; the session is unaffected either way.
    """
    recorder = LiveKitRecorder(
        session,
        journey_id=journey_id,
        instructions=instructions,
        record_instructions=record_instructions,
        metadata=metadata,
    )
    recorder._register()
    return recorder


def _tool_pairs(event: Any) -> Optional[List[Tuple[Any, Any]]]:
    """Calls paired with their outputs, or ``None`` if the event is unreadable.

    ``zipped()`` is the documented accessor and is preferred. The parallel
    ``function_calls`` / ``function_call_outputs`` lists are the fallback — they
    are what ``zipped()`` zips, and older livekit-agents exposed only them.

    The distinction that matters is the return type. Collapsing "no tools ran"
    and "this event is a shape I do not recognise" into one empty list is how a
    whole tool turn disappears from a corpus without anything registering that it
    did; ``None`` forces the caller to report the second case.
    """
    zipped = getattr(event, "zipped", None)
    if callable(zipped):
        # pyrefly: ignore[bad-argument-type]  -- `zipped` is `Any` narrowed
        # via `callable()`; pyrefly can't see through the getattr+callable
        # narrowing to know the call result is iterable, but it is (a real
        # livekit-agents `zipped()` always returns an iterable of pairs).
        return list(zipped())
    calls = getattr(event, "function_calls", None)
    if calls is None:
        return None
    outputs = list(getattr(event, "function_call_outputs", None) or [])
    # `strict=False` semantics, matching livekit's own `zipped()`: a batch whose
    # lists disagree in length is still worth recording as far as it goes.
    outputs += [None] * (len(calls) - len(outputs))
    return list(zip(calls, outputs))


def _arguments(raw: Any) -> Dict[str, Any]:
    """LiveKit sends tool arguments as a JSON string. Parse, but never lose.

    ``parse_tool_arguments`` raises on anything that is not a JSON object, which
    is right for a batch import a human is watching and wrong here: a model that
    emitted malformed arguments is exactly the behaviour worth having in a
    corpus, and raising would discard the whole tool turn — the call, the result,
    and the assistant message carrying them.

    So the raw text is kept under a reserved key instead, where a cleaning stage
    can find it.
    """
    try:
        return parse_tool_arguments(raw)
    except (ValueError, TypeError) as exc:
        return {
            "_odyssey_unparsed_arguments": raw if isinstance(raw, str) else repr(raw),
            "_odyssey_parse_error": str(exc),
        }


def _agent_name(session: Any) -> Optional[str]:
    """The active agent's class name, for labelling a handoff. Never raises."""
    try:
        agent = getattr(session, "current_agent", None) or getattr(
            session, "_agent", None
        )
    except Exception:  # noqa: BLE001 - `current_agent` raises pre-start
        return None
    return type(agent).__name__ if agent is not None else None


def _throwaway_allocator() -> Any:
    from odyssey.context import SeqAllocator

    return SeqAllocator(lambda _jid: None)
