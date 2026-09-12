"""Speech-to-speech capture: OpenAI/Azure Realtime and Gemini Live.

No SDK is installed and none is needed: the recorder reads events by attribute
or key, so the sequences here are the vendors' own event shapes replayed as
plain dicts, plus one pass in object form to prove both are read the same way.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

import odyssey
from odyssey.integrations import realtime
from odyssey.primitives import JourneyEvent, Message


@pytest.fixture(autouse=True)
def _clean():
    odyssey.shutdown()
    yield
    odyssey.shutdown()


def start(tmp_path, **kw):
    kw.setdefault("instrument", "none")
    return odyssey.init(
        spool_dir=tmp_path / "spool",
        out_dir=tmp_path / "out",
        drain_interval=None,
        **kw,
    )


def client() -> Any:
    c = odyssey.get_client()
    assert c is not None
    return c


def events(jid: str) -> List[JourneyEvent]:
    return client().spool.read(jid)


def turns(jid: str) -> List[JourneyEvent]:
    return [e for e in events(jid) if e.kind == "message" and e.message]


def msg(event: JourneyEvent) -> Message:
    assert event.message is not None
    return event.message


def meta(event: JourneyEvent) -> Dict[str, Any]:
    return (event.message.metadata if event.message else None) or {}


def roles(jid: str) -> List[str]:
    return [msg(e).role for e in turns(jid)]


def texts(jid: str) -> List[Any]:
    return [msg(e).content for e in turns(jid)]


def terminal(jid: str) -> Any:
    ends = [e.terminal for e in events(jid) if e.kind == "terminal"]
    return ends[-1] if ends else None


def voices(jid: str) -> List[Any]:
    return [e.voice for e in events(jid) if e.kind == "voice" and e.voice]


# --------------------------------------------------------------------------
# OpenAI and Azure Realtime
# --------------------------------------------------------------------------


def heard(text="what are your hours?"):
    return {
        "type": "conversation.item.input_audio_transcription.completed",
        "transcript": text,
    }


def said(text="9 to 5"):
    return [
        {"type": "response.created"},
        {"type": "response.audio_transcript.delta", "delta": text[:3]},
        {"type": "response.audio_transcript.delta", "delta": text[3:]},
        {"type": "response.audio_transcript.done", "transcript": text},
        {
            "type": "response.done",
            "response": {
                "status": "completed",
                "usage": {"input_tokens": 20, "output_tokens": 6, "total_tokens": 26},
            },
        },
    ]


def test_a_realtime_exchange_is_recorded_as_turns(tmp_path):
    start(tmp_path)
    rec = realtime.attach(journey_id="call_1", provider="openai")

    rec.event(
        {"type": "session.created", "session": {"instructions": "you book slots"}}
    )
    rec.event(heard())
    for event in said():
        rec.event(event)
    rec.close()

    assert roles("call_1") == ["system", "user", "assistant"]
    assert texts("call_1") == ["you book slots", "what are your hours?", "9 to 5"]
    answer = msg(turns("call_1")[-1])
    assert answer.usage == {"input_tokens": 20, "output_tokens": 6, "total_tokens": 26}
    assert answer.provider == "openai"
    assert answer.ttft_ms is not None and answer.latency_ms is not None
    assert answer.ttft_ms <= answer.latency_ms
    assert terminal("call_1").termination_reason == "ENV_DONE"


def test_the_whole_transcript_wins_over_the_fragments_it_arrived_in(tmp_path):
    """A vendor that sends deltas *and* a final transcript sent it twice."""
    start(tmp_path)
    rec = realtime.attach(journey_id="call_1")

    rec.event(heard())
    rec.event({"type": "response.created"})
    rec.event({"type": "response.audio_transcript.delta", "delta": "9 to"})
    rec.event({"type": "response.audio_transcript.done", "transcript": "9 to 5"})
    rec.event({"type": "response.done", "response": {"status": "completed"}})
    rec.close()

    assert texts("call_1") == ["what are your hours?", "9 to 5"]


def test_deltas_alone_still_make_one_turn(tmp_path):
    """Text-modality sessions send `response.text.delta` and no transcript."""
    start(tmp_path)
    rec = realtime.attach(journey_id="call_1")

    rec.event(heard())
    rec.event({"type": "response.created"})
    rec.event({"type": "response.text.delta", "delta": "9 "})
    rec.event({"type": "response.text.delta", "delta": "to 5"})
    rec.event({"type": "response.done", "response": {"status": "completed"}})
    rec.close()

    assert texts("call_1") == ["what are your hours?", "9 to 5"]


def test_a_barge_in_truncates_the_reply_it_cut_off(tmp_path):
    start(tmp_path)
    rec = realtime.attach(journey_id="call_1")

    rec.event(heard())
    rec.event({"type": "response.created"})
    rec.event({"type": "response.audio_transcript.delta", "delta": "we are open "})
    rec.event({"type": "input_audio_buffer.speech_started"})
    rec.event(heard("actually, tuesday?"))
    rec.event({"type": "response.created"})
    rec.event({"type": "response.audio_transcript.done", "transcript": "3pm is free"})
    rec.event({"type": "response.done", "response": {"status": "completed"}})
    rec.close()

    assert roles("call_1") == ["user", "assistant", "user", "assistant"]
    cut = turns("call_1")[1]
    assert msg(cut).content == "we are open "
    assert meta(cut)["interrupted"] is True
    assert "interrupted" not in meta(turns("call_1")[3])
    assert [v.voice_kind for v in voices("call_1")] == ["barge_in"]


def test_a_cancelled_response_is_an_interrupted_turn(tmp_path):
    """The server's own word for a reply that was cut off."""
    start(tmp_path)
    rec = realtime.attach(journey_id="call_1")

    rec.event(heard())
    rec.event({"type": "response.created"})
    rec.event({"type": "response.audio_transcript.delta", "delta": "we are op"})
    rec.event({"type": "response.done", "response": {"status": "cancelled"}})
    rec.close()

    assert meta(turns("call_1")[-1])["interrupted"] is True


