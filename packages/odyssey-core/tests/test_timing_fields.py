"""v2.1 timing fields, and where agent identity lives.

- ``stamp()`` fills timing in, but never over a value an integration that knew
  better already set.
- Agent identity is a caller tag, not a schema field: what an agent id means
  differs per deployment, so it rides in ``journey_metadata`` and a handoff
  that retags it is an ordinary per-event delta.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

import odyssey
from odyssey.integrations._timing import Timer, stamp
from odyssey.jsonl import decode_event, decode_header, encode_event, header_line
from odyssey.primitives import JourneyEvent, JourneyHeader, Message


@pytest.fixture(autouse=True)
def _clean():
    odyssey.shutdown()
    yield
    odyssey.shutdown()


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
# Agent identity is a caller tag
# --------------------------------------------------------------------------


def test_agent_identity_is_not_a_schema_field():
    """An agent id is a config row to one deployment and a class name to
    another, so the schema does not claim to know what it is."""
    assert "agent_id" not in {f.name for f in dataclasses.fields(Message)}
    header_fields = {f.name for f in dataclasses.fields(JourneyHeader)}
    assert not {"agent_id", "agent_name"} & header_fields


def test_a_retagged_agent_rides_on_the_turns_after_it(tmp_path):
    """The handoff case needs no field of its own: the header snapshots the
    tag, and a later change is a delta on exactly the events after it."""
    odyssey.init(
        spool_dir=tmp_path / "spool",
        out_dir=tmp_path / "out",
        drain_interval=None,
        instrument="none",
    )
    with odyssey.journey("j_handoff", agent_id="booking_v3") as j:
        j.message(Message(role="assistant", content="from booking"))
        ctx = odyssey.current()
        assert ctx is not None
        ctx.metadata["agent_id"] = "payments_v1"
        j.message(Message(role="assistant", content="from payments"))

    client = odyssey.get_client()
    assert client is not None
    header = client.spool.header("j_handoff")
    assert header is not None
    assert (header.journey_metadata or {})["agent_id"] == "booking_v3"
    turns = [e for e in client.spool.read("j_handoff") if e.kind == "message"]
    assert [(e.metadata or {}).get("agent_id") for e in turns] == [
        None,
        "payments_v1",
    ]


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
            provider="anthropic",
        ),
    )
    decoded = decode_event(json.loads(encode_event(event)))
    assert decoded.message == event.message


def test_the_new_header_field_survives_a_round_trip():
    header = JourneyHeader(journey_id="j", framework="pipecat")
    decoded = decode_header(json.loads(header_line(header=header)))
    assert decoded.framework == "pipecat"


def test_a_v2_0_shard_still_decodes_under_v2_1():
    """Additive means additive: none of the new keys are required."""
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
    assert decoded.message.provider is None


def test_an_unusable_timing_value_costs_the_field_not_the_turn():
    """A bad number must not take a real turn out of the corpus."""
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
