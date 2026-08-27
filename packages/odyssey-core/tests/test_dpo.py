"""DPO pair extraction: (prompt, chosen, rejected) from a folded journey."""

from __future__ import annotations

import json
from pathlib import Path

from odyssey.dpo import dpo_pairs, export_dpo_dir, export_dpo_spool, save_dpo
from odyssey.fold import fold
from odyssey.jsonl import read_events, write_events
from odyssey.primitives import JourneyEvent, JourneyHeader, Message, Signal, Terminal

JID = "j_dpo"
GOLDEN = Path(__file__).resolve().parent / "fixtures" / "golden_journey.jsonl"


def ev(seq, **kw):
    return JourneyEvent(journey_id=JID, seq=seq, event_id=f"e{seq}", **kw)


def msg(seq, role, content=None, **kw):
    return ev(seq, kind="message", message=Message(role=role, content=content, **kw))


# --------------------------------------------------------------------------
# The golden fixture: regenerated -> user_edit -> thumbs_up on the same prompt
# --------------------------------------------------------------------------


def test_the_golden_fixture_yields_two_dpo_pairs():
    """The fixture's own chain (see test_contract.py): "Booked!" is
    regenerated away, "Booked for Tuesday at 3pm." is edited away, and
    "You're all set for Tuesday at 3pm." is the accepted, thumbs-upped
    answer. All three share one prompt prefix, so that is two pairs against
    one chosen answer, not one.
    """
    events = read_events(GOLDEN).events
    result = fold(events, data_source="golden")
    pairs = dpo_pairs(result)

    assert len(pairs) == 2
    rejected_texts = sorted(p["rejected"]["content"] for p in pairs)
    assert rejected_texts == ["Booked for Tuesday at 3pm.", "Booked!"]
    assert all(
        p["chosen"]["content"] == "You're all set for Tuesday at 3pm." for p in pairs
    )


def test_the_prompt_is_the_shared_prefix_before_the_golden_fixtures_answer():
    events = read_events(GOLDEN).events
    result = fold(events, data_source="golden")
    pair = dpo_pairs(result)[0]
    roles = [m["role"] for m in pair["prompt"]]
    assert roles == ["system", "user", "assistant", "tool"]


# --------------------------------------------------------------------------
# Smaller, hand-built scenarios
# --------------------------------------------------------------------------


def test_a_simple_regeneration_produces_one_pair():
    events = [
        msg(0, "user", "book it"),
        msg(1, "assistant", "weak answer"),
        ev(2, kind="signal", signal=Signal(signal="regenerated", target_seq=1)),
        msg(3, "assistant", "strong answer"),
        ev(4, kind="terminal", terminal=Terminal(termination_reason="ENV_DONE")),
    ]
    result = fold(events, data_source="t")
    pairs = dpo_pairs(result)
    assert len(pairs) == 1
    assert pairs[0]["chosen"]["content"] == "strong answer"
    assert pairs[0]["rejected"]["content"] == "weak answer"
    assert [m["role"] for m in pairs[0]["prompt"]] == ["user"]


def test_three_candidates_at_one_decision_point_yield_two_pairs():
    events = [
        msg(0, "user", "book it"),
        msg(1, "assistant", "candidate A"),
        ev(
            2,
            kind="signal",
            signal=Signal(signal="regenerated", target_seq=1, regen_order=0),
        ),
        msg(3, "assistant", "candidate B"),
        ev(
            4,
            kind="signal",
            signal=Signal(signal="regenerated", target_seq=3, regen_order=1),
        ),
        msg(5, "assistant", "candidate C — the winner"),
        ev(6, kind="signal", signal=Signal(signal="thumbs_up", target_seq=5)),
        ev(7, kind="terminal", terminal=Terminal(termination_reason="ENV_DONE")),
    ]
    result = fold(events, data_source="t")
    pairs = dpo_pairs(result)
    assert len(pairs) == 2
    assert all(p["chosen"]["content"] == "candidate C — the winner" for p in pairs)
    assert sorted(p["rejected"]["content"] for p in pairs) == [
        "candidate A",
        "candidate B",
    ]


def test_a_lone_thumbs_down_produces_no_pair():
    """Nothing to prefer it over -- not the same shape as a regeneration."""
    events = [
        msg(0, "user", "q"),
        msg(1, "assistant", "bad answer"),
        ev(2, kind="signal", signal=Signal(signal="thumbs_down", target_seq=1)),
        ev(3, kind="terminal", terminal=Terminal(termination_reason="ENV_DONE")),
    ]
    result = fold(events, data_source="t")
    assert dpo_pairs(result) == []


def test_a_regeneration_nobody_ever_accepted_produces_no_pair():
    """The regenerated answer never got a trainable replacement in this stream."""
    events = [
        msg(0, "user", "q"),
        msg(1, "assistant", "replaced"),
        ev(2, kind="signal", signal=Signal(signal="regenerated", target_seq=1)),
        ev(3, kind="terminal", terminal=Terminal(termination_reason="ENV_DONE")),
    ]
    result = fold(events, data_source="t")
    assert dpo_pairs(result) == []


