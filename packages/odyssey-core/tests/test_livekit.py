"""LiveKit session capture.

Fakes stand in for ``AgentSession`` and its events. That is not a compromise: the
integration imports nothing from ``livekit`` and reads every field through
``getattr``, so a fake with the right shape exercises the same code the real
session does — and the module stays usable across livekit-agents versions
without an optional dependency.

The shapes below are copied from livekit-agents 1.5.8:

    ConversationItemAddedEvent.item -> ChatMessage(id, role, content, interrupted,
                                                  transcript_confidence, extra)
    FunctionToolsExecutedEvent.zipped() -> [(FunctionCall, FunctionCallOutput|None)]
    FunctionCall(call_id, name, arguments: str)      # arguments is a JSON *string*
    FunctionCallOutput(call_id, name, output, is_error)
    CloseEvent(reason: CloseReason, error)
"""

from __future__ import annotations

from typing import Any

import pytest

import odyssey
from odyssey.integrations.livekit import attach as _attach

# --------------------------------------------------------------------------
# Fakes shaped like livekit-agents 1.5.x
# --------------------------------------------------------------------------


class FakeSession:
    """An EventEmitter with the on/off surface AgentSession exposes."""

    # Class-level type hint only, no assignment: several tests set this
    # dynamically (`session.current_agent = FakeAgent(...)`), and
    # `UnstartedSession` below overrides it as a `@property` -- an actual
    # `self.current_agent = ...` assignment in `__init__` would collide with
    # that property (no setter) on the subclass.
    current_agent: Any

    def __init__(self) -> None:
        self.handlers: dict[str, list] = {}

    def on(self, name, handler):
        self.handlers.setdefault(name, []).append(handler)

    def off(self, name, handler):
        self.handlers.get(name, []).remove(handler)

    def emit(self, name, event):
        for h in list(self.handlers.get(name, [])):
            h(event)


class ChatMessage:
    def __init__(
        self,
        role,
        content,
        *,
        id="item_1",
        interrupted=False,
        transcript_confidence=None,
        extra=None,
    ):
        self.role = role
        self.content = content
        self.id = id
        self.interrupted = interrupted
        self.transcript_confidence = transcript_confidence
        self.extra = extra or {}

    @property
    def text_content(self):
        parts = [c for c in self.content if isinstance(c, str)]
        return "\n".join(parts) if parts else None


class ItemAdded:
    def __init__(self, item):
        self.item = item


class FunctionCall:
    def __init__(self, call_id, name, arguments):
        self.call_id = call_id
        self.name = name
        self.arguments = arguments  # JSON string, as LiveKit sends it


class FunctionCallOutput:
    def __init__(self, call_id, name, output, is_error=False):
        self.call_id = call_id
        self.name = name
        self.output = output
        self.is_error = is_error


class ToolsExecuted:
    def __init__(self, pairs):
        self._pairs = pairs

    def zipped(self):
        return list(self._pairs)


class CloseEv:
    def __init__(self, reason, error=None):
        self.reason = reason
        self.error = error


class AudioContent:
    """A non-text ChatContent, so it can be named without being inlined."""


class FakeAgent:
    """``Agent``: the only place the system prompt lives.

    ``AgentSession`` never emits the instructions as a conversation item, which
    is why the recorder has to read them off here.
    """

    def __init__(self, instructions):
        self.instructions = instructions


class UnstartedSession(FakeSession):
    """``AgentSession.current_agent`` raises until ``start()`` is called."""

    @property
    # pyrefly: ignore[bad-override]  — a plain `Any` attribute on the base
    # overridden by a property here; a real variance mismatch pyrefly is
    # right to flag, but exactly what this fake needs to model "not started
    # yet raises" without also making every other subclass carry a property.
    def current_agent(self):
        raise RuntimeError("session not started")


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------

JID = "room_call_42"

# A turn is not written until the speaker is done with it, so a test that reads
# the spool has to complete the turn in flight first -- same as a live review UI
# calling `recorder.flush()`. Wrapping `attach` keeps every test honest about
# that without repeating the call in each one; `test_a_turn_is_not_written_until
# _the_speaker_is_done` asserts the buffering directly.
_RECORDERS: list = []


def attach(session, **kw):
    rec = _attach(session, **kw)
    _RECORDERS.append(rec)
    return rec


@pytest.fixture(autouse=True)
def clean_singleton():
    _RECORDERS.clear()
    odyssey.shutdown()
    yield
    _RECORDERS.clear()
    odyssey.shutdown()


def start(tmp_path, **kw):
    return odyssey.init(
        spool_dir=tmp_path / "spool",
        out_dir=tmp_path / "out",
        drain_interval=None,
        **kw,
    )


def events(jid=JID):
    for rec in _RECORDERS:
        rec.flush()
    client = odyssey.get_client()
    assert client is not None
    return client.spool.read(jid)


def messages(jid=JID):
    return [e.message for e in events(jid) if e.kind == "message" and e.message]


def header(jid=JID):
    for rec in _RECORDERS:
        rec.flush()
    client = odyssey.get_client()
    assert client is not None
    return odyssey.read_events(client.spool.shards(jid)[0]).header


def say(session, role, text, **kw):
    session.emit("conversation_item_added", ItemAdded(ChatMessage(role, [text], **kw)))


# --------------------------------------------------------------------------
# The integration imports nothing from livekit
# --------------------------------------------------------------------------


def test_the_module_does_not_import_livekit():
    """Duck typing is the whole reason no optional dependency is needed."""
    import ast
    from pathlib import Path

    import odyssey.integrations.livekit as mod

    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert not [n for n in imported if n.split(".")[0] == "livekit"]


# --------------------------------------------------------------------------
# Turns
# --------------------------------------------------------------------------


def test_attach_records_user_and_assistant_turns(tmp_path):
    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)

    say(session, "user", "I need an appointment.")
    say(session, "assistant", "What day suits you?")

    assert [(m.role, m.content) for m in messages()] == [
        ("user", "I need an appointment."),
        ("assistant", "What day suits you?"),
    ]


def test_seq_is_allocated_across_session_callbacks(tmp_path):
    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)
    # Alternating, because same-role items in a row are one turn and take one
    # seq between them -- see the coalescing tests below.
    for i in range(5):
        say(session, "user" if i % 2 == 0 else "assistant", f"turn {i}")
    assert [e.seq for e in events()] == [0, 1, 2, 3, 4]


def test_developer_role_maps_to_system(tmp_path):
    """LiveKit has a `developer` role the journey schema does not."""
    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)
    say(session, "developer", "You are a booking assistant.")
    assert messages()[0].role == "system"


def test_an_unknown_role_is_skipped_not_recorded_wrongly(tmp_path):
    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)
    say(session, "moderator", "???")
    assert messages() == []


