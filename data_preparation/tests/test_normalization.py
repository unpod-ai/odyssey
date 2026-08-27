"""Normalization: raw traces -> canonical Journey artifacts."""

from __future__ import annotations

import json

import pytest
from odyssey.jsonl import write_events
from odyssey.primitives import JourneyEvent, JourneyHeader, Message, Terminal

from odyssey_dataprep.normalization import (
    NormalizeResult,
    normalize_byod_dir,
    normalize_odyssey_dir,
    normalize_odyssey_spool,
)

JID = "j_norm"
HEADER = JourneyHeader(journey_id=JID, data_source="livekit")


def ev(seq, **kw):
    return JourneyEvent(journey_id=JID, seq=seq, event_id=f"e{seq}", **kw)


def msg(seq, role, content=None, **kw):
    return ev(seq, kind="message", message=Message(role=role, content=content, **kw))


def odyssey_stream():
    return [
        msg(0, "system", "You book tee times."),
        msg(1, "user", "Book me for Tuesday at 3."),
        msg(2, "assistant", "Booked for Tuesday at 3pm."),
        ev(3, kind="terminal", terminal=Terminal(termination_reason="ENV_DONE")),
    ]


# --------------------------------------------------------------------------
# odyssey-shaped input — a thin wrapper over export_dir
# --------------------------------------------------------------------------


def test_normalize_odyssey_dir_writes_a_canonical_artifact(tmp_path):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    write_events(events_dir / f"{JID}.jsonl", odyssey_stream(), header=HEADER)

    result = normalize_odyssey_dir(events_dir, tmp_path / "normalized")
    assert isinstance(result, NormalizeResult)
    assert result.ok and result.count == 1

    doc = json.loads(result.written[0].read_text())
    assert doc["task"]["conversation_id"] == JID
    assert doc["task"]["data_source"] == "livekit"
    assert len(doc["steps"]) == 1


def test_normalize_odyssey_dir_flags_incomplete_journeys(tmp_path):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    write_events(
        events_dir / f"{JID}.jsonl",
        [e for e in odyssey_stream() if e.kind != "terminal"],
        header=HEADER,
    )

    result = normalize_odyssey_dir(events_dir, tmp_path / "normalized")
    assert result.count == 1
    assert JID in result.incomplete
    assert "no terminal event" in result.incomplete[JID]


def test_normalize_odyssey_dir_survives_one_bad_shard(tmp_path):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    write_events(events_dir / f"{JID}.jsonl", odyssey_stream(), header=HEADER)
    (events_dir / "broken.jsonl").write_text("not a header\n")

    result = normalize_odyssey_dir(events_dir, tmp_path / "normalized")
    assert result.count == 1
    assert not result.ok
    assert any("broken.jsonl" in e for e in result.errors)


def test_normalize_odyssey_spool_reads_straight_from_the_spool(tmp_path):
    from odyssey.spool import Spool, SpoolConfig

    spool = Spool(SpoolConfig(root=tmp_path / "spool"))
    spool.record_all(odyssey_stream(), header=HEADER)
    spool.close()

    result = normalize_odyssey_spool(tmp_path / "spool", tmp_path / "normalized")
    assert result.ok and result.count == 1
    # A view, not a consumption: no watermark moves.
    assert spool.watermark(JID) is None


# --------------------------------------------------------------------------
# BYOD-shaped input — parse + build + save, dispatched by format name
# --------------------------------------------------------------------------


def test_normalize_byod_dir_openai_chat_bare_array(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "conv_1.json").write_text(
        json.dumps(
            [
                {"role": "system", "content": "be helpful"},
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
            ]
        )
    )

    result = normalize_byod_dir(
        raw, tmp_path / "normalized", format="openai_chat", data_source="customer_a"
    )
    assert result.ok and result.count == 1

    doc = json.loads(result.written[0].read_text())
    assert doc["task"]["conversation_id"] == "conv_1"
    assert doc["task"]["data_source"] == "customer_a"
    assert doc["steps"][-1]["messages"][-1]["content"] == "hello"


