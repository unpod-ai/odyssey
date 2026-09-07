"""Pipecat capture: one observer per pipeline task.

No `pipecat` install and no pipeline: frames are plain objects dispatched on
class name, which is also what proves the integration never imports Pipecat.

The dominant hazard here is not parsing — it is that `on_push_frame` fires once
per pipeline *hop*, so the same frame arrives many times. Most of what follows
is about recording it exactly once.
"""

from __future__ import annotations

import asyncio
import itertools

import pytest

import odyssey
from odyssey.integrations.pipecat import attach as _attach
from odyssey.integrations.pipecat import observer

JID = "call_pc_1"

_ids = itertools.count(1)


class Frame:
    """Base for the fakes: every Pipecat frame carries an id."""

    def __init__(self, **kw):
        self.id = next(_ids)
        for k, v in kw.items():
            setattr(self, k, v)


def frame(name, **kw):
    """A frame whose *class name* is what the integration dispatches on."""
    return type(name, (Frame,), {})(**kw)


class FramePushed:
    """Pipecat's new observer payload."""

    def __init__(self, f):
        self.source = "Src"
        self.destination = "Dst"
        self.frame = f
        self.direction = "DOWNSTREAM"
        self.timestamp = 0


class FakeTask:
    def __init__(self):
        self.observers = []

    def add_observer(self, obs):
        self.observers.append(obs)


_RECORDERS: list = []


def attach(task, **kw):
    rec = _attach(task, **kw)
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


def push(task, f, hops=1):
    """Send one frame through the observer, `hops` times — as a pipeline does."""
    for obs in task.observers:
        for _ in range(hops):
            asyncio.run(obs.on_push_frame(FramePushed(f)))


def events(jid=JID):
    for rec in _RECORDERS:
        rec.flush()
    client = odyssey.get_client()
    assert client is not None
    return client.spool.read(jid)


def messages(jid=JID):
    return [e.message for e in events(jid) if e.kind == "message" and e.message]


def voices(jid=JID):
    return [e.voice for e in events(jid) if e.kind == "voice" and e.voice]


def header(jid=JID):
    for rec in _RECORDERS:
        rec.flush()
    client = odyssey.get_client()
    assert client is not None
    return client.spool.header(jid)


def reply(task, *chunks):
    """One assistant turn, streamed the way Pipecat streams it."""
    push(task, frame("LLMFullResponseStartFrame"))
    for chunk in chunks:
        push(task, frame("LLMTextFrame", text=chunk))
    push(task, frame("LLMFullResponseEndFrame"))


def hear(task, text, **kw):
    push(task, frame("TranscriptionFrame", text=text, user_id="u_1", **kw))


# --------------------------------------------------------------------------
# The integration imports nothing from pipecat
# --------------------------------------------------------------------------


def test_the_module_does_not_import_pipecat_at_module_scope():
    """Duck typing is the whole reason no optional dependency is needed."""
    import ast
    from pathlib import Path

    import odyssey.integrations.pipecat as mod

    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    top_level = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            top_level.append(node.module or "")
    assert not [n for n in top_level if n.split(".")[0] == "pipecat"]


def test_the_observer_works_with_pipecat_absent():
    """`BaseObserver` is a typing convenience, not a requirement: Pipecat's
    `add_observer` appends to a list and calls `on_push_frame` on whatever
    is in it."""
    with pytest.warns(RuntimeWarning):
        obs = observer(journey_id=JID)
    assert hasattr(obs, "on_push_frame")
    assert hasattr(obs, "recorder")


# --------------------------------------------------------------------------
# One frame, many hops
# --------------------------------------------------------------------------


def test_a_frame_seen_on_every_hop_is_recorded_once(tmp_path):
    """The failure this prevents looks like a busy call, not like a bug: a
    six-processor pipeline would record every turn six times."""
    start(tmp_path)
    task = FakeTask()
    attach(task, journey_id=JID)
    hear(task, "book me for tuesday", hops=6)

    assert [m.content for m in messages()] == ["book me for tuesday"]