def test_a_handoff_item_without_a_role_is_ignored(tmp_path):
    """AgentHandoff arrives on the same event and is not a message."""
    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)
    session.emit("conversation_item_added", ItemAdded(object()))
    assert messages() == []


def test_caller_metadata_is_stated_once_in_the_header(tmp_path):
    """Deployment tags are constant for a call, so they belong on line 1.

    A 29-event call used to repeat `tenant`, `sip_trunk`, `agent_id` and the rest
    on all 29 lines — the majority of every line, saying nothing a reader could
    not have learned from the first one.
    """
    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID, tenant="acme", sip_trunk="tw-1")
    say(session, "user", "hi")

    assert header().journey_metadata == {"tenant": "acme", "sip_trunk": "tw-1"}
    meta = events()[0].metadata or {}
    assert "tenant" not in meta and "sip_trunk" not in meta


def test_the_header_names_livekit_as_the_data_source(tmp_path):
    """What `fold()` used to make every caller supply by hand.

    Without it two callers could fold one file into two differently-labelled
    journeys and neither was wrong. It is also why `source: "livekit"` no longer
    needs stamping onto every event and every message.
    """
    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)
    say(session, "user", "hi")

    h = header()
    assert h.data_source == "livekit"
    assert h.journey_id == JID
    assert h.started_at  # a journey knows when it began
    assert all("source" not in (e.metadata or {}) for e in events())
    assert all("source" not in (m.metadata or {}) for m in messages())


def test_an_unserializable_tag_cannot_kill_the_journey(tmp_path):
    """Header tags are json-dumped directly, so they are sanitized at the door.

    A LiveKit deployment passing a domain enum (`modality=Modality.TEXT_AUDIO`
    is a real one) used to raise inside `_open_shard`, leave a zero-byte shard
    behind, and drop every event of the call — the loudest possible corpus bug
    reported as silence.
    """
    import enum

    class Modality(enum.Enum):
        TEXT_AUDIO = "text_audio"

    client = start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID, modality=Modality.TEXT_AUDIO)
    say(session, "user", "hi")

    assert header().journey_metadata == {"modality": "text_audio"}
    assert len(events()) == 1
    assert client.stats.events_dropped == 0


def test_a_bare_turn_carries_no_empty_metadata_dict():
    """`{}` survives `_strip_none`, so it would encode as a key saying nothing.

    Reachable whenever the provider gives an item no id and the STT no
    confidence — which is every turn from a text-only or synthetic session.
    """
    from odyssey.integrations.livekit import _Turn

    turn = _Turn("user")
    turn.absorb(
        text="hi",
        item_id=None,
        interrupted=False,
        confidence=None,
        non_text=[],
        extra=None,
    )
    assert turn.metadata() is None


# --------------------------------------------------------------------------
# Voice-specific signal that must not be lost
# --------------------------------------------------------------------------


def test_an_interrupted_turn_is_flagged(tmp_path):
    """Barge-in is the difference between an answer and half of one.

    A data_preparation stage can drop interrupted turns, but only if it can see
    them — so the flag has to survive into the corpus.
    """
    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)
    say(session, "assistant", "Your appointment is on Tues—", interrupted=True)
    assert (messages()[0].metadata or {})["interrupted"] is True


def test_a_clean_turn_carries_no_interrupted_key(tmp_path):
    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)
    say(session, "assistant", "Booked.")
    assert "interrupted" not in (messages()[0].metadata or {})


def test_stt_confidence_is_kept(tmp_path):
    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)
    say(session, "user", "Tuesday", transcript_confidence=0.82)
    assert (messages()[0].metadata or {})["transcript_confidence"] == 0.82


def test_non_text_content_is_named_but_not_inlined(tmp_path):
    """Audio frames have no place in a text corpus; their presence does."""
    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)
    session.emit(
        "conversation_item_added",
        ItemAdded(ChatMessage("user", ["hello", AudioContent()])),
    )
    assert (messages()[0].metadata or {})["non_text_content"] == ["AudioContent"]
    assert messages()[0].content == "hello"


def test_a_silent_item_is_not_recorded(tmp_path):
    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)
    session.emit("conversation_item_added", ItemAdded(ChatMessage("user", [])))
    assert messages() == []


def test_an_interrupted_agent_turn_is_not_a_training_target(tmp_path):
    """The reason `interrupted` is load-bearing and not just informational.

    A turn the caller cut off is half an utterance. Marked trainable, it teaches
    the model to stop mid-sentence — a defect that then shows up in every future
    generation. The flag outranks even an explicit thumbs-up, because it
    describes what the turn *is*, not how good it was.
    """
    start(tmp_path)
    session = FakeSession()
    rec = attach(session, journey_id=JID)
    say(session, "user", "Book Tuesday.")
    say(session, "assistant", "Let me just check the—", interrupted=True)
    say(session, "assistant", "Booked for Tuesday 3pm.")
    rec.signal("thumbs_up")
    session.emit("close", CloseEv(Reason("task_completed")))

    result = odyssey.fold(events(), data_source="livekit")
    from odyssey.fold import derive_trainable_status

    msgs = [e for e in events() if e.kind == "message" and e.message]
    statuses = derive_trainable_status(
        {e.seq: e.message for e in msgs if e.message is not None}, result.signals
    )
    # Keyed by content, not a literal seq: a barge_in voice event (item 0'.4)
    # now shares the seq space with messages, so message seqs are no longer
    # contiguous small ints.
    by_content = {
        e.message.content: statuses[e.seq] for e in msgs if e.message is not None
    }
    assert (
        by_content["Let me just check the—"] == "not_trainable"
    ), "a cut-off agent turn must not be a target"
    assert by_content["Booked for Tuesday 3pm."] == "trainable"


def test_an_interrupted_user_turn_stays_normal(tmp_path):
    """A caller being talked over is ordinary conversation, not a bad target.

    User turns are context, never targets, so the flag changes nothing for them —
    but it must not be applied in a way that implies otherwise.
    """
    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)
    say(session, "user", "I wanted to ask about—", interrupted=True)
    say(session, "assistant", "Go ahead.")
    session.emit("close", CloseEv(Reason("task_completed")))

    from odyssey.fold import derive_trainable_status

    result = odyssey.fold(events(), data_source="livekit")
    msgs = [e for e in events() if e.kind == "message" and e.message]
    statuses = derive_trainable_status(
        {e.seq: e.message for e in msgs if e.message is not None}, result.signals
    )
    by_content = {
        e.message.content: statuses[e.seq] for e in msgs if e.message is not None
    }
    # role default, as any user turn
    assert by_content["I wanted to ask about—"] == "not_trainable"
    assert by_content["Go ahead."] == "trainable"


# --------------------------------------------------------------------------
# Tool calls — absent from conversation_item_added, so a separate hook
# --------------------------------------------------------------------------


