"""Spool: local append, crash safety, redaction, watermarks, drain semantics."""

from __future__ import annotations

import json
import subprocess
import sys
import threading

import pytest

from odyssey.primitives import JourneyEvent, Message, Terminal, ToolCall, ToolResponse
from odyssey.spool import (
    REDACTED,
    DrainResult,
    IntervalDrainer,
    Spool,
    SpoolConfig,
    SpoolPathError,
    drain,
    redact_event,
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

    def send(self, journey_id, events):
        self.batches.append((journey_id, list(events)))

    @property
    def all_events(self):
        return [e for _, batch in self.batches for e in batch]


class FailingSink:
    def __init__(self, exc=RuntimeError("sink down")):
        self.exc = exc
        self.calls = 0

    def send(self, journey_id, events):
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