def test_two_frames_with_the_same_text_are_both_recorded(tmp_path):
    """Deduplication is on frame identity, not on content. A caller who says
    "yes" twice said it twice."""
    start(tmp_path)
    task = FakeTask()
    attach(task, journey_id=JID)
    hear(task, "yes", hops=3)
    hear(task, "yes", hops=3)

    assert [m.content for m in messages()] == ["yes", "yes"]


def test_a_frame_with_no_id_is_never_deduplicated(tmp_path):
    """Collapsing unidentifiable frames under one key would lose turns.
    Duplicating is recoverable on read; losing is not."""
    start(tmp_path)
    task = FakeTask()
    attach(task, journey_id=JID)

    # A frame class with a `text` but deliberately no `id`.
    unidentified = type("TranscriptionFrame", (), {"text": "hello"})
    push(task, unidentified(), hops=2)

    assert [m.content for m in messages()] == ["hello", "hello"]


def test_the_seen_set_stays_bounded(tmp_path):
    """A call that runs for hours must not grow the set without limit."""
    from odyssey.integrations.pipecat import _SEEN_LIMIT

    start(tmp_path)
    task = FakeTask()
    rec = attach(task, journey_id=JID)
    for i in range(_SEEN_LIMIT + 50):
        rec._first_sighting(frame("TranscriptionFrame", text=str(i)))

    assert len(rec._seen) <= _SEEN_LIMIT


# --------------------------------------------------------------------------
# One message per turn, never one per chunk
# --------------------------------------------------------------------------


def test_a_streamed_reply_becomes_one_turn(tmp_path):
    """A corpus of token fragments is not a corpus of turns."""
    start(tmp_path)
    task = FakeTask()
    attach(task, journey_id=JID)
    hear(task, "book me")
    reply(task, "Booked ", "for ", "Tuesday.")

    assert [(m.role, m.content) for m in messages()] == [
        ("user", "book me"),
        ("assistant", "Booked for Tuesday."),
    ]


def test_interim_transcripts_are_never_recorded(tmp_path):
    """`InterimTranscriptionFrame` fires per partial hypothesis while the
    caller is still speaking. Its final text is what `TranscriptionFrame`
    delivers, so consuming it would flood the spool with prefixes."""
    start(tmp_path)
    task = FakeTask()
    attach(task, journey_id=JID)
    push(task, frame("InterimTranscriptionFrame", text="I need"))
    push(task, frame("InterimTranscriptionFrame", text="I need an"))
    hear(task, "I need an appointment")

    assert [m.content for m in messages()] == ["I need an appointment"]


def test_an_unfinalized_transcript_is_not_a_turn(tmp_path):
    start(tmp_path)
    task = FakeTask()
    attach(task, journey_id=JID)
    hear(task, "maybe", finalized=False)
    hear(task, "definitely")

    assert [m.content for m in messages()] == ["definitely"]


def test_text_with_no_response_start_still_records(tmp_path):
    """A service that emits text without the boundary frame must not lose the
    reply."""
    start(tmp_path)
    task = FakeTask()
    attach(task, journey_id=JID)
    push(task, frame("LLMTextFrame", text="orphaned"))

    assert [(m.role, m.content) for m in messages()] == [("assistant", "orphaned")]


def test_a_second_generation_does_not_merge_into_the_first(tmp_path):
    start(tmp_path)
    task = FakeTask()
    attach(task, journey_id=JID)
    push(task, frame("LLMFullResponseStartFrame"))
    push(task, frame("LLMTextFrame", text="first"))
    push(task, frame("LLMFullResponseStartFrame"))
    push(task, frame("LLMTextFrame", text="second"))
    push(task, frame("LLMFullResponseEndFrame"))

    assert [m.content for m in messages()] == ["first", "second"]


def test_the_transcript_language_and_speaker_are_recorded(tmp_path):
    start(tmp_path)
    task = FakeTask()
    attach(task, journey_id=JID)
    hear(task, "namaste", language="hi-IN")

    (turn,) = messages()
    assert turn.metadata == {"user_id": "u_1", "language": "hi-IN"}


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


