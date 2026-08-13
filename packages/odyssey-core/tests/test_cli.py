"""The CLI drain trigger. Same drain() as sdk.push() and the interval drainer."""

from __future__ import annotations

from pathlib import Path

from odyssey.cli import FileSink, main
from odyssey.jsonl import read_events
from odyssey.primitives import JourneyEvent, Message, Terminal
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

    def boom(self, journey_id, events):
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
