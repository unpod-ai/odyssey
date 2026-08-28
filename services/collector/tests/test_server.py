"""odyssey-collector, against a real server on an ephemeral port.

The happy-path tests dogfood ``odyssey.HttpSink`` as the client — the exact
thing this server exists to receive — so a passing suite proves the two
projects' idea of the wire contract still agrees, not just that this file's
own assumptions about it are internally consistent.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest
from odyssey import HttpSink
from odyssey.jsonl import read_events
from odyssey.primitives import JourneyEvent, JourneyHeader, Message, Terminal
from odyssey.sinks import HttpSinkError

from odyssey_collector.server import CollectorConfig, _safe_stem, resolve_config, serve

JID = "j_collector"
FIXED_DATE = "2026-08-27"

HEADER = JourneyHeader(
    journey_id=JID,
    data_source="livekit",
    trace_id="t_1",
    started_at="2026-01-01T00:00:00+00:00",
)


def evs() -> list[JourneyEvent]:
    return [
        JourneyEvent(
            journey_id=JID,
            seq=0,
            kind="message",
            event_id="e0",
            message=Message(role="user", content="hi"),
        ),
        JourneyEvent(
            journey_id=JID,
            seq=1,
            kind="terminal",
            event_id="e1",
            terminal=Terminal(termination_reason="ENV_DONE"),
        ),
    ]


@pytest.fixture
def running(tmp_path):
    config = CollectorConfig(
        host="127.0.0.1",
        port=0,
        data_dir=tmp_path / "data",
        date_fn=lambda: FIXED_DATE,
    )
    server = serve(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join()


def endpoint(server) -> str:
    host, port = server.server_address
    return f"http://{host}:{port}"


def stored_path(server, journey_id: str = JID):
    return server.config.data_dir / FIXED_DATE / f"{_safe_stem(journey_id)}.jsonl"


# --------------------------------------------------------------------------
# The happy path, through the real client
# --------------------------------------------------------------------------


def test_health(running):
    with urllib.request.urlopen(f"{endpoint(running)}/health") as resp:
        assert resp.status == 200
        assert json.loads(resp.read()) == {"status": "ok"}


def test_a_batch_sent_via_httpsink_is_persisted_and_readable(running):
    sent = evs()
    HttpSink(endpoint(running)).send(JID, sent, header=HEADER)

    result = read_events(stored_path(running))
    assert result.clean
    assert result.events == sent
    assert result.header == HEADER


def test_a_second_drain_appends_without_a_second_header(running):
    HttpSink(endpoint(running)).send(JID, evs()[:1], header=HEADER)
    HttpSink(endpoint(running)).send(JID, evs()[1:], header=HEADER)

    raw = stored_path(running).read_text()
    assert raw.count("odyssey_schema_version") == 1
    assert [e.seq for e in read_events(stored_path(running)).events] == [0, 1]


def test_a_retried_batch_is_not_written_twice(running):
    """HttpSink retries on failure; a lost 200 must not double the raw layer."""
    sent = evs()
    HttpSink(endpoint(running)).send(JID, sent, header=HEADER)
    HttpSink(endpoint(running)).send(JID, sent, header=HEADER)  # the "retry"

    result = read_events(stored_path(running))
    assert result.events == sent
    raw = stored_path(running).read_text()
    assert raw.count("odyssey_schema_version") == 1


def test_a_partially_new_retried_batch_only_writes_the_new_events(running):
    HttpSink(endpoint(running)).send(JID, evs()[:1], header=HEADER)
    HttpSink(endpoint(running)).send(JID, evs(), header=HEADER)  # e0 repeats, e1 new

    result = read_events(stored_path(running))
    assert [e.event_id for e in result.events] == ["e0", "e1"]


def test_different_journeys_land_in_different_files(running):
    HttpSink(endpoint(running)).send("j_a", evs())
    HttpSink(endpoint(running)).send("j_b", evs())
    assert stored_path(running, "j_a").exists()
    assert stored_path(running, "j_b").exists()


# --------------------------------------------------------------------------
# Date partitioning — <data_dir>/<date>/<journey_id>.jsonl
# --------------------------------------------------------------------------


def test_a_batch_lands_under_the_date_it_was_received(running):
    HttpSink(endpoint(running)).send(JID, evs())
    assert (running.config.data_dir / FIXED_DATE / f"{JID}.jsonl").exists()
    # And nowhere else — no flat <data_dir>/<journey_id>.jsonl left behind.
    assert not (running.config.data_dir / f"{JID}.jsonl").exists()


def test_a_new_day_starts_a_fresh_date_directory(tmp_path):
    today = [FIXED_DATE]
    config = CollectorConfig(
        host="127.0.0.1", port=0, data_dir=tmp_path / "data", date_fn=lambda: today[0]
    )
    server = serve(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        HttpSink(endpoint(server)).send(JID, evs()[:1])
        today[0] = "2026-08-28"
        HttpSink(endpoint(server)).send(JID, evs()[1:])

        first_day = config.data_dir / FIXED_DATE / f"{JID}.jsonl"
        second_day = config.data_dir / "2026-08-28" / f"{JID}.jsonl"
        assert first_day.exists() and second_day.exists()
        assert [e.seq for e in read_events(first_day).events] == [0]
        assert [e.seq for e in read_events(second_day).events] == [1]
    finally:
        server.shutdown()
        thread.join()


def test_default_timezone_is_utc(tmp_path, monkeypatch):
    monkeypatch.delenv("ODYSSEY_COLLECTOR_TIMEZONE", raising=False)
    from datetime import datetime, timezone

    config = CollectorConfig(host="127.0.0.1", port=0, data_dir=tmp_path / "data")
    server = serve(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        HttpSink(endpoint(server)).send(JID, evs())
        expected = datetime.now(timezone.utc).date().isoformat()
        assert (config.data_dir / expected / f"{JID}.jsonl").exists()
    finally:
        server.shutdown()
        thread.join()


def test_explicit_timezone_is_respected(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    config = CollectorConfig(
        host="127.0.0.1", port=0, data_dir=tmp_path / "data", timezone="Asia/Kolkata"
    )
    server = serve(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        HttpSink(endpoint(server)).send(JID, evs())
        expected = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
        assert (config.data_dir / expected / f"{JID}.jsonl").exists()
    finally:
        server.shutdown()
        thread.join()


def test_the_timezone_env_var_is_respected(tmp_path, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    monkeypatch.setenv("ODYSSEY_COLLECTOR_TIMEZONE", "Asia/Kolkata")
    config = resolve_config(host="127.0.0.1", port=0, data_dir=tmp_path / "data")
    server = serve(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        HttpSink(endpoint(server)).send(JID, evs())
        expected = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
        assert (config.data_dir / expected / f"{JID}.jsonl").exists()
    finally:
        server.shutdown()
        thread.join()


def test_an_unknown_timezone_falls_back_to_utc(tmp_path):
    from datetime import datetime, timezone

    config = CollectorConfig(
        host="127.0.0.1", port=0, data_dir=tmp_path / "data", timezone="Not/A_Real_Zone"
    )
    server = serve(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        HttpSink(endpoint(server)).send(JID, evs())
        expected = datetime.now(timezone.utc).date().isoformat()
        assert (config.data_dir / expected / f"{JID}.jsonl").exists()
    finally:
        server.shutdown()
        thread.join()


def test_a_traversal_journey_id_cannot_escape_data_dir(running):
    """A journey_id is caller-chosen; nothing stops one holding a separator.
    Written naively, "../../etc/passwd" would escape data_dir entirely."""
    HttpSink(endpoint(running)).send("../../../etc/passwd", evs())

    escaped = (
        running.config.data_dir / ".." / ".." / ".." / "etc" / "passwd"
    ).resolve()
    assert not escaped.exists()

    written = list((running.config.data_dir / FIXED_DATE).glob("*.jsonl"))
    assert len(written) == 1
    assert written[0].resolve().is_relative_to(running.config.data_dir.resolve())
    assert "etc_passwd" in written[0].name


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


@pytest.fixture
def guarded(tmp_path):
    config = CollectorConfig(
        host="127.0.0.1",
        port=0,
        data_dir=tmp_path / "data",
        api_key="sk-collector",
        date_fn=lambda: FIXED_DATE,
    )
    server = serve(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join()


def test_a_missing_key_is_rejected_and_nothing_is_written(guarded):
    with pytest.raises(HttpSinkError, match="HTTP 401"):
        HttpSink(endpoint(guarded)).send(JID, evs())
    assert not stored_path(guarded).exists()


def test_the_correct_key_is_accepted(guarded):
    sent = evs()
    HttpSink(endpoint(guarded), api_key="sk-collector").send(JID, sent)
    assert read_events(stored_path(guarded)).events == sent


def test_the_wrong_key_is_rejected(guarded):
    with pytest.raises(HttpSinkError, match="HTTP 401"):
        HttpSink(endpoint(guarded), api_key="sk-wrong").send(JID, evs())


# --------------------------------------------------------------------------
# Malformed input — a validating ingest point, not a dumb pipe
# --------------------------------------------------------------------------


def test_a_bad_gzip_body_is_rejected_with_400(running):
    request = urllib.request.Request(
        f"{endpoint(running)}/journeys/{JID}/events",
        data=b"not actually gzip",
        method="POST",
        headers={"Content-Encoding": "gzip"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(request)
    assert exc_info.value.code == 400
    assert not stored_path(running).exists()


def test_an_uncompressed_body_is_still_accepted(running):
    """compress=False callers (or anything predating item 1.7) still work —
    Content-Encoding is optional, not required."""
    sent = evs()
    HttpSink(endpoint(running), compress=False).send(JID, sent, header=HEADER)
    result = read_events(stored_path(running))
    assert result.clean
    assert result.events == sent


def test_a_malformed_body_is_rejected_with_400_and_nothing_is_written(running):
    request = urllib.request.Request(
        f"{endpoint(running)}/journeys/{JID}/events",
        data=b"not an odyssey header at all\n",
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(request)
    assert exc_info.value.code == 400
    assert not stored_path(running).exists()


def test_an_empty_journey_id_is_rejected(running):
    request = urllib.request.Request(
        f"{endpoint(running)}/journeys//events", data=b"x", method="POST"
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(request)
    assert exc_info.value.code == 400


def test_an_unrecognised_path_is_404(running):
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(f"{endpoint(running)}/nonsense")
    assert exc_info.value.code == 404