def test_a_tool_call_is_recorded_paired_with_its_result(tmp_path):
    """Emitted as a pair rather than on sight: a call with no outcome is not
    something a model should be trained to produce."""
    start(tmp_path)
    task = FakeTask()
    attach(task, journey_id=JID)
    push(
        task,
        frame(
            "FunctionCallInProgressFrame",
            function_name="check_slot",
            tool_call_id="call_1",
            arguments={"day": "tuesday"},
        ),
    )
    push(
        task,
        frame(
            "FunctionCallResultFrame",
            function_name="check_slot",
            tool_call_id="call_1",
            arguments={"day": "tuesday"},
            result={"available": True},
        ),
    )

    call, response = messages()
    assert call.tool_calls is not None
    assert call.tool_calls[0].name == "check_slot"
    assert call.tool_calls[0].arguments == {"day": "tuesday"}
    assert response.tool_response is not None
    assert response.tool_response.response == {"available": True}


def test_a_call_with_no_result_is_never_recorded(tmp_path):
    start(tmp_path)
    task = FakeTask()
    attach(task, journey_id=JID)
    push(
        task,
        frame(
            "FunctionCallInProgressFrame",
            function_name="check_slot",
            tool_call_id="call_1",
            arguments={},
        ),
    )

    assert messages() == []


def test_unreadable_arguments_cost_the_shape_not_the_call(tmp_path):
    """Raising would lose the call entirely; an empty dict records that it
    happened with no readable arguments, which is true."""
    start(tmp_path)
    task = FakeTask()
    attach(task, journey_id=JID)
    push(
        task,
        frame(
            "FunctionCallResultFrame",
            function_name="check_slot",
            tool_call_id="call_9",
            arguments="not-a-dict",
            result="ok",
        ),
    )

    call, _response = messages()
    assert call.tool_calls is not None
    assert call.tool_calls[0].arguments == {"_raw": "not-a-dict"}


# --------------------------------------------------------------------------
# Metrics — the latency budget Pipecat already measures
# --------------------------------------------------------------------------


def data(name, **kw):
    return type(name, (), {})() if not kw else _with(type(name, (), {})(), kw)


def _with(obj, kw):
    for k, v in kw.items():
        setattr(obj, k, v)
    return obj


def test_ttfb_becomes_a_latency_event_in_milliseconds(tmp_path):
    """Pipecat reports seconds; the schema's field is named `_ms`. The two
    differ by 1000 — a guess that is silently wrong, not loudly wrong."""
    start(tmp_path)
    task = FakeTask()
    attach(task, journey_id=JID)
    push(
        task,
        frame(
            "MetricsFrame",
            data=[
                data(
                    "TTFBMetricsData",
                    processor="OpenAILLMService",
                    model="gpt-4.1-mini",
                    value=0.214,
                )
            ],
        ),
    )

    (reading,) = [v for v in voices() if v.voice_kind == "latency"]
    assert reading.latency_ms == 214.0
    assert reading.metadata is not None
    assert reading.metadata["stage"] == "ttfb"
    assert reading.metadata["processor"] == "OpenAILLMService"
    assert reading.metadata["model"] == "gpt-4.1-mini"


def test_llm_ttfb_lands_on_the_assistant_turn(tmp_path):
    start(tmp_path)
    task = FakeTask()
    attach(task, journey_id=JID)
    push(
        task,
        frame(
            "MetricsFrame",
            data=[data("TTFBMetricsData", processor="OpenAILLMService", value=0.214)],
        ),
    )
    reply(task, "hi")

    assert [m.ttft_ms for m in messages()] == [214.0]


def test_tts_ttfb_never_lands_on_the_model_turn(tmp_path):
    """TTFB out of a TTS service measures a different thing. Attaching it to
    the model's turn would report speech synthesis as thinking time."""
    start(tmp_path)
    task = FakeTask()
    attach(task, journey_id=JID)
    push(
        task,
        frame(
            "MetricsFrame",
            data=[data("TTFBMetricsData", processor="CartesiaTTSService", value=0.089)],
        ),
    )
    reply(task, "hi")

    assert [m.ttft_ms for m in messages()] == [None]
    assert [v.latency_ms for v in voices() if v.voice_kind == "latency"] == [89.0]