def test_a_tool_call_and_the_result_the_app_sent_back(tmp_path):
    start(tmp_path)
    rec = realtime.attach(journey_id="call_1")

    rec.event(heard("anything tuesday?"))
    rec.event({"type": "response.created"})
    rec.event({"type": "response.audio_transcript.delta", "delta": "let me check"})
    rec.event(
        {
            "type": "response.function_call_arguments.done",
            "call_id": "call_7",
            "name": "check_slots",
            "arguments": '{"day": "tuesday"}',
        }
    )
    rec.event(
        {
            "type": "response.done",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_7",
                        "name": "check_slots",
                        "arguments": '{"day": "tuesday"}',
                    }
                ],
            },
        }
    )
    rec.tool_result("call_7", {"slots": ["3pm"]})
    rec.close()

    called = [e for e in turns("call_1") if msg(e).tool_calls]
    assert len(called) == 1, "the same call announced twice is recorded once"
    call = (msg(called[0]).tool_calls or [])[0]
    assert (call.name, call.arguments) == ("check_slots", {"day": "tuesday"})
    assert msg(called[0]).content == "let me check", "the speech before it, same turn"
    answered = [e for e in turns("call_1") if msg(e).role == "tool"]
    response = msg(answered[0]).tool_response
    assert response is not None
    assert response.name == "check_slots"
    assert response.response == '{"slots": ["3pm"]}'


def test_a_prompt_changed_mid_call_is_recorded_again(tmp_path):
    start(tmp_path)
    rec = realtime.attach(journey_id="call_1")

    rec.event(
        {"type": "session.created", "session": {"instructions": "you book slots"}}
    )
    rec.event(
        {"type": "session.updated", "session": {"instructions": "you book slots"}}
    )
    rec.event(
        {"type": "session.updated", "session": {"instructions": "you cancel them"}}
    )
    rec.close()

    assert texts("call_1") == ["you book slots", "you cancel them"]


