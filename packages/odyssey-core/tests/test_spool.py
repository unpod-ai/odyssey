"""Spool: local append, crash safety, redaction, watermarks, drain semantics."""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
import threading

import pytest

from odyssey.jsonl import read_events, read_header
from odyssey.primitives import (
    JourneyEvent,
    JourneyHeader,
    Message,
    Terminal,
    ToolCall,
    ToolResponse,
)
from odyssey.spool import (
    REDACTED,
    DrainResult,
    IntervalDrainer,
    Spool,
    SpoolConfig,
    SpoolPathError,
    drain,
    redact_event,
    redact_header,
    safe_child,
    validate_interval,
)

JID = "j_spool"


def ev(seq: int, role: str = "assistant", content: str = "x", **kw) -> JourneyEvent:
    return JourneyEvent(
        journey_id=JID,
        seq=seq,
        kind="message",
        event_id=f"e{seq}",
        ts=f"2026-01-01T00:00:{seq:02d}+00:00",
        message=Message(role=role, content=content),
        **kw,
    )


def spool(tmp_path, **kw) -> Spool:
    return Spool(SpoolConfig(root=tmp_path / "spool", **kw))


class MemorySink:
    def __init__(self):
        self.batches: list[tuple[str, list[JourneyEvent]]] = []
        self.headers: list = []

    def send(self, journey_id, events, header=None):
        self.batches.append((journey_id, list(events)))
        self.headers.append(header)

    @property
    def all_events(self):
        return [e for _, batch in self.batches for e in batch]


class FailingSink:
    def __init__(self, exc=RuntimeError("sink down")):
        self.exc = exc
        self.calls = 0

    def send(self, journey_id, events, header=None):
        self.calls += 1
        raise self.exc


# --------------------------------------------------------------------------
# Recording is local only
# --------------------------------------------------------------------------


def test_record_writes_locally_and_reads_back(tmp_path):
    s = spool(tmp_path)
    s.record_all([ev(0), ev(1), ev(2)])
    assert [e.seq for e in s.read(JID)] == [0, 1, 2]


def test_record_succeeds_with_no_sink_configured(tmp_path):
    """Recording must work with nothing reachable — no sink is even passed."""
    s = spool(tmp_path)
    s.record(ev(0))
    assert len(s.read(JID)) == 1


def test_record_performs_no_network_io(tmp_path, monkeypatch):
    """Any socket construction on the record path is a design violation."""
    import socket

    def boom(*a, **k):
        raise AssertionError("record() attempted network I/O")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    s = spool(tmp_path)
    s.record_all([ev(i) for i in range(5)])
    assert len(s.read(JID)) == 5


def test_shard_has_a_header_and_one_json_object_per_line(tmp_path):
    s = spool(tmp_path)
    s.record_all([ev(0), ev(1)])
    lines = s.shards(JID)[0].read_text().splitlines()
    assert len(lines) == 3  # header + 2
    for line in lines:
        assert isinstance(json.loads(line), dict)


# --------------------------------------------------------------------------
# Crash safety and concurrency
# --------------------------------------------------------------------------


