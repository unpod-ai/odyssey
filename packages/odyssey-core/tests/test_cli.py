"""The CLI drain trigger. Same drain() as sdk.push() and the interval drainer."""

from __future__ import annotations

from pathlib import Path

from odyssey.cli import FileSink, main
from odyssey.jsonl import read_events
from odyssey.primitives import JourneyEvent, Message, Signal, Terminal
from odyssey.spool import Spool, SpoolConfig

JID = "j_cli"


def seed(root: Path, n: int = 3) -> Spool:
    s = Spool(SpoolConfig(root=root))
    s.record_all(
        JourneyEvent(
            journey_id=JID,
            seq=i,
            kind="message",
            event_id=f"e{i}",
            ts=f"2026-01-01T00:00:{i:02d}+00:00",
            message=Message(role="assistant", content=f"m{i}"),
        )
        for i in range(n)
    )
    return s


def test_push_drains_the_spool_to_jsonl(tmp_path, capsys):
    root = tmp_path / "spool"
    seed(root)
    out = tmp_path / "out"
    rc = main(["--spool", str(root), "push", "--out", str(out)])
    assert rc == 0
    assert "pushed  3" in capsys.readouterr().out

    written = out / f"{JID}.jsonl"
    assert written.exists()
    assert [e.seq for e in read_events(written).events] == [0, 1, 2]


def test_push_advances_the_watermark_so_a_second_push_is_a_noop(tmp_path, capsys):
    root = tmp_path / "spool"
    seed(root)
    out = tmp_path / "out"
    main(["--spool", str(root), "push", "--out", str(out)])
    capsys.readouterr()
    rc = main(["--spool", str(root), "push", "--out", str(out)])
    assert rc == 0
    assert "pushed  0" in capsys.readouterr().out


def test_push_appends_the_tail_on_resume(tmp_path):
    root = tmp_path / "spool"
    s = seed(root, n=2)
    out = tmp_path / "out"
    main(["--spool", str(root), "push", "--out", str(out)])
    s.record(
        JourneyEvent(
            journey_id=JID,
            seq=2,
            kind="terminal",
            event_id="t2",
            ts="2026-01-01T00:00:02+00:00",
            terminal=Terminal(termination_reason="ENV_DONE"),
        )
    )
    main(["--spool", str(root), "push", "--out", str(out)])
    # Appended, not rewritten: all three events present exactly once.
    events = read_events(out / f"{JID}.jsonl").events
    assert [e.seq for e in events] == [0, 1, 2]


def test_push_reports_gaps_on_stderr(tmp_path, capsys):
    root = tmp_path / "spool"
    s = Spool(SpoolConfig(root=root))
    for i in (0, 2):
        s.record(
            JourneyEvent(
                journey_id=JID,
                seq=i,
                kind="message",
                event_id=f"e{i}",
                message=Message(role="user", content="x"),
            )
        )
    main(["--spool", str(root), "push", "--out", str(tmp_path / "out")])
    assert "missing seq [1]" in capsys.readouterr().err


def test_push_exits_nonzero_when_the_sink_fails(tmp_path, capsys, monkeypatch):
    root = tmp_path / "spool"
    seed(root)

    def boom(self, journey_id, events, header=None):
        raise OSError("disk full")

    monkeypatch.setattr(FileSink, "send", boom)
    rc = main(["--spool", str(root), "push", "--out", str(tmp_path / "out")])
    assert rc == 1
    assert "disk full" in capsys.readouterr().err


