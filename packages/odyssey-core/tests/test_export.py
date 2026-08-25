"""The artifact, not the transport.

`push` writes events; these tests cover what turns those events into the
`{conversation_id}.json` a trainer or the Trajectory platform consumes. The two
shapes are deliberately different — see `test_contract.py` for the rule that
`Step` never reaches the wire — so the export hop is where cumulative state is
allowed to exist, and where losing a field is silent unless something checks.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

import odyssey
from odyssey.export import (
    DIAGNOSTICS_KEY,
    ExportError,
    export_dir,
    fold_shard,
    journey_to_dict,
    save,
)
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

JID = "call_export_1"


def ev(seq, **kw):
    return JourneyEvent(journey_id=JID, seq=seq, event_id=f"e{seq}", **kw)


def msg(seq, role, content=None, **kw):
    return ev(seq, kind="message", message=Message(role=role, content=content, **kw))


def stream():
    """A conversation with every shape the artifact has to carry."""
    return [
        msg(0, "system", "You book tee times."),
        msg(1, "user", "Book me for Tuesday at 3."),
        msg(
            2,
            "assistant",
            "Let me check.",
            tool_calls=[ToolCall(name="check", arguments={"day": "tue"}, id="c1")],
        ),
        msg(
            3,
            "tool",
            tool_response=ToolResponse(
                id="c1", name="check", arguments={"day": "tue"}, response={"ok": True}
            ),
        ),
        msg(4, "assistant", "Booked for Tuesday at 3pm."),
        ev(5, kind="signal", signal=Signal(signal="thumbs_up", target_seq=4)),
        ev(6, kind="terminal", terminal=Terminal(termination_reason="ENV_DONE")),
    ]


HEADER = JourneyHeader(
    journey_id=JID,
    data_source="livekit",
    trace_id="t_9",
    started_at="2026-01-01T00:00:00+00:00",
    journey_metadata={"agent_id": "agent_7", "handler": "LiteV2Handler"},
)


@pytest.fixture
def shard(tmp_path):
    p = tmp_path / "events" / f"{JID}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    write_events(p, stream(), header=HEADER)
    return p


# --------------------------------------------------------------------------
# The shape the platform declares
# --------------------------------------------------------------------------


def test_the_artifact_is_a_trajectory_not_an_event_stream(tmp_path, shard):
    """One JSON object per conversation, with `steps` at the top level.

    This is the whole point of the module: handing a consumer the event stream
    and calling it the deliverable is what this closes.
    """
    save([fold_shard(shard)], tmp_path / "exports")
    doc = json.loads((tmp_path / "exports" / f"{JID}.json").read_text())

    assert set(doc) >= {"task", "steps", "metrics", "execution_metrics"}
    assert isinstance(doc["steps"], list)
    assert doc["task"]["conversation_id"] == JID
    assert doc["task"]["data_source"] == "livekit"
    assert doc["task"]["id"] == f"livekit:{JID}"


def test_steps_are_cumulative_in_the_artifact(tmp_path, shard):
    """The opposite of the wire rule, and correct here: each step is a
    self-contained training example, so it carries everything before it."""
    save([fold_shard(shard)], tmp_path / "exports")
    doc = json.loads((tmp_path / "exports" / f"{JID}.json").read_text())

    counts = [len(s["messages"]) for s in doc["steps"]]
    assert counts == sorted(counts)
    assert counts[-1] == 5  # system + user + tool-call + tool + answer
    assert doc["steps"][-1]["messages"][0]["role"] == "system"


def test_every_message_carries_its_trainable_status(tmp_path, shard):
    """Derived at fold time, and the field a trainer filters on. Absent from the
    wire on purpose; present here on purpose."""
    save([fold_shard(shard)], tmp_path / "exports")
    doc = json.loads((tmp_path / "exports" / f"{JID}.json").read_text())

    last = doc["steps"][-1]["messages"]
    assert all("trainable_status" in m for m in last)
    by_role = {m["role"]: m["trainable_status"] for m in last}
    assert by_role["assistant"] == "trainable"
    assert by_role["system"] == "not_trainable"
    assert by_role["tool"] == "not_trainable"


def test_tool_call_correlation_survives_the_export(tmp_path, shard):
    save([fold_shard(shard)], tmp_path / "exports")
    doc = json.loads((tmp_path / "exports" / f"{JID}.json").read_text())

    msgs = doc["steps"][-1]["messages"]
    call = next(m for m in msgs if m.get("tool_calls"))
    resp = next(m for m in msgs if m.get("tool_response"))
    assert call["tool_calls"][0]["id"] == resp["tool_response"]["id"] == "c1"


def test_the_file_is_named_by_conversation_id(tmp_path, shard):
    """`tj.save()` writes `{output_dir}/{conversation_id}.json`."""
    result = save([fold_shard(shard)], tmp_path / "exports")
    assert [p.name for p in result.written] == [f"{JID}.json"]


def test_a_platform_owned_field_keeps_the_platform_spelling(tmp_path):
    """`reference_journey` is odyssey's name; the schema says
    `reference_trajectory`. The artifact exists to be read without a translation
    table, so the platform wins — same rule as `TelemetryEvent.trajectory_id`."""
    r = fold(stream(), data_source="livekit")
    journey = dataclasses.replace(r.journey, reference_journey={"a": 1})
    doc = journey_to_dict(journey)

    assert doc["reference_trajectory"] == {"a": 1}
    assert "reference_journey" not in doc


def test_nulls_are_dropped_but_false_and_zero_are_not(tmp_path, shard):
    """Absence is absence; `false` is a value."""
    doc = journey_to_dict(fold_shard(shard).journey, diagnostics={"complete": False})
    assert "error" not in doc  # None, so gone
    assert doc[DIAGNOSTICS_KEY]["complete"] is False  # kept


# --------------------------------------------------------------------------
# Journey identity and tags must survive the hop
# --------------------------------------------------------------------------


def test_fold_shard_takes_identity_from_the_files_own_header(shard):
    """The payoff of the v1.1 header: `fold()` no longer needs to be told what
    the file is by whoever happens to be holding it."""
    r = fold_shard(shard)
    assert r.journey.task.data_source == "livekit"
    assert r.journey.task.conversation_id == JID
    assert r.journey.trace_id == "t_9"


def test_journey_tags_reach_the_artifact(tmp_path, shard):
    """`agent_id` and `handler` are in the shard header. The builder reads only
    num_turns/total_tokens/total_cost out of `task_metadata` and drops the rest,
    so without the `extra_telemetry` passthrough they would survive the wire and
    then vanish here — data loss at the last hop."""
    save([fold_shard(shard)], tmp_path / "exports")
    doc = json.loads((tmp_path / "exports" / f"{JID}.json").read_text())

    assert doc["telemetry"]["data"]["agent_id"] == "agent_7"
    assert doc["telemetry"]["data"]["handler"] == "LiteV2Handler"
    assert doc["telemetry"]["source"] == "livekit"


def test_a_v1_0_shard_without_a_header_still_exports(tmp_path):
    """No identity to inherit, so `data_source` falls back rather than failing —
    a file that predates v1.1 is still worth exporting."""
    p = tmp_path / "events" / "old.jsonl"
    p.parent.mkdir(parents=True)
    write_events(p, stream())  # bare header
    r = fold_shard(p)
    assert r.journey.task.data_source == "unknown"


# --------------------------------------------------------------------------
# Incomplete journeys: written, but never passing for whole
# --------------------------------------------------------------------------


def test_complete_is_always_stated_even_when_true(tmp_path, shard):
    """A flag that appears only on failure is one a consumer forgets to check:
    the absent key and the healthy key look identical to code that never saw a
    bad file."""
    save([fold_shard(shard)], tmp_path / "exports")
    doc = json.loads((tmp_path / "exports" / f"{JID}.json").read_text())
    assert doc[DIAGNOSTICS_KEY]["complete"] is True


def test_a_journey_with_no_terminal_is_exported_and_flagged(tmp_path):
    r = fold([e for e in stream() if e.kind != "terminal"], data_source="livekit")
    result = save([r], tmp_path / "exports")

    doc = json.loads(result.written[0].read_text())
    assert doc[DIAGNOSTICS_KEY]["complete"] is False
    assert "no terminal event" in doc[DIAGNOSTICS_KEY]["incomplete_reason"]
    assert doc[DIAGNOSTICS_KEY]["terminated"] is False
    # Reported without re-reading the file, for a caller wanting stricter policy.
    assert "no terminal event" in result.incomplete[JID]


def test_a_gap_in_seq_is_flagged_with_the_missing_numbers(tmp_path):
    events = [e for e in stream() if e.seq != 3]
    result = save([fold(events, data_source="livekit")], tmp_path / "exports")

    diag = json.loads(result.written[0].read_text())[DIAGNOSTICS_KEY]
    assert diag["complete"] is False
    assert diag["missing_seqs"] == [3]


def test_a_writer_conflict_is_named_in_the_artifact(tmp_path):
    """The one diagnostic that means "do not train on this": two processes each
    allocated `seq` from zero, so the journey is a silent interleaving."""
    tagged = [
        dataclasses.replace(e, metadata={odyssey.WRITER_META_KEY: f"w{i % 2}"})
        for i, e in enumerate(stream())
    ]
    result = save([fold(tagged, data_source="livekit")], tmp_path / "exports")

    diag = json.loads(result.written[0].read_text())[DIAGNOSTICS_KEY]
    assert diag["complete"] is False
    assert sorted(diag["writers"]) == ["w0", "w1"]


# --------------------------------------------------------------------------
# Writing is not allowed to go wrong quietly
# --------------------------------------------------------------------------


def test_the_write_is_atomic(tmp_path, shard):
    """A reader watching this directory must never pick up half a file."""
    out = tmp_path / "exports"
    save([fold_shard(shard)], out)
    assert list(out.glob("*.tmp")) == []
    assert json.loads((out / f"{JID}.json").read_text())  # parses whole


def test_a_conversation_id_cannot_escape_the_output_directory(tmp_path):
    """Journey ids are caller-chosen; nothing stops one holding a separator."""
    r = fold(stream(), data_source="livekit", conversation_id="../../etc/passwd")
    result = save([r], tmp_path / "exports")

    assert result.written[0].parent == tmp_path / "exports"
    assert result.written[0].name == "etc_passwd.json"


@pytest.mark.parametrize(
    "cid,expected",
    [
        ("../../etc/passwd", "etc_passwd.json"),
        ("room/sub", "room_sub.json"),
        (r"win\path", "win_path.json"),
        ("..", "journey.json"),
        ("", "journey.json"),
        (".hidden", "hidden.json"),
        ("x" * 400, "x" * 240 + ".json"),
    ],
)
def test_filename_sanitisation(cid, expected):
    from odyssey.export import _filename

    name = _filename(cid)
    assert name == expected
    assert "/" not in name and "\\" not in name
    assert not name.startswith(".")


def test_export_dir_survives_one_unreadable_shard(tmp_path):
    """The other journeys in the directory are fine; a partial export beats
    none, and the failure is named rather than swallowed."""
    src = tmp_path / "events"
    src.mkdir()
    write_events(src / f"{JID}.jsonl", stream(), header=HEADER)
    (src / "broken.jsonl").write_text("not a header\n")

    result = export_dir(src, tmp_path / "exports")
    assert result.count == 1
    assert result.ok is False
    assert any("broken.jsonl" in e for e in result.errors)


def test_export_dir_can_target_one_journey(tmp_path):
    src = tmp_path / "events"
    src.mkdir()
    write_events(src / f"{JID}.jsonl", stream(), header=HEADER)
    write_events(src / "other.jsonl", stream(), header=HEADER)

    result = export_dir(src, tmp_path / "exports", journey_id=JID)
    assert [p.name for p in result.written] == [f"{JID}.json"]


def test_folding_an_empty_shard_is_an_error_not_an_empty_artifact(tmp_path):
    """An empty file must not become a Trajectory with no steps — that would
    look like a real conversation in which nothing was said."""
    p = tmp_path / "empty.jsonl"
    write_events(p, [], header=HEADER)
    with pytest.raises(ExportError, match="no events"):
        fold_shard(p)


def test_the_cli_exports_and_reports_what_it_flagged(tmp_path, capsys):
    from odyssey.cli import main

    src = tmp_path / "events"
    src.mkdir()
    write_events(
        src / f"{JID}.jsonl",
        [e for e in stream() if e.kind != "terminal"],
        header=HEADER,
    )

    rc = main(
        [
            "export",
            "--events",
            str(src),
            "--out",
            str(tmp_path / "exports"),
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0  # flagged, not failed — the caller decides
    assert "exported 1" in captured.out
    assert "no terminal event" in captured.err


# --------------------------------------------------------------------------
# Straight from the spool — "show me the artifact for the call I just recorded"
# --------------------------------------------------------------------------


def _spool(root, **kw):
    from odyssey.spool import Spool, SpoolConfig

    return Spool(SpoolConfig(root=root, **kw))


def test_export_reads_the_spool_without_a_push(tmp_path):
    """`export_dir` cannot answer this: the spool is not a flat directory. It
    nests one directory per journey, so a journey lives at
    `<root>/journeys/<jid>/NNN.jsonl` where a `*.jsonl` glob finds nothing."""
    sp = _spool(tmp_path / "spool")
    for e in stream():
        sp.record(e, header=HEADER)
    sp.close()

    result = odyssey.export_spool(tmp_path / "spool", tmp_path / "exports")
    doc = json.loads(result.written[0].read_text())
    assert result.count == 1
    assert doc["task"]["conversation_id"] == JID
    assert doc["steps"]


def test_exporting_does_not_drain_the_spool(tmp_path):
    """Exporting is a view, not a consumption: no watermark moves, so a later
    `push` still ships every event."""
    sp = _spool(tmp_path / "spool")
    for e in stream():
        sp.record(e, header=HEADER)
    sp.close()

    odyssey.export_spool(tmp_path / "spool", tmp_path / "exports")
    assert sp.watermark(JID) is None
    assert len(sp.undrained(JID)) == len(stream())


def test_export_spool_reassembles_a_rotated_journey(tmp_path):
    """Several shards, one artifact. `Spool.read` is the only thing that knows
    how to put them back in order."""
    sp = _spool(tmp_path / "spool", max_shard_bytes=200)
    for e in stream():
        sp.record(e, header=HEADER)
    sp.close()
    assert len(sp.shards(JID)) > 1

    result = odyssey.export_spool(tmp_path / "spool", tmp_path / "exports")
    doc = json.loads(result.written[0].read_text())
    assert len(doc["steps"][-1]["messages"]) == 5  # nothing lost across shards


def test_export_spool_can_target_one_journey(tmp_path):
    sp = _spool(tmp_path / "spool")
    for e in stream():
        sp.record(e, header=HEADER)
    sp.record(
        dataclasses.replace(stream()[0], journey_id="other"),
        header=dataclasses.replace(HEADER, journey_id="other"),
    )
    sp.close()

    result = odyssey.export_spool(
        tmp_path / "spool", tmp_path / "exports", journey_id=JID
    )
    assert [p.name for p in result.written] == [f"{JID}.json"]


def test_the_cli_exports_from_the_spool_by_default(tmp_path, capsys):
    from odyssey.cli import main

    sp = _spool(tmp_path / "spool")
    for e in stream():
        sp.record(e, header=HEADER)
    sp.close()

    rc = main(
        ["--spool", str(tmp_path / "spool"), "export", "--out", str(tmp_path / "out")]
    )
    assert rc == 0
    assert "exported 1" in capsys.readouterr().out
    assert (tmp_path / "out" / f"{JID}.json").exists()


# --------------------------------------------------------------------------
# Only the last step: the one that already holds the whole conversation
# --------------------------------------------------------------------------


def multi_turn():
    """Three user turns, so the fold builds three cumulative steps."""
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


@pytest.fixture
def long_shard(tmp_path):
    p = tmp_path / "events" / f"{JID}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    write_events(p, multi_turn(), header=HEADER)
    return p


def test_the_last_step_alone_still_carries_every_message(tmp_path, long_shard):
    """Steps are prefixes of each other, so N-1 of them are duplicates.

    A twelve-turn phone call exported all twelve cumulative steps: 54 KB of
    which 50 KB was the same messages written again, quadratic in turns. The
    final step is the only one that is not a prefix of another.
    """
    save([fold_shard(long_shard)], tmp_path / "exports", last_step_only=True)
    doc = json.loads((tmp_path / "exports" / f"{JID}.json").read_text())

    assert len(doc["steps"]) == 1
    msgs = doc["steps"][0]["messages"]
    assert [m["role"] for m in msgs] == [
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
        "assistant",
    ]


def test_trimming_the_steps_keeps_the_tool_call_and_its_result(tmp_path, long_shard):
    """The metric a failed booking shows up in must survive the trim."""
    save([fold_shard(long_shard)], tmp_path / "exports", last_step_only=True)
    doc = json.loads((tmp_path / "exports" / f"{JID}.json").read_text())

    msgs = doc["steps"][0]["messages"]
    call = next(m for m in msgs if m.get("tool_calls"))
    resp = next(m for m in msgs if m.get("tool_response"))
    assert call["tool_calls"][0]["id"] == resp["tool_response"]["id"] == "c1"
    assert doc["metrics"]["num_tool_calls"] == 1


def test_a_trimmed_export_says_so(tmp_path, long_shard):
    """A consumer counting steps must be able to tell a one-turn call from a
    trimmed three-turn one."""
    save([fold_shard(long_shard)], tmp_path / "exports", last_step_only=True)
    doc = json.loads((tmp_path / "exports" / f"{JID}.json").read_text())

    assert doc[DIAGNOSTICS_KEY]["steps_written"] == "last"
    assert doc["task"]["num_turns"] == 3  # the conversation is unchanged
    assert doc[DIAGNOSTICS_KEY]["complete"] is True


def test_a_full_export_is_not_marked_as_trimmed(tmp_path, long_shard):
    save([fold_shard(long_shard)], tmp_path / "exports")
    doc = json.loads((tmp_path / "exports" / f"{JID}.json").read_text())

    assert len(doc["steps"]) == 3
    assert "steps_written" not in doc[DIAGNOSTICS_KEY]


def test_trimming_a_single_step_journey_changes_nothing(tmp_path):
    """One step is already the last one; the file must not claim a trim."""
    events = [
        msg(0, "user", "hi"),
        msg(1, "assistant", "hello"),
        ev(2, kind="terminal", terminal=Terminal(termination_reason="ENV_DONE")),
    ]
    p = tmp_path / "events" / f"{JID}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    write_events(p, events, header=HEADER)

    save([fold_shard(p)], tmp_path / "exports", last_step_only=True)
    doc = json.loads((tmp_path / "exports" / f"{JID}.json").read_text())

    assert len(doc["steps"]) == 1
    assert "steps_written" not in doc[DIAGNOSTICS_KEY]


def test_the_cli_exports_only_the_last_step_on_demand(tmp_path, capsys):
    spool_dir = tmp_path / "spool"
    spool = odyssey.Spool(odyssey.SpoolConfig(root=spool_dir))
    spool.record_all(multi_turn(), header=HEADER)
    spool.close()

    rc = odyssey.cli.main(
        [
            "--spool",
            str(spool_dir),
            "export",
            "--out",
            str(tmp_path / "exports"),
            "--last-step",
        ]
    )
    assert rc == 0
    doc = json.loads((tmp_path / "exports" / f"{JID}.json").read_text())
    assert len(doc["steps"]) == 1
    assert doc[DIAGNOSTICS_KEY]["steps_written"] == "last"