def test_tool_calls_and_outputs_are_paired(tmp_path):
    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)

    say(session, "user", "Book Tuesday 3pm.")
    session.emit(
        "function_tools_executed",
        ToolsExecuted(
            [
                (
                    FunctionCall("c1", "book", '{"day": "tue", "time": "15:00"}'),
                    FunctionCallOutput("c1", "book", '{"ref": "BK-1"}'),
                )
            ]
        ),
    )
    say(session, "assistant", "Booked.")

    roles = [m.role for m in messages()]
    assert roles == ["user", "assistant", "tool", "assistant"]

    tool_calls = messages()[1].tool_calls
    assert tool_calls is not None
    call = tool_calls[0]
    assert call.id == "c1" and call.name == "book"
    # LiveKit sends arguments as a JSON string; the corpus wants a dict.
    assert call.arguments == {"day": "tue", "time": "15:00"}

    response = messages()[2].tool_response
    assert response is not None
    assert response.id == "c1" and response.response == '{"ref": "BK-1"}'
    assert response.error is None


def test_parallel_tool_calls_share_one_assistant_turn(tmp_path):
    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)
    session.emit(
        "function_tools_executed",
        ToolsExecuted(
            [
                (
                    FunctionCall("c1", "book", "{}"),
                    FunctionCallOutput("c1", "book", "ok1"),
                ),
                (
                    FunctionCall("c2", "notify", "{}"),
                    FunctionCallOutput("c2", "notify", "ok2"),
                ),
            ]
        ),
    )
    msgs = messages()
    assert [m.role for m in msgs] == ["assistant", "tool", "tool"]
    assert [c.id for c in msgs[0].tool_calls or []] == ["c1", "c2"]


def test_a_failed_tool_is_recorded_as_a_failure(tmp_path):
    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)
    session.emit(
        "function_tools_executed",
        ToolsExecuted(
            [
                (
                    FunctionCall("c1", "book", "{}"),
                    FunctionCallOutput("c1", "book", "slot taken", is_error=True),
                )
            ]
        ),
    )
    tool_response = messages()[1].tool_response
    assert tool_response is not None
    assert tool_response.error == "tool_error"


def test_a_tool_that_never_returned_is_recorded_not_skipped(tmp_path):
    """A silent tool failure is data; dropping it hides a real defect."""
    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)
    session.emit(
        "function_tools_executed",
        ToolsExecuted([(FunctionCall("c1", "book", "{}"), None)]),
    )
    response = messages()[1].tool_response
    assert response is not None
    assert response.id == "c1" and response.response is None


def test_unparseable_tool_arguments_do_not_lose_the_turn(tmp_path):
    """A model emitting bad JSON is behaviour worth keeping, not a reason to
    discard the call, the result, and the assistant message carrying them."""
    client = start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)
    session.emit(
        "function_tools_executed",
        ToolsExecuted(
            [
                (
                    FunctionCall("c1", "book", "not json"),
                    FunctionCallOutput("c1", "book", "ok"),
                )
            ]
        ),
    )
    assert [m.role for m in messages()] == ["assistant", "tool"]
    tool_calls = messages()[0].tool_calls
    assert tool_calls is not None
    args = tool_calls[0].arguments
    assert args["_odyssey_unparsed_arguments"] == "not json"
    assert "not valid JSON" in args["_odyssey_parse_error"]
    assert client.stats.capture_errors == 0


# --------------------------------------------------------------------------
# Closing — what makes the journey exportable
# --------------------------------------------------------------------------


class Reason:
    def __init__(self, value):
        self.value = value


@pytest.mark.parametrize(
    "livekit_reason,expected",
    [
        ("participant_disconnected", "ENV_DONE"),
        ("user_initiated", "ENV_DONE"),
        ("task_completed", "ENV_DONE"),
        ("error", "ERROR"),
        ("job_shutdown", "TRUNCATION"),
    ],
)
def test_close_reasons_map_to_the_schema(tmp_path, livekit_reason, expected):
    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)
    say(session, "user", "hi")
    session.emit("close", CloseEv(Reason(livekit_reason)))
    tail = events()[-1]
    assert tail.kind == "terminal"
    assert tail.terminal is not None
    assert tail.terminal.termination_reason == expected


def test_a_worker_shutdown_is_truncation_not_a_clean_end(tmp_path):
    """The platform cut the call off; the conversation is incomplete."""
    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)
    say(session, "assistant", "Your slot is on Tues")
    session.emit("close", CloseEv(Reason("job_shutdown")))
    result = odyssey.fold(events(), data_source="livekit")
    metrics = result.journey.execution_metrics
    assert metrics is not None
    assert metrics.termination_reason == "TRUNCATION"


def test_close_carries_the_error(tmp_path):
    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)
    say(session, "user", "hi")
    session.emit("close", CloseEv(Reason("error"), error=RuntimeError("llm down")))
    terminal = events()[-1].terminal
    assert terminal is not None
    err = terminal.error
    assert err is not None and "llm down" in err


def test_close_is_idempotent(tmp_path):
    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)
    say(session, "user", "hi")
    session.emit("close", CloseEv(Reason("user_initiated")))
    session.emit("close", CloseEv(Reason("user_initiated")))
    assert [e.kind for e in events()].count("terminal") == 1


def test_close_detaches_the_handlers(tmp_path):
    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)
    session.emit("close", CloseEv(Reason("user_initiated")))
    say(session, "user", "after the call ended")
    assert messages() == []


def test_an_unclosed_session_is_not_exportable(tmp_path):
    """No terminal means fold() cannot tell 'still on the call' from 'lost'."""
    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)
    say(session, "user", "hi")
    result = odyssey.fold(events(), data_source="livekit")
    assert not result.trainable
    assert "may still be running" in (result.incomplete_reason or "")


def test_detach_stops_recording_without_closing(tmp_path):
    start(tmp_path)
    session = FakeSession()
    rec = attach(session, journey_id=JID)
    say(session, "user", "recorded")
    rec.detach()
    say(session, "user", "not recorded")
    assert [m.content for m in messages()] == ["recorded"]
    assert [e.kind for e in events()].count("terminal") == 0


# --------------------------------------------------------------------------
# Concurrency — one worker process, many calls
# --------------------------------------------------------------------------


def test_two_sessions_do_not_bleed_into_each_other(tmp_path):
    """A worker runs many calls at once; the journey is held per recorder.

    Relying on the ambient ContextVar here would interleave two callers into one
    journey, which is the corruption writer_id detection exists to catch.
    """
    start(tmp_path)
    a, b = FakeSession(), FakeSession()
    attach(a, journey_id="call_a")
    attach(b, journey_id="call_b")

    say(a, "user", "I am caller A")
    say(b, "user", "I am caller B")
    say(a, "assistant", "Hello A")
    say(b, "assistant", "Hello B")

    assert [m.content for m in messages("call_a")] == ["I am caller A", "Hello A"]
    assert [m.content for m in messages("call_b")] == ["I am caller B", "Hello B"]
    assert [e.seq for e in events("call_a")] == [0, 1]