def test_the_prompt_can_be_kept_out_of_the_journey(tmp_path):
    start(tmp_path)
    rec = realtime.attach(journey_id="call_1", record_instructions=False)

    rec.event({"type": "session.created", "session": {"instructions": "3000 words"}})
    rec.event(heard())
    rec.close()

    assert roles("call_1") == ["user"]


def test_a_session_error_is_counted_not_raised(tmp_path):
    start(tmp_path)
    rec = realtime.attach(journey_id="call_1")

    rec.event({"type": "error", "error": {"message": "session expired"}})
    rec.close()

    assert any(
        "session expired" in e for e in odyssey.health()["stats"]["recent_errors"]
    )


def test_events_are_read_the_same_as_objects_or_dicts(tmp_path):
    """Through the SDK an event is a pydantic object, through a bare websocket
    a dict."""
    start(tmp_path)
    rec = realtime.attach(journey_id="call_1")

    rec.event(
        SimpleNamespace(
            type="conversation.item.input_audio_transcription.completed",
            transcript="what are your hours?",
        )
    )
    rec.event(SimpleNamespace(type="response.created"))
    rec.event(
        SimpleNamespace(type="response.audio_transcript.done", transcript="9 to 5")
    )
    rec.event(
        SimpleNamespace(
            type="response.done",
            response=SimpleNamespace(status="completed", usage=None, output=[]),
        )
    )
    rec.close()

    assert texts("call_1") == ["what are your hours?", "9 to 5"]


# --------------------------------------------------------------------------
# Gemini Live
# --------------------------------------------------------------------------


def live(**content):
    """One `LiveServerMessage`: the fields it does not carry are still there
    and still `None`, which is what the real object does."""
    fields = {
        "input_transcription": None,
        "output_transcription": None,
        "model_turn": None,
        "interrupted": False,
        "turn_complete": False,
    }
    fields.update(content)
    return SimpleNamespace(server_content=SimpleNamespace(**fields), tool_call=None)


def test_a_gemini_live_turn_is_recorded(tmp_path):
    """Gemini streams both transcripts and ends the turn with a flag, with no
    discriminator on any of it."""
    start(tmp_path)
    rec = realtime.attach(journey_id="call_1", provider="gemini")

    rec.event(live(input_transcription=SimpleNamespace(text="what are your ")))
    rec.event(live(input_transcription=SimpleNamespace(text="hours?")))
    rec.event(live(output_transcription=SimpleNamespace(text="9 to ")))
    rec.event(live(output_transcription=SimpleNamespace(text="5")))
    rec.event(live(turn_complete=True))
    rec.close()

    assert roles("call_1") == ["user", "assistant"]
    assert texts("call_1") == ["what are your hours?", "9 to 5"]
    answer = msg(turns("call_1")[-1])
    assert answer.provider == "gemini"
    assert answer.ttft_ms is not None


def test_a_gemini_text_part_is_a_turn_too(tmp_path):
    start(tmp_path)
    rec = realtime.attach(journey_id="call_1")

    rec.event(live(input_transcription=SimpleNamespace(text="hours?")))
    rec.event(live(model_turn=SimpleNamespace(parts=[SimpleNamespace(text="9 to 5")])))
    rec.event(live(turn_complete=True))
    rec.close()

    assert texts("call_1") == ["hours?", "9 to 5"]


def test_a_gemini_interruption_truncates_the_reply(tmp_path):
    start(tmp_path)
    rec = realtime.attach(journey_id="call_1")

    rec.event(live(input_transcription=SimpleNamespace(text="hours?")))
    rec.event(live(output_transcription=SimpleNamespace(text="we are op")))
    rec.event(live(interrupted=True))
    rec.close()

    assert meta(turns("call_1")[-1])["interrupted"] is True