def test_two_independent_decision_points_do_not_cross_pair():
    """A regeneration on turn 1 must not pair against turn 2's answer."""
    events = [
        msg(0, "user", "first question"),
        msg(1, "assistant", "weak first answer"),
        ev(2, kind="signal", signal=Signal(signal="regenerated", target_seq=1)),
        msg(3, "assistant", "strong first answer"),
        msg(4, "user", "second question"),
        msg(5, "assistant", "second answer"),
        ev(6, kind="terminal", terminal=Terminal(termination_reason="ENV_DONE")),
    ]
    result = fold(events, data_source="t")
    pairs = dpo_pairs(result)
    assert len(pairs) == 1
    assert pairs[0]["chosen"]["content"] == "strong first answer"
    assert pairs[0]["rejected"]["content"] == "weak first answer"


def test_conversation_id_is_stamped():
    events = [
        msg(0, "user", "q"),
        msg(1, "assistant", "a1"),
        ev(2, kind="signal", signal=Signal(signal="regenerated", target_seq=1)),
        msg(3, "assistant", "a2"),
        ev(4, kind="terminal", terminal=Terminal(termination_reason="ENV_DONE")),
    ]
    result = fold(events, data_source="t", conversation_id="conv_dpo_1")
    assert dpo_pairs(result)[0]["conversation_id"] == "conv_dpo_1"


def test_pair_messages_have_no_trainable_status_key():
    events = [
        msg(0, "user", "q"),
        msg(1, "assistant", "a1"),
        ev(2, kind="signal", signal=Signal(signal="regenerated", target_seq=1)),
        msg(3, "assistant", "a2"),
        ev(4, kind="terminal", terminal=Terminal(termination_reason="ENV_DONE")),
    ]
    result = fold(events, data_source="t")
    pair = dpo_pairs(result)[0]
    assert "trainable_status" not in pair["chosen"]
    assert "trainable_status" not in pair["rejected"]
    assert all("trainable_status" not in m for m in pair["prompt"])


# --------------------------------------------------------------------------
# save_dpo — the file
# --------------------------------------------------------------------------


def test_save_dpo_writes_one_json_object_per_line(tmp_path):
    events = [
        msg(0, "user", "q"),
        msg(1, "assistant", "a1"),
        ev(2, kind="signal", signal=Signal(signal="regenerated", target_seq=1)),
        msg(3, "assistant", "a2"),
        ev(4, kind="terminal", terminal=Terminal(termination_reason="ENV_DONE")),
    ]
    result = fold(events, data_source="t")
    out = tmp_path / "prefs.jsonl"
    r = save_dpo([result], out)
    assert r.ok and r.written == 1

    lines = out.read_text().splitlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert {"prompt", "chosen", "rejected"} <= set(obj)


def test_save_dpo_is_atomic(tmp_path):
    events = [
        msg(0, "user", "q"),
        msg(1, "assistant", "a1"),
        ev(2, kind="signal", signal=Signal(signal="regenerated", target_seq=1)),
        msg(3, "assistant", "a2"),
        ev(4, kind="terminal", terminal=Terminal(termination_reason="ENV_DONE")),
    ]
    result = fold(events, data_source="t")
    save_dpo([result], tmp_path / "prefs.jsonl")
    assert list(tmp_path.glob("*.tmp")) == []


def test_a_journey_with_no_pairs_writes_an_empty_file(tmp_path):
    events = [
        msg(0, "user", "q"),
        msg(1, "assistant", "a"),
        ev(2, kind="terminal", terminal=Terminal(termination_reason="ENV_DONE")),
    ]
    result = fold(events, data_source="t")
    out = tmp_path / "prefs.jsonl"
    r = save_dpo([result], out)
    assert r.written == 0
    assert out.read_text() == ""


def test_an_incomplete_journey_is_skipped_not_written(tmp_path):
    events = [
        msg(0, "user", "q"),
        msg(1, "assistant", "a1"),
        ev(2, kind="signal", signal=Signal(signal="regenerated", target_seq=1)),
        msg(3, "assistant", "a2"),
        # no terminal event
    ]
    result = fold(events, data_source="t")
    out = tmp_path / "prefs.jsonl"
    r = save_dpo([result], out)
    assert r.written == 0
    assert JID in r.skipped_incomplete


HEADER = JourneyHeader(journey_id=JID, data_source="livekit")


def _regen_stream():
    return [
        msg(0, "user", "book it"),
        msg(1, "assistant", "weak answer"),
        ev(2, kind="signal", signal=Signal(signal="regenerated", target_seq=1)),
        msg(3, "assistant", "strong answer"),
        ev(4, kind="terminal", terminal=Terminal(termination_reason="ENV_DONE")),
    ]


def test_export_dpo_dir_reads_a_drained_directory(tmp_path):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    write_events(events_dir / f"{JID}.jsonl", _regen_stream(), header=HEADER)

    out = tmp_path / "prefs.jsonl"
    r = export_dpo_dir(events_dir, out)
    assert r.ok and r.written == 1


def test_export_dpo_spool_reads_straight_from_the_spool(tmp_path):
    from odyssey.spool import Spool, SpoolConfig

    spool = Spool(SpoolConfig(root=tmp_path / "spool"))
    spool.record_all(_regen_stream(), header=HEADER)
    spool.close()

    out = tmp_path / "prefs.jsonl"
    r = export_dpo_spool(tmp_path / "spool", out)
    assert r.ok and r.written == 1
    assert spool.watermark(JID) is None