def test_token_usage_lands_on_the_turn_not_on_a_latency_event(tmp_path):
    """Usage data carries counts, not a duration. A latency event with no
    latency in it is a row every consumer has to filter out."""
    start(tmp_path)
    task = FakeTask()
    attach(task, journey_id=JID)

    class Usage:
        prompt_tokens = 412
        completion_tokens = 37
        total_tokens = 449

    push(
        task,
        frame(
            "MetricsFrame",
            data=[
                data("LLMUsageMetricsData", processor="OpenAILLMService", value=Usage())
            ],
        ),
    )
    reply(task, "hi")

    (turn,) = messages()
    assert turn.usage == {
        "prompt_tokens": 412,
        "completion_tokens": 37,
        "total_tokens": 449,
    }
    assert [v for v in voices() if v.voice_kind == "latency"] == []


def test_a_metrics_type_this_build_has_never_seen_still_records(tmp_path):
    start(tmp_path)
    task = FakeTask()
    attach(task, journey_id=JID)
    push(
        task,
        frame(
            "MetricsFrame",
            data=[data("SmartTurnMetricsData", processor="TurnAnalyzer", value=0.031)],
        ),
    )

    (reading,) = [v for v in voices() if v.voice_kind == "latency"]
    assert reading.metadata is not None
    assert reading.metadata["stage"] == "smartturn"
    assert reading.latency_ms == 31.0


def test_a_negative_reading_is_dropped(tmp_path):
    start(tmp_path)
    task = FakeTask()
    attach(task, journey_id=JID)
    push(
        task,
        frame(
            "MetricsFrame", data=[data("TTFBMetricsData", processor="X", value=-1.0)]
        ),
    )

    assert [v for v in voices() if v.voice_kind == "latency"] == []


# --------------------------------------------------------------------------
# Barge-in
# --------------------------------------------------------------------------


def test_an_interrupted_reply_is_marked_and_flushed(tmp_path):
    """Training on a reply cut off in its third sentence teaches the model to
    stop mid-thought."""
    start(tmp_path)
    task = FakeTask()
    attach(task, journey_id=JID)
    push(task, frame("LLMFullResponseStartFrame"))
    push(task, frame("LLMTextFrame", text="Your appointment is on"))
    push(task, frame("StartInterruptionFrame"))

    (turn,) = messages()
    assert turn.metadata is not None
    assert turn.metadata["interrupted"] is True
    assert [v.voice_kind for v in voices()] == ["barge_in"]


def test_the_older_interruption_frame_name_also_works(tmp_path):
    """Both names are in the wild. Picking one silently captures nothing on
    the other."""
    start(tmp_path)
    task = FakeTask()
    attach(task, journey_id=JID)
    push(task, frame("LLMFullResponseStartFrame"))
    push(task, frame("LLMTextFrame", text="Your appointment"))
    push(task, frame("InterruptionFrame"))

    (turn,) = messages()
    assert turn.metadata is not None
    assert turn.metadata["interrupted"] is True


def test_an_interruption_with_nothing_in_flight_is_still_recorded(tmp_path):
    start(tmp_path)
    task = FakeTask()
    attach(task, journey_id=JID)
    push(task, frame("StartInterruptionFrame"))

    assert [v.voice_kind for v in voices()] == ["barge_in"]
    assert messages() == []


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


def test_an_end_frame_closes_the_journey(tmp_path):
    """Without a terminal event `fold()` cannot tell "still running" from
    "lost the tail", and refuses the journey forever."""
    start(tmp_path)
    task = FakeTask()
    attach(task, journey_id=JID)
    hear(task, "bye")
    push(task, frame("EndFrame"))

    terminals = [e.terminal for e in events() if e.kind == "terminal" and e.terminal]
    assert [t.termination_reason for t in terminals] == ["ENV_DONE"]


