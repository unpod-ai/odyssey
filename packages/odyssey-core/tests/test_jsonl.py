"""The JSONL wire contract: round-trip, versioning, truncation, bad lines."""

from __future__ import annotations

import json

import pytest

from odyssey.jsonl import (
    HEADER_KEY,
    MalformedHeaderError,
    SchemaVersionError,
    decode_event,
    encode_event,
    header_line,
    read_events,
    read_header,
    read_schema_version,
    write_events,
)
from odyssey.primitives import (
    SCHEMA_VERSION,
    JourneyEvent,
    JourneyHeader,
    Message,
    Reward,
    RewardComponent,
    Signal,
    Terminal,
    ToolCall,
    ToolResponse,
)

JID = "j_wire"


def rich_stream() -> list[JourneyEvent]:
    """Exercises every payload kind and every nested type."""
    return [
        JourneyEvent(
            journey_id=JID,
            seq=0,
            kind="message",
            event_id="e0",
            ts="2026-01-01T00:00:00+00:00",
            message=Message(role="system", content="be helpful"),
        ),
        JourneyEvent(
            journey_id=JID,
            seq=1,
            kind="message",
            event_id="e1",
            ts="2026-01-01T00:00:01+00:00",
            message=Message(
                role="assistant",
                content=None,
                tool_calls=[ToolCall(name="check", arguments={"a": 1}, id="c1")],
                usage={"prompt_tokens": 10, "completion_tokens": 4},
                finish_reason="tool_calls",
                reasoning="need to check",
                trainable_status="trainable",
            ),
            model_id="openai/gpt-4.1-mini",
        ),
        JourneyEvent(
            journey_id=JID,
            seq=2,
            kind="message",
            event_id="e2",
            ts="2026-01-01T00:00:02+00:00",
            message=Message(
                role="tool",
                tool_response=ToolResponse(
                    id="c1", name="check", arguments={"a": 1}, response={"ok": True}
                ),
            ),
        ),
        JourneyEvent(
            journey_id=JID,
            seq=3,
            kind="signal",
            event_id="e3",
            ts="2026-01-01T00:00:03+00:00",
            signal=Signal(
                signal="user_edit", target_seq=1, regen_order=2, edited_output="fixed"
            ),
        ),
        JourneyEvent(
            journey_id=JID,
            seq=4,
            kind="reward",
            event_id="e4",
            ts="2026-01-01T00:00:04+00:00",
            reward=Reward(
                aggregated_value=0.9,
                aggregation_method="weighted",
                components=[
                    RewardComponent(
                        name="task_success",
                        value=0.9,
                        scaled_value=0.9,
                        weight=2.0,
                        range=(0.0, 1.0),
                    )
                ],
            ),
        ),
        JourneyEvent(
            journey_id=JID,
            seq=5,
            kind="terminal",
            event_id="e5",
            ts="2026-01-01T00:00:05+00:00",
            terminal=Terminal(termination_reason="ENV_DONE"),
        ),
    ]


# --------------------------------------------------------------------------
# Round-trip
# --------------------------------------------------------------------------


def test_every_payload_kind_round_trips_exactly():
    for original in rich_stream():
        assert decode_event(json.loads(encode_event(original))) == original


def test_range_is_restored_as_a_tuple_not_a_list():
    ev = rich_stream()[4]
    back = decode_event(json.loads(encode_event(ev)))
    assert back.reward.components[0].range == (0.0, 1.0)
    assert isinstance(back.reward.components[0].range, tuple)


def test_file_round_trip(tmp_path):
    p = tmp_path / "j.jsonl"
    n = write_events(p, rich_stream())
    assert n == 6
    result = read_events(p)
    assert result.clean
    assert result.events == rich_stream()


def test_one_event_per_line(tmp_path):
    p = tmp_path / "j.jsonl"
    write_events(p, rich_stream())
    lines = p.read_text().splitlines()
    assert len(lines) == 7  # header + 6 events
    for line in lines[1:]:
        assert isinstance(json.loads(line), dict)


def test_append_does_not_rewrite_the_header(tmp_path):
    p = tmp_path / "j.jsonl"
    events = rich_stream()
    write_events(p, events[:2])
    write_events(p, events[2:], append=True)
    text = p.read_text()
    assert text.count(HEADER_KEY) == 1
    assert read_events(p).events == events


def test_none_valued_fields_are_omitted_from_the_line(tmp_path):
    line = encode_event(rich_stream()[0])
    obj = json.loads(line)
    assert "signal" not in obj and "terminal" not in obj and "model_id" not in obj


