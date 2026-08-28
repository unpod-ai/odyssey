"""Collection: raw traces -> flat *.jsonl raw layer (item 3.1)."""

from __future__ import annotations

import io

from odyssey.jsonl import read_events, write_events
from odyssey.primitives import JourneyEvent, JourneyHeader, Message, Terminal
from odyssey.spool import Spool, SpoolConfig

from odyssey_dataprep.collection import (
    collect_from_collector,
    collect_from_object_store,
    collect_from_spool,
)


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


class FakeS3Client:
    """A minimal boto3 S3 client double (item 1.10) -- just the two methods
    ``collect_from_object_store`` calls, paginated in two pages so the
    continuation-token loop is actually exercised."""

    def __init__(self, objects):
        # objects: dict of key -> bytes
        self._objects = objects

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        keys = sorted(k for k in self._objects if k.startswith(Prefix))
        page_size = 1
        start = 0 if ContinuationToken is None else int(ContinuationToken)
        page = keys[start : start + page_size]
        next_start = start + page_size
        truncated = next_start < len(keys)
        return {
            "Contents": [{"Key": k} for k in page],
            "IsTruncated": truncated,
            "NextContinuationToken": str(next_start) if truncated else None,
        }

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self._objects[Key])}


def _jsonl_bytes(jid, events, header=None):
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "tmp.jsonl"
        write_events(path, events, header=header or JourneyHeader(journey_id=jid))
        return path.read_bytes()


def test_collect_from_object_store_merges_across_keys(tmp_path):
    body1 = _jsonl_bytes("j3", [msg("j3", 0, "user", "hi")])
    body2 = _jsonl_bytes("j3", [msg("j3", 1, "assistant", "hello"), term("j3", 2)])
    client = FakeS3Client(
        {
            "traces/2026-08-25/j3.jsonl": body1,
            "traces/2026-08-26/j3.jsonl": body2,
            "other-prefix/x.jsonl": _jsonl_bytes("x", [msg("x", 0, "user", "nope")]),
        }
    )

    result = collect_from_object_store(
        "bucket", "traces/", tmp_path / "raw", client=client
    )
    assert result.ok and result.count == 1

    parsed = read_events(result.written[0])
    assert sorted(e.seq for e in parsed.events) == [0, 1, 2]
    assert parsed.header.journey_id == "j3"


def test_collect_from_object_store_keeps_journeys_separate(tmp_path):
    client = FakeS3Client(
        {
            "a.jsonl": _jsonl_bytes("a", [msg("a", 0, "user", "1")]),
            "b.jsonl": _jsonl_bytes("b", [msg("b", 0, "user", "2")]),
        }
    )

    result = collect_from_object_store("bucket", "", tmp_path / "raw", client=client)
    assert result.count == 2
    names = {p.name for p in result.written}
    assert names == {"a.jsonl", "b.jsonl"}


def test_collect_from_object_store_records_one_bad_key_without_aborting(tmp_path):
    client = FakeS3Client(
        {
            "good.jsonl": _jsonl_bytes("good", [msg("good", 0, "user", "hi")]),
            "bad.jsonl": b"not valid jsonl {{{",
        }
    )

    result = collect_from_object_store("bucket", "", tmp_path / "raw", client=client)
    assert not result.ok
    assert result.count == 1
    assert any("bad.jsonl" in e for e in result.errors)
