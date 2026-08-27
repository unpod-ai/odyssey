"""SFT export: one JSON line per trainable turn."""

from __future__ import annotations

import json

from odyssey.fold import fold
from odyssey.jsonl import write_events
from odyssey.primitives import (
    JourneyEvent,
    JourneyHeader,
    Message,
    Signal,
    Terminal,
    ToolCall,
    ToolResponse,
)
from odyssey.sft import export_sft_dir, export_sft_spool, save_sft, sft_examples

JID = "j_sft"


def ev(seq, **kw):
    return JourneyEvent(journey_id=JID, seq=seq, event_id=f"e{seq}", **kw)


def msg(seq, role, content=None, **kw):
    return ev(seq, kind="message", message=Message(role=role, content=content, **kw))


def multi_turn_stream():
    return [
        msg(0, "system", "You book tee times."),
        msg(1, "user", "Book me for Tuesday at 3."),
        msg(2, "assistant", "Which course?"),
        msg(3, "user", "Qutub."),
        msg(
            4,
            "assistant",
            "Let me check.",
            tool_calls=[ToolCall(name="check", arguments={"day": "tue"}, id="c1")],
        ),
        msg(
            5,
            "tool",
            tool_response=ToolResponse(
                id="c1", name="check", arguments={"day": "tue"}, response={"ok": True}
            ),
        ),
        msg(6, "assistant", "Three PM is free. Book it?"),
        msg(7, "user", "Yes."),
        msg(8, "assistant", "Booked for Tuesday at 3pm."),
        ev(9, kind="terminal", terminal=Terminal(termination_reason="ENV_DONE")),
    ]


def test_one_example_per_trainable_step():
    result = fold(multi_turn_stream(), data_source="t")
    examples = sft_examples(result)
    # 3 user turns -> 3 trainable steps -> 3 examples.
    assert len(examples) == 3
    assert [ex["messages"][-1]["content"] for ex in examples] == [
        "Which course?",
        "Three PM is free. Book it?",
        "Booked for Tuesday at 3pm.",
    ]


def test_a_trainable_examples_messages_have_no_trainable_status_key():
    result = fold(multi_turn_stream(), data_source="t")
    example = sft_examples(result)[-1]
    assert all("trainable_status" not in m for m in example["messages"])


def test_tool_call_and_result_survive_in_the_example():
    result = fold(multi_turn_stream(), data_source="t")
    example = sft_examples(result)[1]  # the step ending on "Three PM is free..."
    roles = [m["role"] for m in example["messages"]]
    assert roles == [
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    call = next(m for m in example["messages"] if m.get("tool_calls"))
    resp = next(m for m in example["messages"] if m.get("tool_response"))
    assert call["tool_calls"][0]["id"] == resp["tool_response"]["id"] == "c1"


def test_conversation_id_and_step_index_are_stamped():
    result = fold(multi_turn_stream(), data_source="t", conversation_id="conv_1")
    examples = sft_examples(result)
    assert [ex["conversation_id"] for ex in examples] == ["conv_1"] * 3
    assert [ex["step_index"] for ex in examples] == [0, 1, 2]


# --------------------------------------------------------------------------
# What must NOT become an example
# --------------------------------------------------------------------------


def test_superseded_and_not_trainable_steps_are_excluded():
    events = [
        msg(0, "user", "book it"),
        msg(1, "assistant", "weak answer"),
        ev(2, kind="signal", signal=Signal(signal="regenerated", target_seq=1)),
        msg(3, "assistant", "strong answer"),
        ev(4, kind="terminal", terminal=Terminal(termination_reason="ENV_DONE")),
    ]
    result = fold(events, data_source="t")
    examples = sft_examples(result)
    assert len(examples) == 1
    assert examples[0]["messages"][-1]["content"] == "strong answer"


def test_a_thumbs_down_turn_is_excluded():
    events = [
        msg(0, "user", "q"),
        msg(1, "assistant", "bad answer"),
        ev(2, kind="signal", signal=Signal(signal="thumbs_down", target_seq=1)),
        ev(3, kind="terminal", terminal=Terminal(termination_reason="ENV_DONE")),
    ]
    result = fold(events, data_source="t")
    assert sft_examples(result) == []


def test_a_trailing_unanswered_user_turn_is_excluded():
    events = [
        msg(0, "user", "book it"),
        msg(1, "assistant", "done"),
        msg(2, "user", "thanks"),
        ev(3, kind="terminal", terminal=Terminal(termination_reason="ENV_DONE")),
    ]
    result = fold(events, data_source="t")
    examples = sft_examples(result)
    assert len(examples) == 1
    assert examples[0]["messages"][-1]["role"] == "assistant"


# --------------------------------------------------------------------------
# save_sft — the file
# --------------------------------------------------------------------------


def test_save_sft_writes_one_json_object_per_line(tmp_path):
    result = fold(multi_turn_stream(), data_source="t")
    out = tmp_path / "train.jsonl"
    r = save_sft([result], out)
    assert r.ok and r.written == 3

    lines = out.read_text().splitlines()
    assert len(lines) == 3
    for line in lines:
        obj = json.loads(line)
        assert "messages" in obj


def test_save_sft_is_atomic(tmp_path):
    result = fold(multi_turn_stream(), data_source="t")
    out = tmp_path / "train.jsonl"
    save_sft([result], out)
    assert list(tmp_path.glob("*.tmp")) == []


def test_save_sft_with_no_examples_writes_an_empty_file(tmp_path):
    events = [
        msg(0, "user", "q"),
        msg(1, "assistant", "bad"),
        ev(2, kind="signal", signal=Signal(signal="thumbs_down", target_seq=1)),
        ev(3, kind="terminal", terminal=Terminal(termination_reason="ENV_DONE")),
    ]
    result = fold(events, data_source="t")
    out = tmp_path / "train.jsonl"
    r = save_sft([result], out)
    assert r.written == 0
    assert out.read_text() == ""


def test_an_incomplete_journey_is_skipped_not_written(tmp_path):
    """No terminal event -> not trainable -> contributes nothing, but is named."""
    events = [msg(0, "user", "q"), msg(1, "assistant", "a")]
    result = fold(events, data_source="t")
    out = tmp_path / "train.jsonl"
    r = save_sft([result], out)
    assert r.written == 0
    assert JID in r.skipped_incomplete
    assert "may still be running" in r.skipped_incomplete[JID]


HEADER = JourneyHeader(journey_id=JID, data_source="livekit")


def test_export_sft_dir_reads_a_drained_directory(tmp_path):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    write_events(events_dir / f"{JID}.jsonl", multi_turn_stream(), header=HEADER)

    out = tmp_path / "train.jsonl"
    r = export_sft_dir(events_dir, out)
    assert r.ok and r.written == 3


def test_export_sft_spool_reads_straight_from_the_spool(tmp_path):
    from odyssey.spool import Spool, SpoolConfig

    spool = Spool(SpoolConfig(root=tmp_path / "spool"))
    spool.record_all(multi_turn_stream(), header=HEADER)
    spool.close()

    out = tmp_path / "train.jsonl"
    r = export_sft_spool(tmp_path / "spool", out)
    assert r.ok and r.written == 3
    # A view, not a consumption: no watermark moves.
    assert spool.watermark(JID) is None