# --------------------------------------------------------------------------
# Versioning
# --------------------------------------------------------------------------


def test_schema_version_readable_without_parsing_events(tmp_path):
    p = tmp_path / "j.jsonl"
    write_events(p, rich_stream())
    assert read_schema_version(p) == SCHEMA_VERSION


def test_version_readable_even_when_every_event_is_garbage(tmp_path):
    p = tmp_path / "j.jsonl"
    p.write_text(header_line() + "\n" + "not json\n" * 5)
    assert read_schema_version(p) == SCHEMA_VERSION


def test_unknown_major_version_refuses_to_parse(tmp_path):
    p = tmp_path / "j.jsonl"
    p.write_text(json.dumps({HEADER_KEY: "9.0"}) + "\n")
    with pytest.raises(SchemaVersionError, match="Refusing to parse"):
        read_events(p)


def test_same_major_different_minor_is_accepted(tmp_path):
    p = tmp_path / "j.jsonl"
    major = SCHEMA_VERSION.split(".")[0]
    p.write_text(json.dumps({HEADER_KEY: f"{major}.99"}) + "\n")
    assert read_events(p).schema_version == f"{major}.99"


def test_missing_header_is_an_error(tmp_path):
    p = tmp_path / "j.jsonl"
    p.write_text(json.dumps({"journey_id": "x"}) + "\n")
    with pytest.raises(MalformedHeaderError, match="not an odyssey header"):
        read_events(p)


def test_empty_file_is_an_error(tmp_path):
    p = tmp_path / "j.jsonl"
    p.write_text("")
    with pytest.raises(MalformedHeaderError, match="no header"):
        read_events(p)


# --------------------------------------------------------------------------
# Truncation — a writer killed mid-append
# --------------------------------------------------------------------------