def test_normalize_byod_dir_wrapped_object_with_metadata(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "ignored_filename.json").write_text(
        json.dumps(
            {
                "conversation_id": "explicit_id",
                "trace_id": "trace_xyz",
                "task_metadata": {"num_turns": 1},
                "messages": [
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "hello"},
                ],
            }
        )
    )

    result = normalize_byod_dir(
        raw, tmp_path / "normalized", format="openai_chat", data_source="customer_a"
    )
    assert result.count == 1
    doc = json.loads(result.written[0].read_text())
    assert doc["task"]["conversation_id"] == "explicit_id"
    assert doc["trace_id"] == "trace_xyz"
    assert doc["task"]["num_turns"] == 1


def test_normalize_byod_dir_labels_the_assistant_turn_trainable(tmp_path):
    """build_journey_from_messages runs no fold() -- without this, every
    message would keep the dataclass default (not_trainable), including the
    assistant's own reply, making trainable_status useless downstream."""
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "conv_1.json").write_text(
        json.dumps(
            [
                {"role": "system", "content": "be helpful"},
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
            ]
        )
    )

    result = normalize_byod_dir(
        raw, tmp_path / "normalized", format="openai_chat", data_source="customer_a"
    )
    doc = json.loads(result.written[0].read_text())
    by_role = {m["role"]: m["trainable_status"] for m in doc["steps"][-1]["messages"]}
    assert by_role["assistant"] == "trainable"
    assert by_role["user"] == "not_trainable"
    assert by_role["system"] == "not_trainable"


def test_normalize_byod_dir_anthropic_messages(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "conv_1.json").write_text(
        json.dumps(
            [
                {"role": "user", "content": [{"type": "text", "text": "hi"}]},
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hello there"}],
                },
            ]
        )
    )

    result = normalize_byod_dir(
        raw,
        tmp_path / "normalized",
        format="anthropic_messages",
        data_source="customer_b",
    )
    assert result.ok and result.count == 1
    doc = json.loads(result.written[0].read_text())
    assert doc["steps"][-1]["messages"][-1]["content"] == "hello there"


def test_normalize_byod_dir_vercel_ai_sdk(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "conv_1.json").write_text(
        json.dumps(
            [
                {"role": "user", "content": "ping"},
                {"role": "assistant", "content": "pong"},
            ]
        )
    )

    result = normalize_byod_dir(
        raw, tmp_path / "normalized", format="vercel_ai_sdk", data_source="customer_c"
    )
    assert result.ok and result.count == 1


def test_normalize_byod_dir_rejects_an_unknown_format(tmp_path):
    with pytest.raises(ValueError, match="unknown format"):
        normalize_byod_dir(
            tmp_path, tmp_path, format="not_a_real_format", data_source="x"
        )


def test_normalize_byod_dir_reports_a_malformed_file_without_aborting(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "good.json").write_text(
        json.dumps(
            [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hey"}]
        )
    )
    (raw / "bad.json").write_text(json.dumps({"not_messages": True}))

    result = normalize_byod_dir(
        raw, tmp_path / "normalized", format="openai_chat", data_source="customer_a"
    )
    assert result.count == 1
    assert not result.ok
    assert any("bad.json" in e for e in result.errors)


def test_normalize_byod_dir_filename_becomes_conversation_id_by_default(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "room_42.json").write_text(json.dumps([{"role": "user", "content": "hi"}]))

    result = normalize_byod_dir(
        raw, tmp_path / "normalized", format="openai_chat", data_source="customer_a"
    )
    doc = json.loads(result.written[0].read_text())
    assert doc["task"]["conversation_id"] == "room_42"


def test_normalize_byod_dir_write_is_atomic(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "conv_1.json").write_text(json.dumps([{"role": "user", "content": "hi"}]))
    out = tmp_path / "normalized"
    normalize_byod_dir(raw, out, format="openai_chat", data_source="x")
    assert list(out.glob("*.tmp")) == []
