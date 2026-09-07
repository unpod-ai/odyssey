"""v2.1 agent attribution and the timing stamp.

Two rules under test, and both exist to keep the corpus small without losing
the answer:

- The header names the agent once. A turn carries ``agent_id`` only when the
  journey has handed off to a different one, so a reader takes the header value
  and overrides it wherever a message names one.
- ``stamp()`` fills timing in, but never over a value an integration that knew
  better already set.
"""

from __future__ import annotations

import dataclasses

import pytest

import odyssey
from odyssey.context import JourneyContext, SeqAllocator
from odyssey.integrations._timing import Timer, stamp
from odyssey.jsonl import decode_event, decode_header, encode_event, header_line
from odyssey.primitives import JourneyEvent, JourneyHeader, Message


@pytest.fixture(autouse=True)
def _clean():
    odyssey.shutdown()
    yield
    odyssey.shutdown()


def start(tmp_path, **kw):
    return odyssey.init(
        spool_dir=tmp_path / "spool",
        out_dir=tmp_path / "out",
        drain_interval=None,
        **kw,
    )


# --------------------------------------------------------------------------
# stamp()
# --------------------------------------------------------------------------


def test_stamp_fills_empty_timing_fields():
    stamped = stamp(
        Message(role="assistant", content="hi"),
        latency_ms=12.5,
        ttft_ms=3.5,
        provider="openai",
    )
    assert (stamped.latency_ms, stamped.ttft_ms, stamped.provider) == (
        12.5,
        3.5,
        "openai",
    )


def test_stamp_never_overwrites_what_an_integration_already_measured():
    """A streaming wrapper that timed its own chunks knows more than the
    surrounding call timer does."""
    original = Message(role="assistant", content="hi", ttft_ms=1.0, provider="livekit")
    stamped = stamp(original, latency_ms=99.0, ttft_ms=50.0, provider="openai")
    assert stamped.ttft_ms == 1.0
    assert stamped.provider == "livekit"
    assert stamped.latency_ms == 99.0


def test_stamp_with_nothing_to_add_returns_the_same_object():
    original = Message(role="user", content="q")
    assert stamp(original) is original


def test_the_timer_reports_no_first_token_until_one_is_marked():
    timer = Timer()
    assert timer.ttft_ms is None
    timer.first_token()
    first = timer.ttft_ms
    assert first is not None
    timer.first_token()
    assert timer.ttft_ms == first, "first_token() must be idempotent"


# --------------------------------------------------------------------------
# Header-then-delta attribution
# --------------------------------------------------------------------------


def test_the_header_carries_the_agent_and_the_turns_do_not(tmp_path):
    start(tmp_path)
    with odyssey.journey("j_solo") as j:
        ctx = odyssey.current()
        assert ctx is not None
        ctx.agent_id = "agent_a"
        ctx.agent_name = "BookingAgent"
        ctx.framework = "livekit"
        j.message(Message(role="assistant", content="hi"))

    client = odyssey.get_client()
    assert client is not None
    header = client.spool.header("j_solo")
    assert header is not None
    assert (header.agent_id, header.agent_name, header.framework) == (
        "agent_a",
        "BookingAgent",
        "livekit",
    )
    turns = [e.message for e in client.spool.read("j_solo") if e.message]
    assert [m.agent_id for m in turns] == [
        None
    ], "a single-agent journey repeats nothing the header already said"


def test_a_handoff_stamps_the_new_agent_on_every_turn_after_it(tmp_path):
    start(tmp_path)
    with odyssey.journey("j_handoff") as j:
        ctx = odyssey.current()
        assert ctx is not None
        ctx.agent_id = "agent_a"
        j.message(Message(role="assistant", content="from a"))
        ctx.agent_id = "agent_b"
        j.message(Message(role="assistant", content="from b"))
        j.message(Message(role="assistant", content="still b"))

    client = odyssey.get_client()
    assert client is not None
    turns = [e.message for e in client.spool.read("j_handoff") if e.message]
    assert [m.agent_id for m in turns] == [None, "agent_b", "agent_b"]


def test_a_caller_supplied_agent_id_is_never_overwritten(tmp_path):
    start(tmp_path)
    with odyssey.journey("j_explicit") as j:
        ctx = odyssey.current()
        assert ctx is not None
        ctx.agent_id = "agent_a"
        j.message(Message(role="assistant", content="hi", agent_id="agent_sub"))

    client = odyssey.get_client()
    assert client is not None
    (turn,) = [e.message for e in client.spool.read("j_explicit") if e.message]
    assert turn.agent_id == "agent_sub"


def test_agent_delta_is_empty_when_nothing_named_an_agent():
    ctx = JourneyContext(journey_id="j", allocator=SeqAllocator(lambda _jid: None))
    assert ctx.agent_delta() is None


# --------------------------------------------------------------------------
# Round trip
# --------------------------------------------------------------------------


def test_the_new_message_fields_survive_a_round_trip():
    event = JourneyEvent(
        journey_id="j",
        seq=0,
        kind="message",
        message=Message(
            role="assistant",
            content="hi",
            latency_ms=812.5,
            ttft_ms=214.0,
            agent_id="agent_a",
            provider="anthropic",
        ),
    )
    import json

    decoded = decode_event(json.loads(encode_event(event)))
    assert decoded.message == event.message


def test_the_new_header_fields_survive_a_round_trip():
    import json

    header = JourneyHeader(
        journey_id="j",
        agent_id="agent_a",
        agent_name="BookingAgent",
        framework="pipecat",
    )
    decoded = decode_header(json.loads(header_line(header=header)))
    assert (decoded.agent_id, decoded.agent_name, decoded.framework) == (
        "agent_a",
        "BookingAgent",
        "pipecat",
    )


def test_a_v2_0_shard_still_decodes_under_v2_1():
    """Additive means additive: none of the new keys are required."""
    import json

    line = json.dumps(
        {
            "journey_id": "j",
            "seq": 0,
            "kind": "message",
            "event_id": "e0",
            "ts": "2026-01-01T00:00:00+00:00",
            "message": {"role": "assistant", "content": "hi"},
        }
    )
    decoded = decode_event(json.loads(line))
    assert decoded.message is not None
    assert decoded.message.latency_ms is None
    assert decoded.message.agent_id is None
    assert decoded.message.provider is None


def test_an_unusable_timing_value_costs_the_field_not_the_turn():
    """A bad number must not take a real turn out of the corpus."""
    import json

    line = json.dumps(
        {
            "journey_id": "j",
            "seq": 0,
            "kind": "message",
            "event_id": "e0",
            "ts": "2026-01-01T00:00:00+00:00",
            "message": {
                "role": "assistant",
                "content": "hi",
                "latency_ms": "not-a-number",
            },
        }
    )
    decoded = decode_event(json.loads(line))
    assert decoded.message is not None
    assert decoded.message.content == "hi"
    assert decoded.message.latency_ms is None


def test_a_message_with_no_new_fields_encodes_no_new_keys():
    """The additions must cost nothing on a line that does not use them."""
    import json

    event = JourneyEvent(
        journey_id="j",
        seq=0,
        kind="message",
        message=Message(role="user", content="q"),
    )
    encoded = json.loads(encode_event(event))
    assert set(encoded["message"]) == {"role", "content"}


def test_dataclass_replace_still_works_on_the_widened_message():
    """The wrappers stamp via `dataclasses.replace`; a required field added by
    mistake would break every one of them at once."""
    replaced = dataclasses.replace(Message(role="user"), content="q")
    assert replaced.content == "q"