def test_truncated_final_line_keeps_every_complete_event(tmp_path):
    p = tmp_path / "j.jsonl"
    write_events(p, rich_stream())
    text = p.read_text()
    # Simulate SIGKILL mid-write: chop the last line in half, no trailing newline.
    lines = text.splitlines()
    partial = lines[-1][: len(lines[-1]) // 2]
    p.write_text("\n".join(lines[:-1]) + "\n" + partial)

    result = read_events(p)
    assert result.truncated_last_line is True
    assert result.rejected_count == 0  # truncation is not corruption
    assert len(result.events) == 5  # the other five survive
    assert [e.seq for e in result.events] == [0, 1, 2, 3, 4]


def test_a_complete_file_is_not_reported_as_truncated(tmp_path):
    p = tmp_path / "j.jsonl"
    write_events(p, rich_stream())
    assert read_events(p).truncated_last_line is False


def test_broken_line_that_is_not_last_is_a_rejection_not_truncation(tmp_path):
    p = tmp_path / "j.jsonl"
    write_events(p, rich_stream())
    lines = p.read_text().splitlines()
    lines[3] = '{"journey_id": "j_wire", "seq": '  # truncated but mid-file
    p.write_text("\n".join(lines) + "\n")
    result = read_events(p)
    assert result.truncated_last_line is False
    assert result.rejected_count == 1
    assert result.rejections[0].line_no == 4


# --------------------------------------------------------------------------
# Per-line rejection — one bad line never eats the file
# --------------------------------------------------------------------------


def test_one_malformed_line_does_not_eat_the_file(tmp_path):
    p = tmp_path / "j.jsonl"
    write_events(p, rich_stream())
    lines = p.read_text().splitlines()
    lines.insert(4, "}{ not json at all")
    p.write_text("\n".join(lines) + "\n")

    result = read_events(p)
    assert len(result.events) == 6  # all six real events still parsed
    assert result.rejected_count == 1
    assert result.rejections[0].line_no == 5
    assert "not valid JSON" in result.rejections[0].reason


def test_rejection_reports_line_numbers_for_multiple_bad_lines(tmp_path):
    p = tmp_path / "j.jsonl"
    p.write_text(
        header_line()
        + "\n"
        + "\n".join(["oops", json.dumps({"kind": "message"}), "[1,2,3]"])
        + "\n"
    )
    result = read_events(p)
    assert [r.line_no for r in result.rejections] == [2, 3, 4]
    assert result.events == []
    assert result.clean is False


def test_event_missing_required_field_is_rejected_with_its_reason(tmp_path):
    p = tmp_path / "j.jsonl"
    p.write_text(
        header_line() + "\n" + json.dumps({"journey_id": "j", "seq": 0}) + "\n"
    )
    result = read_events(p)
    assert result.rejected_count == 1
    assert "KeyError" in result.rejections[0].reason


def test_event_with_mismatched_payload_is_rejected(tmp_path):
    """kind/payload disagreement is caught by JourneyEvent validation."""
    p = tmp_path / "j.jsonl"
    bad = {"journey_id": "j", "seq": 0, "kind": "message", "event_id": "x"}
    p.write_text(header_line() + "\n" + json.dumps(bad) + "\n")
    result = read_events(p)
    assert result.rejected_count == 1
    assert "requires a 'message' payload" in result.rejections[0].reason


def test_blank_lines_are_skipped_silently(tmp_path):
    p = tmp_path / "j.jsonl"
    write_events(p, rich_stream())
    p.write_text(p.read_text() + "\n\n\n")
    result = read_events(p)
    assert result.clean and len(result.events) == 6


# --------------------------------------------------------------------------
# The v1.1 header: identity travels with the file
# --------------------------------------------------------------------------


HEADER = JourneyHeader(
    journey_id="j_hdr",
    data_source="livekit",
    trace_id="t_9",
    started_at="2026-01-01T00:00:00+00:00",
    journey_metadata={"tenant": "acme"},
)


def test_the_header_survives_a_round_trip(tmp_path):
    """The point of v1.1: a reader learns what the file is from the file.

    Before this, `data_source` had to be supplied by whoever happened to call the
    reader, so two callers could fold one file into two differently-labelled
    journeys and neither was wrong.
    """
    p = tmp_path / "j.jsonl"
    write_events(p, rich_stream(), header=HEADER)
    got = read_events(p).header
    assert got.journey_id == "j_hdr"
    assert got.data_source == "livekit"
    assert got.trace_id == "t_9"
    assert got.started_at == "2026-01-01T00:00:00+00:00"
    assert got.journey_metadata == {"tenant": "acme"}
    assert got.odyssey_schema_version == SCHEMA_VERSION


def test_read_header_parses_no_events(tmp_path):
    """A drain needs the identity, not a journey that may be thousands long."""
    p = tmp_path / "j.jsonl"
    write_events(p, rich_stream(), header=HEADER)
    assert read_header(p) == read_events(p).header


def test_read_header_rejects_a_major_it_cannot_parse(tmp_path):
    p = tmp_path / "j.jsonl"
    p.write_text('{"odyssey_schema_version":"9.0"}\n')
    with pytest.raises(SchemaVersionError):
        read_header(p)


def test_a_v1_0_file_still_reads_with_an_empty_header(tmp_path):
    """Back-compat, and the signal a caller needs.

    Every identity field None is exactly the condition that tells a caller it
    must supply `data_source` itself.
    """
    p = tmp_path / "j.jsonl"
    p.write_text(
        '{"odyssey_schema_version":"1.0"}\n'
        + "".join(encode_event(e) + "\n" for e in rich_stream())
    )
    result = read_events(p)
    assert result.clean and len(result.events) == 6
    assert result.header.odyssey_schema_version == "1.0"
    assert result.header.journey_id is None
    assert result.header.data_source is None


def test_unknown_header_keys_are_ignored_not_rejected(tmp_path):
    """A MINOR bump may add keys; refusing here would make every forward-
    compatible file unreadable to the build that predates it."""
    p = tmp_path / "j.jsonl"
    p.write_text(
        json.dumps({HEADER_KEY: SCHEMA_VERSION, "journey_id": "j", "future": 1})
        + "\n"
        + "".join(encode_event(e) + "\n" for e in rich_stream())
    )
    result = read_events(p)
    assert result.clean
    assert result.header.journey_id == "j"


def test_the_writer_decides_the_version_not_its_payload():
    """`header.odyssey_schema_version` is informational on the way out."""
    line = json.loads(header_line(header=JourneyHeader(journey_id="j")))
    assert line[HEADER_KEY] == SCHEMA_VERSION


def test_appending_does_not_write_a_second_header(tmp_path):
    """A resumed drain sends the tail into a file that already has one."""
    p = tmp_path / "j.jsonl"
    events = list(rich_stream())
    write_events(p, events[:2], header=HEADER)
    write_events(p, events[2:], append=True, header=HEADER)
    lines = p.read_text().splitlines()
    assert sum(1 for ln in lines if HEADER_KEY in ln) == 1
    assert read_events(p).header == HEADER