def test_status_lists_per_journey_state(tmp_path, capsys):
    root = tmp_path / "spool"
    seed(root)
    rc = main(["--spool", str(root), "status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert JID in out and "undrained" in out


def test_status_on_an_empty_spool(tmp_path, capsys):
    rc = main(["--spool", str(tmp_path / "empty"), "status"])
    assert rc == 0
    assert "spool is empty" in capsys.readouterr().out


# --------------------------------------------------------------------------
# health — "is it actually recording?"
#
# Read-only against the spool, so it is safe to run while a process is writing.
# Exits 3 on a writer conflict: the lineage-violation code CI greps for (ADR 0003).
# --------------------------------------------------------------------------


def test_health_on_an_empty_spool_is_clean(tmp_path, capsys):
    assert main(["--spool", str(tmp_path / "nothing"), "health"]) == 0
    assert "empty" in capsys.readouterr().out


def test_health_reports_a_trainable_journey(tmp_path, capsys):
    s = seed(tmp_path / "spool")
    s.record(
        JourneyEvent(
            journey_id=JID,
            seq=3,
            kind="terminal",
            event_id="t3",
            terminal=Terminal(termination_reason="ENV_DONE"),
        )
    )
    assert main(["--spool", str(tmp_path / "spool"), "health"]) == 0
    out = capsys.readouterr().out
    assert JID in out
    assert "True" in out


def test_health_names_why_a_journey_is_not_exportable(tmp_path, capsys):
    seed(tmp_path / "spool")  # three messages, no terminal
    assert main(["--spool", str(tmp_path / "spool"), "health"]) == 0
    assert "may still be running" in capsys.readouterr().out


def test_health_exits_3_on_a_writer_conflict(tmp_path, capsys):
    """A silent interleave of two conversations is the one thing CI must catch."""
    from odyssey.primitives import WRITER_META_KEY

    s = Spool(SpoolConfig(root=tmp_path / "spool"))
    for i, writer in enumerate(("w1", "w2")):
        s.record(
            JourneyEvent(
                journey_id=JID,
                seq=i,
                kind="message",
                event_id=f"c{i}",
                message=Message(role="assistant", content="x"),
                metadata={WRITER_META_KEY: writer},
            )
        )
    assert main(["--spool", str(tmp_path / "spool"), "health"]) == 3
    out = capsys.readouterr().out
    assert "WRITER CONFLICT" in out


def test_health_json_is_machine_readable(tmp_path, capsys):
    import json as _json

    seed(tmp_path / "spool")
    assert main(["--spool", str(tmp_path / "spool"), "health", "--json"]) == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload["process"]["initialised"] is False
    assert payload["journeys"][0]["journey_id"] == JID
    assert payload["journeys"][0]["events"] == 3


# --------------------------------------------------------------------------
# show — "what did you capture, and what of it is trainable?"
#
# health answers "is it recording". This answers the question a person has next.
# Without it the only way to inspect a corpus is reading raw JSONL, which is how
# a capture layer ends up trusted on faith instead of on evidence.
# --------------------------------------------------------------------------


def preference_journey(root: Path) -> Spool:
    """A journey with a regenerated answer — the minimum DPO shape."""
    s = Spool(SpoolConfig(root=root))
    s.record_all(
        [
            JourneyEvent(
                journey_id=JID,
                seq=0,
                kind="message",
                event_id="m0",
                message=Message(role="user", content="Book Tuesday 3pm"),
            ),
            JourneyEvent(
                journey_id=JID,
                seq=1,
                kind="message",
                event_id="m1",
                message=Message(role="assistant", content="weak answer"),
            ),
            JourneyEvent(
                journey_id=JID,
                seq=2,
                kind="signal",
                event_id="s2",
                signal=Signal(signal="regenerated", target_seq=1),
            ),
            JourneyEvent(
                journey_id=JID,
                seq=3,
                kind="message",
                event_id="m3",
                message=Message(role="assistant", content="strong answer"),
            ),
            JourneyEvent(
                journey_id=JID,
                seq=4,
                kind="signal",
                event_id="s4",
                signal=Signal(signal="thumbs_up", target_seq=3),
            ),
            JourneyEvent(
                journey_id=JID,
                seq=5,
                kind="terminal",
                event_id="t5",
                terminal=Terminal(termination_reason="ENV_DONE"),
            ),
        ]
    )
    return s


def test_show_prints_the_conversation_in_order(tmp_path, capsys):
    seed(tmp_path / "spool")
    assert main(["--spool", str(tmp_path / "spool"), "show", JID]) == 0
    out = capsys.readouterr().out
    assert JID in out
    assert out.index("m0") < out.index("m1") < out.index("m2")


def test_show_marks_the_trainable_turn(tmp_path, capsys):
    """The label is DERIVED, not read off disk.

    Events carry whatever trainable_status the producer set — usually the
    default — and the real label depends on signals that arrive later. Reading
    the recorded field would report every turn as not_trainable.
    """
    preference_journey(tmp_path / "spool")
    assert main(["--spool", str(tmp_path / "spool"), "show", JID]) == 0
    lines = capsys.readouterr().out.splitlines()
    strong = [ln for ln in lines if "strong answer" in ln][0]
    weak = [ln for ln in lines if "weak answer" in ln][0]
    assert "trainable" in strong
    assert "superseded" in weak


def test_show_surfaces_the_preference_pair(tmp_path, capsys):
    preference_journey(tmp_path / "spool")
    main(["--spool", str(tmp_path / "spool"), "show", JID])
    out = capsys.readouterr().out
    assert "TRAINABLE" in out
    assert "superseded     : 1 turn(s) at seq [1]" in out


def test_show_says_why_an_unexportable_journey_is_unexportable(tmp_path, capsys):
    seed(tmp_path / "spool")  # three messages, no terminal
    main(["--spool", str(tmp_path / "spool"), "show", JID])
    out = capsys.readouterr().out
    assert "NOT TRAINABLE" in out
    assert "may still be running" in out
    assert "nothing exportable" in out


def test_show_with_no_journey_argument_renders_all(tmp_path, capsys):
    s = Spool(SpoolConfig(root=tmp_path / "spool"))
    for jid in ("a", "b"):
        s.record(
            JourneyEvent(
                journey_id=jid,
                seq=0,
                kind="message",
                event_id=f"e-{jid}",
                message=Message(role="user", content=f"hello {jid}"),
            )
        )
    main(["--spool", str(tmp_path / "spool"), "show"])
    out = capsys.readouterr().out
    assert "hello a" in out and "hello b" in out


def test_show_on_an_empty_spool_says_so(tmp_path, capsys):
    assert main(["--spool", str(tmp_path / "none"), "show"]) == 0
    assert "empty" in capsys.readouterr().out
