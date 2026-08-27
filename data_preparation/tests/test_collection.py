"""Collection: raw traces -> flat *.jsonl raw layer (item 3.1)."""

from __future__ import annotations

from odyssey.jsonl import read_events, write_events
from odyssey.primitives import JourneyEvent, JourneyHeader, Message, Terminal
from odyssey.spool import Spool, SpoolConfig

from odyssey_dataprep.collection import collect_from_collector, collect_from_spool


def ev(jid, seq, **kw):
    return JourneyEvent(journey_id=jid, seq=seq, event_id=f"{jid}-{seq}", **kw)


def msg(jid, seq, role, content):
    return ev(jid, seq, kind="message", message=Message(role=role, content=content))


def term(jid, seq):
    return ev(
        jid, seq, kind="terminal", terminal=Terminal(termination_reason="ENV_DONE")
    )


def test_collect_from_spool_reassembles_rotated_shards(tmp_path):
    spool = Spool(SpoolConfig(root=tmp_path / ".odyssey"))
    spool.record(msg("j1", 0, "user", "hi"))
    spool.record(msg("j1", 1, "assistant", "hello"))
    spool.record(term("j1", 2))
    spool.close()

    result = collect_from_spool(tmp_path / ".odyssey", tmp_path / "raw")
    assert result.ok and result.count == 1

    parsed = read_events(result.written[0])
    assert [e.seq for e in parsed.events] == [0, 1, 2]
    assert parsed.header.journey_id == "j1"


def test_collect_from_collector_merges_across_date_partitions(tmp_path):
    collector_root = tmp_path / "collector"
    day1 = collector_root / "2026-08-25"
    day2 = collector_root / "2026-08-26"
    day1.mkdir(parents=True)
    day2.mkdir(parents=True)

    header = JourneyHeader(journey_id="j2", data_source="test")
    write_events(day1 / "j2.jsonl", [msg("j2", 0, "user", "hi")], header=header)
    write_events(
        day2 / "j2.jsonl",
        [msg("j2", 1, "assistant", "hello"), term("j2", 2)],
        header=header,
    )

    result = collect_from_collector(collector_root, tmp_path / "raw")
    assert result.ok and result.count == 1

    parsed = read_events(result.written[0])
    assert sorted(e.seq for e in parsed.events) == [0, 1, 2]
    assert parsed.header.journey_id == "j2"


def test_collect_from_collector_keeps_journeys_separate(tmp_path):
    collector_root = tmp_path / "collector"
    day = collector_root / "2026-08-25"
    day.mkdir(parents=True)

    write_events(
        day / "a.jsonl",
        [msg("a", 0, "user", "1")],
        header=JourneyHeader(journey_id="a"),
    )
    write_events(
        day / "b.jsonl",
        [msg("b", 0, "user", "2")],
        header=JourneyHeader(journey_id="b"),
    )

    result = collect_from_collector(collector_root, tmp_path / "raw")
    assert result.count == 2
    names = {p.name for p in result.written}
    assert names == {"a.jsonl", "b.jsonl"}