def test_recording_works_outside_any_ambient_journey(tmp_path):
    """attach() must not require the entrypoint to be inside a `with` block."""
    from odyssey.context import current

    start(tmp_path)
    assert current() is None
    session = FakeSession()
    attach(session, journey_id=JID)
    say(session, "user", "hi")
    assert current() is None  # and it does not leak one either
    assert len(messages()) == 1


# --------------------------------------------------------------------------
# Never take the call down
# --------------------------------------------------------------------------


def test_a_capture_failure_never_reaches_livekit(tmp_path):
    """An exception here propagates into LiveKit's loop and can drop the call."""
    client = start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)

    def boom(_event):
        raise OSError("disk full")

    client.spool.record = boom  # type: ignore[method-assign]
    say(session, "user", "hi")  # must not raise
    session.emit("close", CloseEv(Reason("user_initiated")))
    assert client.stats.capture_errors >= 1


def test_a_malformed_tools_event_is_survived_but_reported(tmp_path):
    """The call lives; the lost tool turn is counted rather than swallowed.

    An unreadable event is NOT the same as an empty batch. Treating it as one is
    how a whole tool turn leaves no trace: the corpus then shows a failed booking
    as a clean conversation, with `num_tool_calls == 0` to confirm it.
    """
    client = start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)
    session.emit("function_tools_executed", object())  # neither accessor
    assert messages() == []  # nothing invented from an event we cannot read
    assert client.stats.capture_errors == 1
    assert "function_tools_executed" in client.stats.recent_errors[-1]


def test_an_empty_tool_batch_is_not_an_error(tmp_path):
    """The other half of the distinction: genuinely nothing ran."""

    class Empty:
        def zipped(self):
            return []

    client = start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)
    session.emit("function_tools_executed", Empty())
    assert messages() == []
    assert client.stats.capture_errors == 0


def test_tools_are_recorded_from_the_parallel_lists_without_zipped(tmp_path):
    """Older livekit-agents exposed the lists but no `zipped()`.

    They are what `zipped()` zips, so reading them directly captures the tool
    turn instead of dropping it.
    """

    class NoZipped:
        def __init__(self, calls, outputs):
            self.function_calls = calls
            self.function_call_outputs = outputs

    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)
    session.emit(
        "function_tools_executed",
        NoZipped(
            [FunctionCall("c1", "check_slot", '{"day": "tue"}')],
            [FunctionCallOutput("c1", "check_slot", "ok")],
        ),
    )

    msgs = messages()
    tool_calls = msgs[0].tool_calls
    assert tool_calls is not None
    assert tool_calls[0].name == "check_slot"
    assert tool_calls[0].arguments == {"day": "tue"}
    tool_response = msgs[1].tool_response
    assert tool_response is not None
    assert tool_response.response == "ok"


def test_a_short_outputs_list_still_records_every_call(tmp_path):
    """Lists that disagree in length are recorded as far as they go, matching
    livekit's own `strict=False` zip — a batch half-reported beats one dropped."""

    class Ragged:
        def __init__(self, calls, outputs):
            self.function_calls = calls
            self.function_call_outputs = outputs

    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)
    session.emit(
        "function_tools_executed",
        Ragged(
            [FunctionCall("c1", "a", "{}"), FunctionCall("c2", "b", "{}")],
            [FunctionCallOutput("c1", "a", "done")],
        ),
    )

    msgs = messages()
    assert [c.name for c in msgs[0].tool_calls or []] == ["a", "b"]
    # The call with no output is recorded as one — `num_tool_response_none`
    # counts it, and a silently missing second call would not be countable.
    assert [m.tool_response.name for m in msgs[1:] if m.tool_response is not None] == [
        "a",
        "b",
    ]
    tool_response = msgs[2].tool_response
    assert tool_response is not None
    assert tool_response.response is None


def test_attach_without_init_records_nothing_and_does_not_raise(tmp_path):
    import odyssey.client as client_mod

    client_mod._warned_uninitialised = False
    session = FakeSession()
    with pytest.warns(RuntimeWarning, match="init"):
        attach(session, journey_id=JID)
        say(session, "user", "hi")
    session.emit("close", CloseEv(Reason("user_initiated")))


def test_disabled_client_records_nothing(tmp_path):
    start(tmp_path, enabled=False)
    session = FakeSession()
    attach(session, journey_id=JID)
    say(session, "user", "hi")
    assert events() == []


# --------------------------------------------------------------------------
# The app supplies what session events cannot
# --------------------------------------------------------------------------


def test_the_app_can_attach_feedback_after_the_turns(tmp_path):
    """LiveKit knows what was said, not whether it was any good."""
    start(tmp_path)
    session = FakeSession()
    rec = attach(session, journey_id=JID)
    say(session, "user", "Book Tuesday.")
    say(session, "assistant", "Booked.")
    rec.signal("thumbs_up")
    rec.reward(0.9)
    session.emit("close", CloseEv(Reason("task_completed")))

    result = odyssey.fold(events(), data_source="livekit")
    assert result.trainable
    assert [s.trainable_status for s in result.journey.steps][-1] == "trainable"
    metrics = result.journey.metrics
    assert metrics is not None
    assert metrics.aggregated_reward == pytest.approx(0.9)


# --------------------------------------------------------------------------
# The whole call, end to end
# --------------------------------------------------------------------------