def test_a_cancel_frame_closes_the_journey_as_truncated(tmp_path):
    start(tmp_path)
    task = FakeTask()
    attach(task, journey_id=JID)
    push(task, frame("CancelFrame"))

    terminals = [e.terminal for e in events() if e.kind == "terminal" and e.terminal]
    assert [t.termination_reason for t in terminals] == ["TRUNCATION"]


def test_the_last_reply_is_flushed_by_close(tmp_path):
    """The bot's sign-off is the last turn of the call and nothing else will
    come along to flush it."""
    start(tmp_path)
    task = FakeTask()
    attach(task, journey_id=JID)
    push(task, frame("LLMFullResponseStartFrame"))
    push(task, frame("LLMTextFrame", text="Goodbye!"))
    push(task, frame("EndFrame"))

    assert [m.content for m in messages()] == ["Goodbye!"]


def test_closing_twice_writes_one_terminal(tmp_path):
    start(tmp_path)
    task = FakeTask()
    rec = attach(task, journey_id=JID)
    push(task, frame("EndFrame"))
    rec.close()

    assert len([e for e in events() if e.kind == "terminal"]) == 1


def test_frames_after_close_are_ignored(tmp_path):
    start(tmp_path)
    task = FakeTask()
    attach(task, journey_id=JID)
    push(task, frame("EndFrame"))
    hear(task, "still there?")

    assert messages() == []


def test_process_shutdown_closes_a_pipeline_that_was_killed(tmp_path):
    """A worker killed mid-call never pushes an `EndFrame`."""
    start(tmp_path)
    task = FakeTask()
    attach(task, journey_id=JID)
    hear(task, "hello")
    odyssey.shutdown()

    client = odyssey.init(spool_dir=tmp_path / "spool", out_dir=tmp_path / "out")
    terminals = [
        e.terminal
        for e in client.spool.read(JID)
        if e.kind == "terminal" and e.terminal
    ]
    assert [t.termination_reason for t in terminals] == ["STALE"]


# --------------------------------------------------------------------------
# Identity and safety
# --------------------------------------------------------------------------


def test_the_header_names_the_framework_and_the_agent(tmp_path):
    start(tmp_path)
    task = FakeTask()
    attach(task, journey_id=JID, agent_id="agent_7", tenant="acme")
    hear(task, "hello")

    head = header()
    assert head is not None
    assert head.framework == "pipecat"
    assert head.data_source == "pipecat"
    assert head.agent_id == "agent_7"
    assert head.journey_metadata == {"tenant": "acme"}


def test_a_broken_frame_never_reaches_the_pipeline(tmp_path):
    """An exception escaping an observer propagates into the pipeline's task
    group, where it can tear down the call."""
    start(tmp_path)
    task = FakeTask()
    attach(task, journey_id=JID)

    class Exploding(Frame):
        @property
        def text(self):
            raise RuntimeError("boom")

    f = Exploding()
    f.__class__ = type("TranscriptionFrame", (Exploding,), {})
    push(task, f)

    assert messages() == []
    assert odyssey.health()["stats"]["capture_errors"] >= 1


def test_the_deprecated_positional_observer_signature_still_works(tmp_path):
    """Pipecat replaced `on_push_frame(src, dst, frame, direction, ts)` with a
    single data object. Both are deployed."""
    start(tmp_path)
    task = FakeTask()
    attach(task, journey_id=JID)
    f = frame("TranscriptionFrame", text="old style")
    for obs in task.observers:
        asyncio.run(obs.on_push_frame("Src", "Dst", f, "DOWNSTREAM", 0))

    assert [m.content for m in messages()] == ["old style"]


def test_recording_is_a_no_op_when_init_was_never_called():
    """The pipeline must be unaffected either way."""
    task = FakeTask()
    with pytest.warns(RuntimeWarning):
        rec = _attach(task, journey_id=JID)
    push(task, frame("TranscriptionFrame", text="hello"))
    rec.close()