def test_events_survive_sigkill(tmp_path):
    """Hard-kill a real child process mid-stream; completed writes must remain."""
    root = tmp_path / "spool"
    script = f"""
import sys, os, time
sys.path.insert(0, {str(_src_dir())!r})
from odyssey.spool import Spool, SpoolConfig
from odyssey.primitives import JourneyEvent, Message
s = Spool(SpoolConfig(root={str(root)!r}, fsync=True))
for i in range(20):
    s.record(JourneyEvent(journey_id={JID!r}, seq=i, kind="message",
             event_id=f"e{{i}}", message=Message(role="assistant", content="x")))
print("READY", flush=True)
time.sleep(30)
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True
    )
    assert proc.stdout.readline().strip() == "READY"
    proc.kill()
    proc.wait(timeout=10)

    s = Spool(SpoolConfig(root=root))
    assert [e.seq for e in s.read(JID)] == list(range(20))


def _src_dir() -> str:
    import odyssey

    return str(__import__("pathlib").Path(odyssey.__file__).parent.parent)


def test_concurrent_writers_never_interleave_a_line(tmp_path):
    s = spool(tmp_path)
    errors: list[Exception] = []

    def writer(base: int):
        try:
            for i in range(50):
                s.record(ev(base + i))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(b,)) for b in (0, 100, 200, 300)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    # Every line must be complete, well-formed JSON — no torn writes.
    for shard in s.shards(JID):
        for line in shard.read_text().splitlines():
            json.loads(line)
    assert len(s.read(JID)) == 200


def test_shard_rotates_at_the_size_cap(tmp_path):
    s = spool(tmp_path, max_shard_bytes=400)
    s.record_all([ev(i) for i in range(20)])
    shards = s.shards(JID)
    assert len(shards) > 1
    assert [p.name for p in shards] == sorted(p.name for p in shards)
    # No event is lost across a rotation.
    assert [e.seq for e in s.read(JID)] == list(range(20))


def test_zero_shard_size_rejected(tmp_path):
    with pytest.raises(ValueError, match="must be positive"):
        SpoolConfig(root=tmp_path, max_shard_bytes=0)


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------


def test_secret_in_tool_arguments_is_masked_before_disk(tmp_path):
    s = spool(tmp_path)
    e = JourneyEvent(
        journey_id=JID,
        seq=0,
        kind="message",
        event_id="e0",
        message=Message(
            role="assistant",
            tool_calls=[
                ToolCall(name="login", arguments={"user": "amy", "password": "hunter2"})
            ],
        ),
    )
    s.record(e)
    raw = s.shards(JID)[0].read_text()
    assert "hunter2" not in raw
    assert REDACTED in raw
    assert "amy" in raw  # non-secret keys survive


def test_secret_in_metadata_and_tool_response_is_masked(tmp_path):
    s = spool(tmp_path)
    e = JourneyEvent(
        journey_id=JID,
        seq=0,
        kind="message",
        event_id="e0",
        metadata={"api_key": "sk-live-123"},
        message=Message(
            role="tool",
            tool_response=ToolResponse(
                id="c1", name="f", arguments={}, response={"auth_token": "abc"}
            ),
        ),
    )
    s.record(e)
    raw = s.shards(JID)[0].read_text()
    assert "sk-live-123" not in raw and "abc" not in raw


def test_empty_secret_value_is_not_marked_redacted():
    """A marker must always mean a real value existed."""
    e = JourneyEvent(
        journey_id=JID,
        seq=0,
        kind="message",
        event_id="e0",
        metadata={"password": "", "token": None},
        message=Message(role="user", content="hi"),
    )
    out = redact_event(e, frozenset({"password", "token"}))
    assert out.metadata == {"password": "", "token": None}


def test_message_content_is_never_redacted():
    """content is the training data; blanket masking would destroy the corpus."""
    e = JourneyEvent(
        journey_id=JID,
        seq=0,
        kind="message",
        event_id="e0",
        message=Message(role="user", content="my password is hunter2"),
    )
    out = redact_event(e, frozenset({"password"}))
    assert out.message.content == "my password is hunter2"


def test_nested_token_key_is_caught():
    e = JourneyEvent(
        journey_id=JID,
        seq=0,
        kind="message",
        event_id="e0",
        metadata={"outer": {"refresh_token": "zzz"}},
        message=Message(role="user", content="x"),
    )
    out = redact_event(e, frozenset({"token"}))
    assert out.metadata["outer"]["refresh_token"] == REDACTED


# --------------------------------------------------------------------------
# Path containment — configured root, not cwd
# --------------------------------------------------------------------------


def test_spool_root_outside_cwd_is_accepted(tmp_path, monkeypatch):
    workdir = tmp_path / "elsewhere"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    s = Spool(SpoolConfig(root=tmp_path / "outside_spool"))
    s.record(ev(0))
    assert len(s.read(JID)) == 1


def test_traversal_is_rejected(tmp_path):
    with pytest.raises(SpoolPathError, match="escapes spool root"):
        safe_child(tmp_path, "journeys", "..", "..", "etc")


def test_symlink_component_is_rejected(tmp_path):
    root = tmp_path / "spool"
    (root / "journeys").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "journeys" / "sneaky").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SpoolPathError, match="symlink"):
        safe_child(root, "journeys", "sneaky")


def test_unusable_journey_id_rejected(tmp_path):
    s = spool(tmp_path)
    for bad in ("", "..", "a/b"):
        with pytest.raises(SpoolPathError):
            s.record(
                JourneyEvent(
                    journey_id=bad,
                    seq=0,
                    kind="message",
                    message=Message(role="user", content="x"),
                )
            )


# --------------------------------------------------------------------------
# Drain
# --------------------------------------------------------------------------


def test_push_sends_everything_and_advances_the_watermark(tmp_path):
    s = spool(tmp_path)
    s.record_all([ev(0), ev(1), ev(2)])
    sink = MemorySink()
    r = s.push(sink)
    assert r.pushed == 3 and r.ok
    assert s.watermark(JID) == 2
    assert [e.seq for e in sink.all_events] == [0, 1, 2]


def test_drain_with_nothing_pending_is_a_noop(tmp_path):
    s = spool(tmp_path)
    s.record(ev(0))
    sink = MemorySink()
    s.push(sink)
    again = s.push(sink)
    assert again == DrainResult()
    assert len(sink.batches) == 1  # no second network call


def test_failed_drain_leaves_the_shard_and_watermark_intact(tmp_path):
    s = spool(tmp_path)
    s.record_all([ev(0), ev(1)])
    bad = FailingSink()
    r = s.push(bad)
    assert r.failed == 2 and not r.ok
    assert "sink down" in r.errors[0]
    assert s.watermark(JID) is None  # not advanced
    assert len(s.read(JID)) == 2  # shard retained — it IS the queue
    # And the retry sends the same events.
    good = MemorySink()
    assert s.push(good).pushed == 2


def test_drain_failure_is_never_silent(tmp_path):
    s = spool(tmp_path)
    s.record(ev(0))
    r = s.push(FailingSink())
    assert r.errors and r.failed == 1


def test_double_drain_is_harmless(tmp_path):
    s = spool(tmp_path)
    s.record_all([ev(0), ev(1)])
    sink = MemorySink()
    s.push(sink)
    s.push(sink)
    assert [e.seq for e in sink.all_events] == [0, 1]  # not duplicated


def test_resume_transmits_only_the_tail(tmp_path):
    s = spool(tmp_path)
    s.record_all([ev(i) for i in range(6)])
    sink = MemorySink()
    s.push(sink)
    s.record_all([ev(i) for i in range(6, 10)])
    sink2 = MemorySink()
    r = s.push(sink2)
    assert r.pushed == 4
    assert [e.seq for e in sink2.all_events] == [6, 7, 8, 9]


def test_gap_is_reported_by_the_drain(tmp_path):
    s = spool(tmp_path)
    s.record_all([ev(0), ev(1), ev(3)])
    r = s.push(MemorySink())
    assert r.gaps == {JID: [2]}


def test_drain_covers_every_journey_by_default(tmp_path):
    s = spool(tmp_path)
    s.record(ev(0))
    s.record(
        JourneyEvent(
            journey_id="j_other",
            seq=0,
            kind="terminal",
            event_id="o0",
            terminal=Terminal(),
        )
    )
    sink = MemorySink()
    r = drain(s, sink)
    assert sorted(r.journeys) == ["j_other", JID]


def test_drain_can_target_one_journey(tmp_path):
    s = spool(tmp_path)
    s.record(ev(0))
    s.record(
        JourneyEvent(
            journey_id="j_other",
            seq=0,
            kind="terminal",
            event_id="o0",
            terminal=Terminal(),
        )
    )
    r = drain(s, MemorySink(), journey_id=JID)
    assert r.journeys == [JID]
    assert s.watermark("j_other") is None


# --------------------------------------------------------------------------
# Interval trigger
# --------------------------------------------------------------------------


def test_interval_bounds_are_enforced_not_clamped():
    assert validate_interval(60.0) == 60.0
    for bad in (0.0, 0.5, 3600.1, 100000):
        with pytest.raises(ValueError, match="outside"):
            validate_interval(bad)


def test_interval_drainer_uses_the_same_drain_path(tmp_path):
    s = spool(tmp_path)
    s.record_all([ev(0), ev(1)])
    sink = MemorySink()
    d = IntervalDrainer(s, sink, interval_seconds=1.0)
    d.start()
    try:
        deadline = 8.0
        waited = 0.0
        while waited < deadline and not sink.batches:
            threading.Event().wait(0.2)
            waited += 0.2
    finally:
        d.stop()
    assert [e.seq for e in sink.all_events] == [0, 1]
    assert s.watermark(JID) == 1


def test_drainer_rejects_double_start(tmp_path):
    d = IntervalDrainer(spool(tmp_path), MemorySink(), interval_seconds=3600.0)
    d.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            d.start()
    finally:
        d.stop()


# --------------------------------------------------------------------------
# Cached shard handles
#
# record() used to rediscover the filesystem on every event — mkdir, two
# resolve()s, a directory glob and three stats, all inside the global lock. That
# was 94% of its cost and, worse, 94% of the time the lock was held. The handle
# cache removes it. These tests pin the behaviour that must survive the cache.
# --------------------------------------------------------------------------


def test_record_is_fast_enough_for_a_capture_hot_path(tmp_path):
    """Regression guard, not a benchmark. The threshold is deliberately loose.

    Measured p50 is ~23us; the old rediscover-every-event path was ~196us. 90us
    catches a return to per-event syscalls without flaking on a busy CI box.
    """
    import time

    s = spool(tmp_path)
    for i in range(50):  # warm: first event pays the cold path
        s.record(ev(i))

    samples = []
    for i in range(50, 550):
        start = time.perf_counter_ns()
        s.record(ev(i))
        samples.append(time.perf_counter_ns() - start)
    samples.sort()
    p50_us = samples[len(samples) // 2] / 1000
    assert p50_us < 90, f"record() p50 regressed to {p50_us:.1f}us"


def test_the_handle_is_reused_across_events(tmp_path):
    s = spool(tmp_path)
    s.record_all([ev(i) for i in range(5)])
    assert s.open_shard_count() == 1


def test_close_releases_the_handle_without_losing_data(tmp_path):
    s = spool(tmp_path)
    s.record_all([ev(i) for i in range(3)])
    s.close(JID)
    assert s.open_shard_count() == 0
    assert [e.seq for e in s.read(JID)] == [0, 1, 2]


def test_recording_after_a_close_reopens(tmp_path):
    s = spool(tmp_path)
    s.record(ev(0))
    s.close()
    s.record(ev(1))
    assert [e.seq for e in s.read(JID)] == [0, 1]
    # Reopening must not write a second header into the same shard.
    assert s.shards(JID)[0].read_text().count("odyssey_schema_version") == 1


def test_open_handles_are_capped(tmp_path):
    """One fd per active journey would exhaust the process limit at scale."""
    s = spool(tmp_path, max_open_shards=4)
    for j in range(12):
        s.record(
            JourneyEvent(
                journey_id=f"j{j}",
                seq=0,
                kind="message",
                event_id=f"x{j}",
                message=Message(role="assistant", content="x"),
            )
        )
    assert s.open_shard_count() == 4
    # Eviction closes a handle; it never loses what was already flushed.
    for j in range(12):
        assert len(s.read(f"j{j}")) == 1


def test_zero_open_shards_rejected(tmp_path):
    with pytest.raises(ValueError, match="max_open_shards"):
        SpoolConfig(root=tmp_path, max_open_shards=0)


def test_rotation_still_happens_with_a_cached_handle(tmp_path):
    s = spool(tmp_path, max_shard_bytes=400)
    s.record_all([ev(i) for i in range(20)])
    assert len(s.shards(JID)) > 1
    assert s.open_shard_count() == 1  # only the newest stays open
    assert [e.seq for e in s.read(JID)] == list(range(20))
    for shard in s.shards(JID):
        assert shard.read_text().startswith('{"odyssey_schema_version"')


# --------------------------------------------------------------------------
# highest_seq — what seeds the allocator after a restart
# --------------------------------------------------------------------------


def test_highest_seq_on_an_unknown_journey_is_none(tmp_path):
    assert spool(tmp_path).highest_seq("never-seen") is None


def test_highest_seq_finds_the_maximum(tmp_path):
    s = spool(tmp_path)
    s.record_all([ev(i) for i in range(7)])
    assert s.highest_seq(JID) == 6


def test_highest_seq_reads_across_a_rotation(tmp_path):
    s = spool(tmp_path, max_shard_bytes=400)
    s.record_all([ev(i) for i in range(20)])
    assert len(s.shards(JID)) > 1
    assert s.highest_seq(JID) == 19


def test_highest_seq_survives_an_unreadable_shard(tmp_path):
    """A half-written shard must not stop a restart from resuming."""
    s = spool(tmp_path)
    s.record_all([ev(i) for i in range(3)])
    s.close()
    (s.root / "journeys" / JID / "001.jsonl").write_text("not json at all\n")
    assert s.highest_seq(JID) == 2


# --------------------------------------------------------------------------
# The shard header: a file that can say what it is a recording of
# --------------------------------------------------------------------------


HEADER = JourneyHeader(
    journey_id=JID,
    data_source="livekit",
    trace_id="t_9",
    started_at="2026-01-01T00:00:00+00:00",
    journey_metadata={"tenant": "acme"},
)


def test_record_stamps_the_header_on_the_shard(tmp_path):
    s = spool(tmp_path)
    s.record(ev(0), header=HEADER)
    s.close()
    assert read_header(s.shards(JID)[0]) == HEADER


def test_every_rotated_shard_repeats_the_header(tmp_path):
    """Each shard is a standalone file a reader may be handed on its own.

    A rotated shard that inherited its identity from a sibling it never names is
    not readable without the sibling.
    """
    s = spool(tmp_path, max_shard_bytes=200)
    for i in range(20):
        s.record(ev(i, content="x" * 40), header=HEADER)
    s.close()
    shards = s.shards(JID)
    assert len(shards) > 1
    assert all(read_header(sh) == HEADER for sh in shards)


def test_the_first_header_wins(tmp_path):
    """A second, different header would mean two writers — which `writer_id`
    already detects and a silent overwrite here would help hide."""
    s = spool(tmp_path)
    s.record(ev(0), header=HEADER)
    s.record(ev(1), header=dataclasses.replace(HEADER, data_source="impostor"))
    s.close()
    assert read_header(s.shards(JID)[0]).data_source == "livekit"


def test_a_journey_recorded_without_a_header_still_writes_a_valid_file(tmp_path):
    s = spool(tmp_path)
    s.record(ev(0))
    s.close()
    h = read_header(s.shards(JID)[0])
    assert h.journey_id is None
    assert s.read(JID) == [ev(0)]


def test_header_metadata_is_redacted(tmp_path):
    """Load-bearing, not symmetry.

    Journey tags used to ride on every event and so passed through
    `redact_event`. Now that the header carries them, skipping redaction would
    turn a dedup into a credential leak.
    """
    s = spool(tmp_path)
    leaky = dataclasses.replace(
        HEADER, journey_metadata={"tenant": "acme", "api_key": "sk-live-9"}
    )
    s.record(ev(0), header=leaky)
    s.close()
    meta = read_header(s.shards(JID)[0]).journey_metadata
    assert meta["api_key"] == REDACTED
    assert meta["tenant"] == "acme"


def test_redact_header_passes_through_when_there_is_nothing_to_mask():
    assert redact_header(HEADER, frozenset()) is HEADER
    assert redact_header(None, frozenset({"api_key"})) is None
    bare = JourneyHeader(journey_id="j")
    assert redact_header(bare, frozenset({"api_key"})) is bare


def test_spool_header_falls_back_to_disk_for_another_process(tmp_path):
    """A drain in a different process never saw the recorder that knew the
    identity — but the recorder wrote it down."""
    s = spool(tmp_path)
    s.record(ev(0), header=HEADER)
    s.close()  # drops the in-memory cache, like a finished journey
    assert s.header(JID) == HEADER


def test_spool_header_is_none_for_an_unknown_journey(tmp_path):
    assert spool(tmp_path).header("never_recorded") is None


def test_drain_hands_the_header_to_the_sink(tmp_path):
    """Otherwise identity is lost at exactly the hop that produces the artifact
    a trainer consumes."""
    s = spool(tmp_path)
    s.record(ev(0), header=HEADER)
    sink = MemorySink()
    s.push(sink)
    assert sink.headers == [HEADER]


def test_file_sink_reproduces_the_header_it_was_given(tmp_path):
    from odyssey.sinks import FileSink

    s = spool(tmp_path)
    s.record(ev(0), header=HEADER)
    s.record(ev(1), header=HEADER)
    out = tmp_path / "out"
    s.push(FileSink(out))
    assert read_header(out / f"{JID}.jsonl") == HEADER


def test_a_resumed_drain_does_not_write_a_second_header(tmp_path):
    from odyssey.sinks import FileSink

    s = spool(tmp_path)
    sink = FileSink(tmp_path / "out")
    s.record(ev(0), header=HEADER)
    s.push(sink)
    s.record(ev(1), header=HEADER)
    s.push(sink)
    text = (tmp_path / "out" / f"{JID}.jsonl").read_text()
    assert text.count("odyssey_schema_version") == 1
    assert len(read_events(tmp_path / "out" / f"{JID}.jsonl").events) == 2