def test_a_full_call_folds_into_a_trainable_journey(tmp_path):
    start(tmp_path)
    session = FakeSession()
    rec = attach(session, journey_id=JID, channel="voice")

    say(session, "developer", "You are a booking assistant.")
    say(session, "user", "I need an appointment Tuesday.", transcript_confidence=0.91)
    say(session, "assistant", "What time on Tuesday?")
    say(session, "user", "3pm.")
    session.emit(
        "function_tools_executed",
        ToolsExecuted(
            [
                (
                    FunctionCall("c1", "book", '{"day": "tue", "time": "15:00"}'),
                    FunctionCallOutput("c1", "book", '{"ref": "BK-7"}'),
                )
            ]
        ),
    )
    say(session, "assistant", "Booked for Tuesday 3pm.")
    rec.signal("thumbs_up")
    session.emit("close", CloseEv(Reason("participant_disconnected")))

    result = odyssey.fold(events(), data_source="livekit")
    assert result.trainable
    assert result.incomplete_reason is None
    assert [m.role for m in messages()] == [
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    metrics = result.journey.metrics
    assert metrics is not None
    assert metrics.num_tool_calls == 1
    assert metrics.num_tool_failures == 0
    assert [s.trainable_status for s in result.journey.steps][-1] == "trainable"


# --------------------------------------------------------------------------
# The system prompt
# --------------------------------------------------------------------------


def test_the_system_prompt_is_recorded_before_the_first_turn(tmp_path):
    """Without it a step shows the exchange but not what the agent was told.

    Two calls under different prompts would otherwise be indistinguishable, so
    the prompt is not optional context — it is what the turn was conditioned on.
    """
    start(tmp_path)
    session = FakeSession()
    session.current_agent = FakeAgent("You are a golf booking agent.")
    attach(session, journey_id=JID)

    say(session, "user", "Hello.")
    say(session, "assistant", "Good morning!")

    assert [(m.role, m.content) for m in messages()] == [
        ("system", "You are a golf booking agent."),
        ("user", "Hello."),
        ("assistant", "Good morning!"),
    ]


def test_the_system_prompt_is_recorded_once(tmp_path):
    """It is polled per item, so an unchanged prompt must not re-emit."""
    start(tmp_path)
    session = FakeSession()
    session.current_agent = FakeAgent("stable prompt")
    attach(session, journey_id=JID)

    for i in range(4):
        say(session, "user", f"turn {i}")

    assert [m.content for m in messages() if m.role == "system"] == ["stable prompt"]


def test_the_prompt_is_read_at_the_first_turn_not_at_attach(tmp_path):
    """The agent arrives with ``session.start(agent=...)``, after ``attach()``.

    Reading once at attach time would capture nothing for every deployment that
    follows LiveKit's own examples.
    """
    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)  # no agent yet

    session.current_agent = FakeAgent("late prompt")
    say(session, "user", "Hello.")

    assert [(m.role, m.content) for m in messages()] == [
        ("system", "late prompt"),
        ("user", "Hello."),
    ]


def test_an_agent_handoff_swaps_the_prompt(tmp_path):
    """A handoff means later turns ran under different instructions.

    ``build_cumulative_steps`` applies it copy-on-write, so the pre-handoff step
    keeps the original prompt and only later steps see the new one.
    """
    start(tmp_path)
    session = FakeSession()
    session.current_agent = FakeAgent("greeter")
    attach(session, journey_id=JID)

    say(session, "user", "Hello.")
    say(session, "assistant", "Transferring you.")
    session.current_agent = FakeAgent("booking specialist")
    say(session, "user", "Book me Tuesday.")
    say(session, "assistant", "Done.")

    systems = [m for m in messages() if m.role == "system"]
    assert [m.content for m in systems] == ["greeter", "booking specialist"]
    assert [(m.metadata or {})["instructions_origin"] for m in systems] == [
        "initial",
        "handoff",
    ]
    assert (systems[0].metadata or {})["agent"] == "FakeAgent"


def test_a_session_that_cannot_report_its_agent_still_records_the_call(tmp_path):
    """``current_agent`` raises pre-start, and realtime models may not expose
    instructions at all. Losing the prompt is bad; losing the call is worse.
    """
    start(tmp_path)
    session = UnstartedSession()
    attach(session, journey_id=JID)

    say(session, "user", "Hello.")

    assert [(m.role, m.content) for m in messages()] == [("user", "Hello.")]


def test_explicit_instructions_seed_the_prompt_when_the_session_hides_it(tmp_path):
    """For deployments that template the prompt outside the Agent object."""
    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID, instructions="templated prompt")

    say(session, "user", "Hello.")

    assert [(m.role, m.content) for m in messages()][0] == (
        "system",
        "templated prompt",
    )


def test_the_session_agent_wins_over_the_seed(tmp_path):
    """The seed is a fallback: the live agent is authoritative and follows
    handoffs, which a value frozen at attach time cannot."""
    start(tmp_path)
    session = FakeSession()
    session.current_agent = FakeAgent("live prompt")
    attach(session, journey_id=JID, instructions="stale seed")

    say(session, "user", "Hello.")

    assert [m.content for m in messages() if m.role == "system"] == ["live prompt"]


def test_a_blank_prompt_is_not_recorded(tmp_path):
    """An empty `instructions` is the default, not a prompt."""
    start(tmp_path)
    session = FakeSession()
    session.current_agent = FakeAgent("   ")
    attach(session, journey_id=JID)

    say(session, "user", "Hello.")

    assert [m.role for m in messages()] == ["user"]


# --------------------------------------------------------------------------
# Step shape for a real voice call
# --------------------------------------------------------------------------


def test_a_voice_call_folds_to_one_step_per_turn(tmp_path):
    """The regression this exists for.

    A voice agent emits one conversation item per spoken utterance, so an agent
    turn arrives as two or three assistant messages. Snapshotting each one gave
    a step per utterance: a real 15-turn booking call produced 33 steps, each a
    near-copy of the last, and the system prompt appeared in none of them.
    """
    start(tmp_path)
    session = FakeSession()
    session.current_agent = FakeAgent("You are a golf booking agent.")
    rec = attach(session, journey_id=JID)

    say(session, "user", "Hello.")
    say(session, "assistant", "Good morning, Sanyam!")
    say(session, "user", "Chandigarh.")
    say(session, "assistant", "I found Chandigarh Golf Club.")
    say(session, "assistant", "Do you want to book there?")
    say(session, "user", "3 PM, 3 players.")
    say(session, "assistant", "Three players, got it.")
    say(session, "assistant", "Let me check availability.")
    say(session, "assistant", "The price is five thousand four hundred rupees.")
    rec.close(reason="ENV_DONE")

    journey = odyssey.fold(events(), data_source="livekit").journey

    # 3 turns, not 6 assistant utterances.
    assert len(journey.steps) == 3
    metrics = journey.metrics
    assert metrics is not None
    assert metrics.steps == 3
    # Every step is conditioned on the prompt, and every step ends on the
    # agent's completed turn, so every step is a usable target.
    for step in journey.steps:
        assert step.messages[0].role == "system"
        assert step.messages[-1].role == "assistant"
        assert step.trainable_status == "trainable"
    # Cumulative: each step is the previous one plus that turn.
    # Each turn adds exactly two messages -- the caller's and the agent's -- no
    # matter how many utterances the agent spoke it in.
    assert [len(s.messages) for s in journey.steps] == [3, 5, 7]


# --------------------------------------------------------------------------
# One message per turn, never a stream
# --------------------------------------------------------------------------


def test_interim_transcripts_are_never_subscribed_to(tmp_path):
    """`user_input_transcribed` fires per partial hypothesis while the caller
    is still speaking. Consuming it would flood the spool with prefixes of one
    sentence -- "I need", "I need an", "I need an appointment" -- and its final
    is the same text `conversation_item_added` already delivers.

    Asserted on the subscription, not just on behaviour, so wiring it up in
    future fails here.
    """
    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)

    assert set(session.handlers) == {
        "conversation_item_added",
        "function_tools_executed",
        "close",
    }

    class Transcribed:
        transcript = "I need an"
        is_final = False

    session.emit("user_input_transcribed", Transcribed())
    assert messages() == []