def test_a_gemini_tool_call_is_recorded(tmp_path):
    start(tmp_path)
    rec = realtime.attach(journey_id="call_1")

    rec.event(
        SimpleNamespace(
            server_content=None,
            tool_call=SimpleNamespace(
                function_calls=[
                    SimpleNamespace(
                        id="fc_1", name="check_slots", args={"day": "tuesday"}
                    )
                ]
            ),
        )
    )
    rec.tool_result("fc_1", "3pm free")
    rec.close()

    called = [e for e in turns("call_1") if msg(e).tool_calls]
    call = (msg(called[0]).tool_calls or [])[0]
    assert (call.name, call.arguments) == ("check_slots", {"day": "tuesday"})
    answered = msg(turns("call_1")[-1]).tool_response
    assert answered is not None and answered.response == "3pm free"


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


class Socket:
    """An async event stream, as the SDK's connection object is."""

    def __init__(self, items, *, fail=None):
        self._items = list(items)
        self._fail = fail

    async def __aiter__(self):
        for item in self._items:
            yield item
        if self._fail is not None:
            raise self._fail


def test_observe_records_the_loop_and_closes_the_journey(tmp_path):
    start(tmp_path)

    async def main():
        stream = realtime.observe(
            Socket([heard(), *said()]), journey_id="call_1", provider="openai"
        )
        seen = [event async for event in stream]
        return len(seen), stream.recorder

    count, rec = asyncio.run(main())
    assert count == 6, "every event still reaches the caller"
    assert texts("call_1") == ["what are your hours?", "9 to 5"]
    assert terminal("call_1").termination_reason == "ENV_DONE"
    assert rec.journey_id == "call_1"


def test_a_socket_that_fails_ends_the_journey_as_truncated(tmp_path):
    """The caller hung up mid-reply: the journey ends, and says how."""
    start(tmp_path)

    async def main():
        stream = realtime.observe(
            Socket([heard()], fail=ConnectionResetError("peer went away")),
            journey_id="call_1",
        )
        with pytest.raises(ConnectionResetError):
            async for _ in stream:
                pass

    asyncio.run(main())
    ended = terminal("call_1")
    assert ended.termination_reason == "TRUNCATION"
    assert "ConnectionResetError" in (ended.error or "")


def test_a_session_nothing_closed_is_ended_at_exit(tmp_path):
    """A dropped socket reaches no close, and a journey with no terminal event
    is refused by the fold forever."""
    start(tmp_path)
    rec = realtime.attach(journey_id="call_1")
    rec.event(heard())

    odyssey.shutdown()  # process exit; nothing ever closed the session

    written = odyssey.read_events(tmp_path / "out" / "call_1.jsonl").events
    assert written[-1].kind == "terminal"
    ended = written[-1].terminal
    assert ended is not None and ended.termination_reason == "STALE"


def test_the_turn_in_flight_is_written_when_the_call_ends(tmp_path):
    start(tmp_path)
    rec = realtime.attach(journey_id="call_1")

    rec.event(heard())
    rec.event({"type": "response.created"})
    rec.event({"type": "response.audio_transcript.delta", "delta": "9 to 5"})
    rec.close()

    assert texts("call_1") == ["what are your hours?", "9 to 5"]


def test_closing_twice_keeps_the_first_reason(tmp_path):
    start(tmp_path)
    rec = realtime.attach(journey_id="call_1")
    rec.close(reason="TRUNCATION", error="hung up")
    rec.close()

    assert terminal("call_1").termination_reason == "TRUNCATION"


def test_recording_without_init_is_a_no_op():
    """An app that forgot `init()` keeps working, minus the recording."""
    odyssey.shutdown()
    with pytest.warns(RuntimeWarning, match="recording nothing"):
        rec = realtime.attach(journey_id="call_1")
    rec.event(heard())
    rec.close()


def test_naming_realtime_in_instrument_explains_itself(tmp_path):
    """It attaches to a session the application owns, so "unknown target" would
    send someone looking for a typo that is not there."""
    start(tmp_path, instrument=["realtime"])

    errors = odyssey.health()["stats"]["recent_errors"]
    assert any("realtime.attach" in e for e in errors)