def test_an_agent_turn_spoken_as_several_utterances_is_one_message(tmp_path):
    """The regression this exists for.

    TTS is driven utterance by utterance and `conversation_item_added` fires for
    each, so one agent reply arrives as three items. Recorded as three messages
    it is the stream, not the conversation: the SFT target for that turn is the
    whole reply, and consecutive same-role messages are rejected outright by the
    OpenAI and Anthropic chat formats.
    """
    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)

    say(session, "user", "Yeah, yeah.")
    say(session, "assistant", "I have found a slot at three PM.", id="item_a")
    say(
        session,
        "assistant",
        "The price is five thousand four hundred rupees.",
        id="item_b",
    )
    say(session, "assistant", "Would you like to book this slot?", id="item_c")

    msgs = messages()
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[1].content == (
        "I have found a slot at three PM. "
        "The price is five thousand four hundred rupees. "
        "Would you like to book this slot?"
    )
    # Nothing is lost: every utterance is still individually addressable.
    assert (msgs[1].metadata or {})["utterances"] == 3
    assert (msgs[1].metadata or {})["provider_item_ids"] == [
        "item_a",
        "item_b",
        "item_c",
    ]


def test_split_stt_finals_are_one_caller_turn(tmp_path):
    """The caller's side streams too: STT emits split finals for one utterance."""
    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)

    say(session, "user", "2, 24.", transcript_confidence=0.71)
    say(session, "user", "3 PM, 3 players.", transcript_confidence=0.81)
    say(session, "assistant", "Three players, got it.")

    msgs = messages()
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[0].content == "2, 24. 3 PM, 3 players."


def test_a_single_utterance_turn_reads_exactly_as_before(tmp_path):
    """One utterance still reports a list — the shape never depends on the count."""
    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)

    say(session, "user", "Hello.", id="item_solo", transcript_confidence=0.92)
    say(session, "assistant", "Hi.")

    meta = messages()[0].metadata
    assert meta is not None
    assert meta["provider_item_ids"] == ["item_solo"]
    assert meta["transcript_confidence"] == 0.92
    assert "parts" not in meta and "utterances" not in meta


def test_merged_confidence_is_weighted_by_how_much_was_said(tmp_path):
    """A plain mean lets a two-word fragment drag down a long sentence.

    Length weighting is what a single item covering the whole turn would have
    reported, which is what a downstream confidence filter is calibrated for.
    """
    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)

    long_text = "x" * 90  # 90 chars at 1.0
    say(session, "user", long_text, transcript_confidence=1.0)
    say(session, "user", "ok", transcript_confidence=0.0)  # 2 chars at 0.0
    say(session, "assistant", "noted")

    meta = messages()[0].metadata
    assert meta is not None
    assert meta["transcript_confidence"] == pytest.approx(90 / 92)
    # Per-utterance values survive for anything that wants them.
    assert [p["transcript_confidence"] for p in meta["parts"]] == [1.0, 0.0]


def test_an_interruption_ends_the_turn(tmp_path):
    """Barge-in *is* a turn boundary: the speaker stopped.

    Merging the next reply into it would discard a complete reply (the merged
    message inherits the flag) and splice a truncated fragment onto it.
    """
    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)

    say(session, "user", "Book Tuesday.")
    say(session, "assistant", "Let me just check the—", interrupted=True)
    say(session, "assistant", "Booked for Tuesday 3pm.")

    msgs = messages()
    assert [m.content for m in msgs[1:]] == [
        "Let me just check the—",
        "Booked for Tuesday 3pm.",
    ]
    assert (msgs[1].metadata or {})["interrupted"] is True
    assert "interrupted" not in (msgs[2].metadata or {})


def test_a_turn_cut_off_mid_stream_is_flagged_whole(tmp_path):
    """Utterances one and two landed cleanly; the third was cut off.

    The reply as a whole is truncated, so the merged message carries the flag and
    the fold refuses it as a target.
    """
    start(tmp_path)
    session = FakeSession()
    rec = attach(session, journey_id=JID)

    say(session, "user", "How much?")
    say(session, "assistant", "I have found a slot.")
    say(session, "assistant", "The price is five thou—", interrupted=True)
    rec.close(reason="ENV_DONE")

    msgs = messages()
    assert msgs[1].content == "I have found a slot. The price is five thou—"
    assert (msgs[1].metadata or {})["interrupted"] is True
    journey = odyssey.fold(events(), data_source="livekit").journey
    assert journey.steps[-1].trainable_status == "not_trainable"


def test_speech_before_a_tool_call_joins_the_call_message(tmp_path):
    """The agent often says "let me check" before calling the tool.

    That was one generation: text and a tool call. `content` + `tool_calls` on a
    single assistant message is exactly how OpenAI and Anthropic represent it,
    and writing them as two consecutive assistant messages would produce a
    message list those formats reject.
    """
    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)

    say(session, "user", "Book Tuesday.")
    say(session, "assistant", "Sure, let me check availability.")
    session.emit(
        "function_tools_executed",
        ToolsExecuted(
            [
                (
                    FunctionCall("c1", "book", '{"day": "tue"}'),
                    FunctionCallOutput("c1", "book", "ok"),
                )
            ]
        ),
    )
    say(session, "assistant", "Booked.")

    msgs = messages()
    assert [(m.role, m.content) for m in msgs] == [
        ("user", "Book Tuesday."),
        ("assistant", "Sure, let me check availability."),
        ("tool", None),
        ("assistant", "Booked."),
    ]
    assert [c.name for c in msgs[1].tool_calls or []] == ["book"]
    # No two messages in a row share a role, which is the property the chat
    # formats actually require.
    assert not any(a.role == b.role for a, b in zip(msgs, msgs[1:]))


def test_a_tool_call_with_no_speech_before_it_stands_alone(tmp_path):
    """The agent can call a tool without saying anything first.

    The caller's turn is still open at that point, and it is a different turn --
    it has to land before the call, not be merged into it.
    """
    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)

    say(session, "user", "Book Tuesday.")
    session.emit(
        "function_tools_executed",
        ToolsExecuted(
            [
                (
                    FunctionCall("c1", "book", '{"day": "tue"}'),
                    FunctionCallOutput("c1", "book", "ok"),
                )
            ]
        ),
    )

    msgs = messages()
    assert [(m.role, m.content) for m in msgs] == [
        ("user", "Book Tuesday."),
        ("assistant", None),
        ("tool", None),
    ]
    assert [c.name for c in msgs[1].tool_calls or []] == ["book"]


def test_a_handoff_does_not_jump_ahead_of_the_turn_it_replaced(tmp_path):
    """A handoff can land mid-turn.

    The new prompt must not be written before the turn that ran under the old
    one, or copy-on-write hands that turn the wrong instructions.
    """
    start(tmp_path)
    session = FakeSession()
    session.current_agent = FakeAgent("greeter")
    attach(session, journey_id=JID)

    say(session, "user", "Hello.")
    say(session, "assistant", "Transferring you.")
    session.current_agent = FakeAgent("booking specialist")
    say(session, "assistant", "You are through to booking.")

    assert [(m.role, m.content) for m in messages()] == [
        ("system", "greeter"),
        ("user", "Hello."),
        ("assistant", "Transferring you."),
        ("system", "booking specialist"),
        ("assistant", "You are through to booking."),
    ]


def test_a_rating_lands_on_the_turn_the_caller_just_heard(tmp_path):
    """`signal()` defaults to the most recent message.

    A turn still being assembled has no seq yet, so without a flush the rating
    would land on the turn before it.
    """
    start(tmp_path)
    session = FakeSession()
    rec = attach(session, journey_id=JID)

    say(session, "user", "Book Tuesday.")
    say(session, "assistant", "Booked.")
    rec.signal("thumbs_up")

    signal = next(e for e in events() if e.kind == "signal")
    assert signal.signal is not None
    rated = {
        e.seq: e.message
        for e in events()
        if e.kind == "message" and e.message is not None
    }[signal.signal.target_seq]
    assert (rated.role, rated.content) == ("assistant", "Booked.")


def test_a_turn_is_not_written_until_the_speaker_is_done(tmp_path):
    """The buffering the test harness papers over, asserted directly.

    At most one turn is ever held, so a crash mid-call loses the turn in flight
    and nothing before it.
    """
    start(tmp_path)
    session = FakeSession()
    rec = attach(session, journey_id=JID)

    say(session, "user", "Hello.")
    say(session, "assistant", "Hi there.")
    client = odyssey.get_client()
    assert client is not None
    assert [e.message.role for e in client.spool.read(JID) if e.message] == ["user"]

    rec.flush()
    assert [e.message.role for e in client.spool.read(JID) if e.message] == [
        "user",
        "assistant",
    ]
    rec.flush()  # idempotent
    assert len(client.spool.read(JID)) == 2


def test_close_writes_the_agents_sign_off(tmp_path):
    """Nothing else comes along to flush the last turn of the call."""
    start(tmp_path)
    session = FakeSession()
    rec = attach(session, journey_id=JID)

    say(session, "user", "Thanks.")
    say(session, "assistant", "Have a great day!")
    rec.close(reason="ENV_DONE")

    client = odyssey.get_client()
    assert client is not None
    assert [
        e.message.content
        for e in client.spool.read(JID)
        if e.kind == "message" and e.message
    ] == ["Thanks.", "Have a great day!"]


# --------------------------------------------------------------------------
# The terminal event: a journey that leaves this process without one is
# refused by fold() forever, because nothing will arrive to complete it
# --------------------------------------------------------------------------


def test_a_session_that_never_closes_is_terminated_at_shutdown(tmp_path):
    """`AgentSession._aclose()` emits `close` only after a series of awaits —
    draining speech, closing recorder IO, closing every toolset. A worker killed
    or a job shut down partway through never reaches the emit."""
    client = start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)
    say(session, "user", "hi")

    assert client.health()["open_journeys"] == 1
    assert [e.kind for e in events()] == ["message"]  # no terminal yet

    odyssey.shutdown()  # process exit; no `close` ever fired

    folded = odyssey.fold(
        odyssey.read_events(tmp_path / "out" / f"{JID}.jsonl").events,
        data_source="livekit",
    )
    assert folded.complete is True
    assert folded.trainable is True
    metrics = folded.journey.execution_metrics
    assert metrics is not None
    assert metrics.termination_reason == "STALE"
    # Labelled, not disguised: a corpus stage can drop these on sight.
    error = folded.journey.error
    assert error is not None
    assert "without the session closing its journey" in error


def test_a_real_close_keeps_its_own_termination_reason(tmp_path):
    """Shutdown must not win the race against a session that ended properly.

    Closers are idempotent, so this only ever reaches journeys nothing ended.
    """
    client = start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)
    say(session, "user", "hi")
    session.emit("close", CloseEv(Reason("user_initiated")))

    assert client.health()["open_journeys"] == 0
    odyssey.shutdown()

    folded = odyssey.fold(
        odyssey.read_events(tmp_path / "out" / f"{JID}.jsonl").events,
        data_source="livekit",
    )
    metrics = folded.journey.execution_metrics
    assert metrics is not None
    assert metrics.termination_reason == "ENV_DONE"
    assert folded.journey.error is None


def test_only_one_terminal_event_is_ever_written(tmp_path):
    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID)
    say(session, "user", "hi")
    session.emit("close", CloseEv(Reason("user_initiated")))
    odyssey.shutdown()

    out = odyssey.read_events(tmp_path / "out" / f"{JID}.jsonl")
    assert [e.kind for e in out.events].count("terminal") == 1


def test_a_detached_recorder_is_not_terminated_by_shutdown(tmp_path):
    """`detach` means "stop recording", not "end the journey" — and a detached
    recorder will never record again, so shutdown has no terminal to add."""
    client = start(tmp_path)
    session = FakeSession()
    rec = attach(session, journey_id=JID)
    say(session, "user", "hi")
    rec.detach()

    assert client.health()["open_journeys"] == 0
    odyssey.shutdown()
    out = odyssey.read_events(tmp_path / "out" / f"{JID}.jsonl")
    assert "terminal" not in [e.kind for e in out.events]


def test_concurrent_calls_are_each_terminated(tmp_path):
    """One worker process runs many calls; shutdown must not stop at the first."""
    start(tmp_path)
    for i in range(3):
        session = FakeSession()
        attach(session, journey_id=f"call_{i}")
        say(session, "user", "hi", id=f"item_{i}")

    odyssey.shutdown()
    for i in range(3):
        events_i = odyssey.read_events(tmp_path / "out" / f"call_{i}.jsonl").events
        terminal = events_i[-1].terminal
        assert events_i[-1].kind == "terminal"
        assert terminal is not None
        assert terminal.termination_reason == "STALE"


# --------------------------------------------------------------------------
# Flow / playbook mode: the prompt is not on the agent, and LiveKit runs no
# tools. Both are structural, not misconfiguration.
# --------------------------------------------------------------------------


def test_a_callable_supplies_the_prompt_the_agent_does_not_have(tmp_path):
    """`llm_node` returning None means LiveKit's LLM never runs, so flow mode
    builds `Agent(instructions="")` and the prompt lives in the dialog machine.
    Polling the session finds nothing, forever."""
    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID, instructions=lambda: "Node 1 prompt.")
    say(session, "user", "hi")

    system = [m for m in messages() if m.role == "system"]
    assert [m.content for m in system] == ["Node 1 prompt."]


def test_a_per_node_prompt_change_is_recorded_as_a_handoff(tmp_path):
    """The reason it is a callable and not a string: the flow rebuilds the
    prompt per node, and a value frozen at attach time cannot follow that."""
    prompt = ["Greeting node."]
    start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID, instructions=lambda: prompt[0])

    say(session, "user", "hi")
    prompt[0] = "Booking node."
    say(session, "user", "book me")

    system = [m for m in messages() if m.role == "system"]
    assert [m.content for m in system] == ["Greeting node.", "Booking node."]
    assert (system[1].metadata or {})["instructions_origin"] == "handoff"


def test_the_live_agent_still_wins_over_the_callable(tmp_path):
    """The callable is a fallback. A deployment that does put the prompt on its
    agent keeps following handoffs the normal way."""

    class WithAgent(FakeSession):
        current_agent = type("A", (), {"instructions": "Real agent prompt."})()

    start(tmp_path)
    session = WithAgent()
    attach(session, journey_id=JID, instructions=lambda: "fallback")
    say(session, "user", "hi")

    system = [m for m in messages() if m.role == "system"]
    assert [m.content for m in system] == ["Real agent prompt."]


def test_a_raising_prompt_reader_does_not_break_capture(tmp_path):
    def boom():
        raise RuntimeError("machine not started")

    client = start(tmp_path)
    session = FakeSession()
    attach(session, journey_id=JID, instructions=boom)
    say(session, "user", "hi")

    assert [m.content for m in messages()] == ["hi"]  # turn still recorded
    assert client.stats.capture_errors >= 1


def test_recorder_tool_records_a_call_livekit_never_ran(tmp_path):
    """Flow mode executes its own tools, so `function_tools_executed` never
    fires and the journey shows `num_tool_calls == 0` however many ran."""
    start(tmp_path)
    session = FakeSession()
    rec = attach(session, journey_id=JID)
    say(session, "user", "Book tomorrow 3pm for four.")
    rec.tool(
        "check_availability",
        {"course": "Qutub", "players": 4},
        result={"slots": ["15:00"]},
        call_id="c1",
    )

    msgs = messages()
    call = next(m for m in msgs if m.tool_calls)
    resp = next(m for m in msgs if m.tool_response)
    assert call.tool_calls is not None
    assert resp.tool_response is not None
    assert call.tool_calls[0].name == "check_availability"
    assert call.tool_calls[0].arguments == {"course": "Qutub", "players": 4}
    assert resp.tool_response.id == call.tool_calls[0].id == "c1"
    assert resp.tool_response.response == {"slots": ["15:00"]}


def test_a_tool_recorded_by_hand_reaches_the_metrics(tmp_path):
    """The whole point: a failed booking must stop reading as a clean call."""
    start(tmp_path)
    session = FakeSession()
    rec = attach(session, journey_id=JID)
    say(session, "user", "book me")
    rec.tool("check_availability", {"players": 4}, error="upstream 503")
    session.emit("close", CloseEv(Reason("user_initiated")))

    m = odyssey.fold(events(), data_source="livekit").journey.metrics
    assert m is not None
    assert m.num_tool_calls == 1
    assert m.num_tool_failures == 1
    assert m.tool_error_rate == 1.0


def test_speech_before_a_hand_recorded_tool_joins_the_call_message(tmp_path):
    """Same shape as the event path: "let me check availability" and the lookup
    were one turn, and content + tool_calls on one message is how OpenAI and
    Anthropic represent that."""
    start(tmp_path)
    session = FakeSession()
    rec = attach(session, journey_id=JID)
    say(session, "assistant", "Ok, let me check availability.")
    rec.tool("check_availability", {"players": 4}, result="ok")

    call = next(m for m in messages() if m.tool_calls)
    assert call.content == "Ok, let me check availability."
    assert call.role == "assistant"


def test_a_tool_that_never_returned_is_recorded_as_such(tmp_path):
    """Distinct from one that returned None — `num_tool_response_none` counts
    it, and a skipped call could not be counted at all."""
    start(tmp_path)
    session = FakeSession()
    rec = attach(session, journey_id=JID)
    say(session, "user", "book me")
    rec.tool("check_availability", {"players": 4}, responded=False)
    session.emit("close", CloseEv(Reason("user_initiated")))

    m = odyssey.fold(events(), data_source="livekit").journey.metrics
    assert m is not None
    assert m.num_tool_response_none == 1


def test_a_hand_recorded_tool_gets_a_call_id_when_none_is_given(tmp_path):
    """Correlation is the thing a corpus cannot reconstruct later."""
    start(tmp_path)
    session = FakeSession()
    rec = attach(session, journey_id=JID)
    rec.tool("check", {}, result="ok")

    msgs = messages()
    call = next(m for m in msgs if m.tool_calls)
    resp = next(m for m in msgs if m.tool_response)
    assert call.tool_calls is not None
    assert resp.tool_response is not None
    assert call.tool_calls[0].id
    assert call.tool_calls[0].id == resp.tool_response.id


def test_unserializable_tool_arguments_do_not_lose_the_turn(tmp_path):
    """A flow machine passes whatever its nodes hold; `repr()` beats losing the
    tool call."""

    class Weird:
        def __repr__(self):
            return "<Weird>"

    start(tmp_path)
    session = FakeSession()
    rec = attach(session, journey_id=JID)
    rec.tool("check", {"obj": Weird()}, result=Weird())

    call = next(m for m in messages() if m.tool_calls)
    assert call.tool_calls is not None
    assert call.tool_calls[0].arguments == {"obj": "<Weird>"}


def test_the_system_prompt_can_be_kept_out_of_the_journey(tmp_path):
    """Some prompts are thousands of tokens of business rules.

    A deployment whose prompt dwarfs the call itself may not want that text
    copied into every exported artifact. Everything else still lands: the
    greeting, both sides of the conversation, and the tool calls.
    """
    start(tmp_path)
    session = FakeSession()
    session.current_agent = FakeAgent("A very long book of business rules.")
    attach(session, journey_id=JID, record_instructions=False)

    say(session, "assistant", "Good afternoon, Sanyam!")
    say(session, "user", "book there again")

    recorded = messages()
    assert [m.role for m in recorded] == ["assistant", "user"]
    assert recorded[0].content == "Good afternoon, Sanyam!"


def test_a_callable_prompt_is_also_skipped_when_recording_is_off(tmp_path):
    """The flow/playbook seed is the *other* way a prompt reaches the journey."""
    start(tmp_path)
    session = FakeSession()
    attach(
        session,
        journey_id=JID,
        instructions=lambda: "Node 1 prompt.",
        record_instructions=False,
    )
    say(session, "user", "hi")

    assert [m.role for m in messages()] == ["user"]
