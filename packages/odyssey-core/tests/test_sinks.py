"""HttpSink: the network destination, over stdlib-only HTTP.

A real local HTTP server (``http.server``, stdlib) rather than a mocked
``urlopen`` — the same bar the rest of this suite holds for I/O-adjacent code
(``test_spool.py`` SIGKILLs a real child process). Mocking ``urlopen`` would
only prove the mock was called correctly, not that a byte stream compatible
with ``read_events`` actually goes over the wire.
"""

from __future__ import annotations

import gzip
import http.server
import json
import threading
import time

import pytest

from odyssey.jsonl import read_events
from odyssey.primitives import JourneyEvent, JourneyHeader, Message, Terminal
from odyssey.sinks import ENV_API_KEY, ENV_ENDPOINT, HttpSink, HttpSinkError
from odyssey.spool import Spool, SpoolConfig

JID = "j_http"

HEADER = JourneyHeader(
    journey_id=JID,
    data_source="livekit",
    trace_id="t_9",
    started_at="2026-01-01T00:00:00+00:00",
    journey_metadata={"tenant": "acme"},
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


class _CapturingHandler(http.server.BaseHTTPRequestHandler):
    # HTTP/1.1 + Content-Length (below) is what makes keep-alive reuse
    # observable in tests, matching services/collector's own opt-in.
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - stdlib method name
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.server.requests.append(  # type: ignore[attr-defined]
            {
                "path": self.path,
                "body": body,
                "headers": dict(self.headers),
            }
        )
        self.send_response(self.server.status_to_return)  # type: ignore[attr-defined]
        retry_after = getattr(self.server, "retry_after", None)
        if retry_after is not None:
            self.send_header("Retry-After", retry_after)
        response_body = getattr(self.server, "batch_response", None)
        if response_body is not None:
            data = response_body
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # keep test output quiet
        pass


class _CountingServer(http.server.HTTPServer):
    """Counts distinct accepted TCP connections, not HTTP requests --
    ``get_request()`` fires once per connection, and a keep-alive client
    serves many requests over one, so this is what actually distinguishes
    "one connection reused" from "one connection per request"."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.accepted_connections = 0

    def get_request(self):  # type: ignore[override]
        self.accepted_connections += 1
        return super().get_request()


@pytest.fixture
def server():
    srv = _CountingServer(("127.0.0.1", 0), _CapturingHandler)
    srv.requests = []  # type: ignore[attr-defined]
    srv.status_to_return = 200  # type: ignore[attr-defined]
    srv.retry_after = None  # type: ignore[attr-defined]
    srv.batch_response = None  # type: ignore[attr-defined]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        thread.join()


def endpoint(srv) -> str:
    host, port = srv.server_address
    return f"http://{host}:{port}"


# --------------------------------------------------------------------------
# Construction
# --------------------------------------------------------------------------


def test_endpoint_is_required_one_way_or_another():
    with pytest.raises(ValueError, match="needs an endpoint"):
        HttpSink()


def test_explicit_endpoint_is_used():
    sink = HttpSink("http://example.invalid:9/")
    assert sink.endpoint == "http://example.invalid:9"  # trailing slash trimmed


def test_env_var_supplies_the_endpoint(monkeypatch):
    monkeypatch.setenv(ENV_ENDPOINT, "http://from-env:8080")
    assert HttpSink().endpoint == "http://from-env:8080"


def test_explicit_endpoint_beats_the_environment(monkeypatch):
    monkeypatch.setenv(ENV_ENDPOINT, "http://from-env:8080")
    assert HttpSink("http://explicit:1").endpoint == "http://explicit:1"


def test_env_var_supplies_the_api_key(monkeypatch):
    monkeypatch.setenv(ENV_API_KEY, "sk-env")
    assert HttpSink("http://x").api_key == "sk-env"


def test_explicit_api_key_beats_the_environment(monkeypatch):
    monkeypatch.setenv(ENV_API_KEY, "sk-env")
    assert HttpSink("http://x", api_key="sk-explicit").api_key == "sk-explicit"


def test_no_api_key_anywhere_is_none(monkeypatch):
    monkeypatch.delenv(ENV_API_KEY, raising=False)
    assert HttpSink("http://x").api_key is None


# --------------------------------------------------------------------------
# send() — the wire format
# --------------------------------------------------------------------------


def test_send_posts_to_the_journey_scoped_path(server):
    HttpSink(endpoint(server)).send(JID, evs(), header=HEADER)
    assert server.requests[0]["path"] == f"/journeys/{JID}/events"


def test_send_url_escapes_the_journey_id(server):
    HttpSink(endpoint(server)).send("room/42 x", evs())
    assert server.requests[0]["path"] == "/journeys/room%2F42%20x/events"


def test_send_body_round_trips_through_read_events(server, tmp_path):
    """The (gzipped, by default) body decompresses to exactly what a shard on
    disk would hold."""
    sent = evs()
    HttpSink(endpoint(server)).send(JID, sent, header=HEADER)
    body = gzip.decompress(server.requests[0]["body"])

    p = tmp_path / "received.jsonl"
    p.write_bytes(body)
    result = read_events(p)
    assert result.clean
    assert result.events == sent
    assert result.header == HEADER


def test_send_sets_the_content_type(server):
    HttpSink(endpoint(server)).send(JID, evs())
    assert "x-ndjson" in server.requests[0]["headers"]["Content-Type"]


def test_no_api_key_means_no_authorization_header(server):
    HttpSink(endpoint(server)).send(JID, evs())
    assert "Authorization" not in server.requests[0]["headers"]


def test_the_api_key_is_sent_as_a_bearer_token(server):
    HttpSink(endpoint(server), api_key="sk-live-1").send(JID, evs())
    assert server.requests[0]["headers"]["Authorization"] == "Bearer sk-live-1"


def test_send_works_with_no_header(server):
    """A v1.0-shaped caller with no header to declare still gets a valid batch."""
    HttpSink(endpoint(server)).send(JID, evs())
    body = gzip.decompress(server.requests[0]["body"])
    assert body.startswith(b'{"odyssey_schema_version"')


# --------------------------------------------------------------------------
# Failure — retryable, per the Sink contract
# --------------------------------------------------------------------------


def test_a_non_2xx_status_raises(server):
    server.status_to_return = 500
    with pytest.raises(HttpSinkError, match=f"{JID}.*HTTP 500"):
        HttpSink(endpoint(server)).send(JID, evs())


def test_a_connection_failure_raises():
    """Nobody listening on this port — a transport failure, not a bad status."""
    with pytest.raises(HttpSinkError, match="could not reach"):
        HttpSink("http://127.0.0.1:1").send(JID, evs())


# --------------------------------------------------------------------------
# Compression and backpressure (item 1.7)
# --------------------------------------------------------------------------


def test_send_gzips_the_body_and_sets_content_encoding_by_default(server):
    HttpSink(endpoint(server)).send(JID, evs())
    req = server.requests[0]
    assert req["headers"]["Content-Encoding"] == "gzip"
    # A real gzip stream, not just the header claiming to be one.
    gzip.decompress(req["body"])


def test_compress_false_sends_a_plain_uncompressed_body(server):
    HttpSink(endpoint(server), compress=False).send(JID, evs())
    req = server.requests[0]
    assert "Content-Encoding" not in req["headers"]
    assert req["body"].startswith(b'{"odyssey_schema_version"')


def test_a_429_sets_a_retry_after_backoff(server):
    server.status_to_return = 429
    server.retry_after = "1"
    sink = HttpSink(endpoint(server))
    with pytest.raises(HttpSinkError, match="HTTP 429"):
        sink.send(JID, evs())

    # The next attempt is refused locally -- no second request reaches the
    # server before the Retry-After window elapses.
    with pytest.raises(HttpSinkError, match="backing off"):
        sink.send(JID, evs())
    assert len(server.requests) == 1


def test_retry_after_backoff_expires(server):
    server.status_to_return = 429
    server.retry_after = "0"
    sink = HttpSink(endpoint(server))
    with pytest.raises(HttpSinkError):
        sink.send(JID, evs())

    time.sleep(0.05)
    server.status_to_return = 200
    sink.send(JID, evs())  # does not raise -- backoff already elapsed
    assert len(server.requests) == 2


def test_a_malformed_retry_after_still_backs_off(server):
    server.status_to_return = 429
    server.retry_after = "not-a-number-or-a-date"
    sink = HttpSink(endpoint(server))
    with pytest.raises(HttpSinkError, match="HTTP 429"):
        sink.send(JID, evs())
    with pytest.raises(HttpSinkError, match="backing off"):
        sink.send(JID, evs())


# --------------------------------------------------------------------------
# Through the drain — the path a real deployment actually takes
# --------------------------------------------------------------------------


def test_drain_advances_the_watermark_on_a_successful_post(tmp_path, server):
    spool = Spool(SpoolConfig(root=tmp_path / "spool"))
    spool.record_all(evs(), header=HEADER)

    result = spool.push(HttpSink(endpoint(server)))
    assert result.ok and result.pushed == 2
    assert spool.watermark(JID) == 1
    assert len(server.requests) == 1


def test_drain_leaves_the_watermark_untouched_on_a_failed_post(tmp_path, server):
    server.status_to_return = 503
    spool = Spool(SpoolConfig(root=tmp_path / "spool"))
    spool.record_all(evs(), header=HEADER)

    result = spool.push(HttpSink(endpoint(server)))
    assert not result.ok and result.failed == 2
    assert spool.watermark(JID) is None
    assert len(spool.undrained(JID)) == 2  # still queued for the next drain

    server.status_to_return = 200
    retry = spool.push(HttpSink(endpoint(server)))
    assert retry.ok and retry.pushed == 2
    assert spool.watermark(JID) == 1


# --------------------------------------------------------------------------
# Connection reuse across journeys (item 1.7's overhead fix)
# --------------------------------------------------------------------------


def test_two_sends_on_the_same_sink_reuse_one_connection(server):
    sink = HttpSink(endpoint(server))
    sink.send("j_a", evs())
    sink.send("j_b", evs())

    assert len(server.requests) == 2
    assert server.accepted_connections == 1


def test_two_sends_on_two_different_sinks_use_two_connections(server):
    HttpSink(endpoint(server)).send("j_a", evs())
    HttpSink(endpoint(server)).send("j_b", evs())

    assert server.accepted_connections == 2


def test_close_releases_the_connection_and_a_later_send_reconnects(server):
    sink = HttpSink(endpoint(server))
    sink.send("j_a", evs())
    sink.close()
    sink.send("j_b", evs())

    assert server.accepted_connections == 2


def test_close_before_any_send_is_a_safe_no_op(server):
    HttpSink(endpoint(server)).close()  # must not raise


def test_a_dropped_keep_alive_connection_is_retried_transparently(server):
    """The server closing an idle connection out from under the client is
    normal keep-alive behaviour (an idle timeout, a restart, ...) -- the
    next send() must not surface that as a spurious drain failure."""
    sink = HttpSink(endpoint(server))
    sink.send("j_a", evs())

    # Simulate the server having dropped the connection: close the socket
    # the client thinks is still open, without telling HttpSink.
    sink._conn.sock.close()  # type: ignore[union-attr]

    sink.send("j_b", evs())  # must transparently reconnect, not raise
    assert len(server.requests) == 2


# --------------------------------------------------------------------------
# send_batch() — cross-journey payload batching (item 1.7)
# --------------------------------------------------------------------------


def test_send_batch_posts_one_request_to_the_batch_path(server):
    server.batch_response = json.dumps(
        {"results": {"j_a": {"ok": True}, "j_b": {"ok": True}}}
    ).encode()

    sink = HttpSink(endpoint(server))
    result = sink.send_batch([("j_a", evs(), HEADER), ("j_b", evs(), HEADER)])

    assert len(server.requests) == 1
    assert server.requests[0]["path"] == "/batch/events"
    assert result == {"j_a": None, "j_b": None}


def test_send_batch_body_carries_every_journeys_own_header_and_events(server):
    server.batch_response = json.dumps(
        {"results": {"j_a": {"ok": True}, "j_b": {"ok": True}}}
    ).encode()
    sink = HttpSink(endpoint(server), compress=False)
    sink.send_batch([("j_a", evs(), HEADER), ("j_b", evs()[:1], None)])

    body = json.loads(server.requests[0]["body"])
    journeys = body["journeys"]
    assert set(journeys) == {"j_a", "j_b"}

    # Each journey's blob is byte-identical to what a lone send() would post.
    lines_a = journeys["j_a"].strip("\n").split("\n")
    assert json.loads(lines_a[0])["journey_id"] == HEADER.journey_id
    assert len(lines_a) == 1 + len(evs())

    lines_b = journeys["j_b"].strip("\n").split("\n")
    assert "journey_id" not in json.loads(lines_b[0])  # no header -> bare v1.0 line
    assert len(lines_b) == 1 + 1


def test_send_batch_reports_a_per_journey_rejection(server):
    server.batch_response = json.dumps(
        {
            "results": {
                "j_a": {"ok": True},
                "j_b": {"ok": False, "error": "malformed batch: 1 rejected line(s)"},
            }
        }
    ).encode()

    result = HttpSink(endpoint(server)).send_batch(
        [("j_a", evs(), HEADER), ("j_b", evs(), HEADER)]
    )
    assert result["j_a"] is None
    assert result["j_b"] == "malformed batch: 1 rejected line(s)"


def test_send_batch_raises_when_the_whole_request_fails():
    with pytest.raises(HttpSinkError, match="batch of 1"):
        HttpSink("http://127.0.0.1:1").send_batch([("j_a", evs(), HEADER)])


def test_send_batch_raises_on_a_non_200_status(server):
    server.status_to_return = 500
    with pytest.raises(HttpSinkError, match="HTTP 500"):
        HttpSink(endpoint(server)).send_batch([("j_a", evs(), HEADER)])


def test_send_batch_raises_on_a_malformed_response_body(server):
    server.batch_response = b"not json at all"
    with pytest.raises(HttpSinkError, match="malformed response"):
        HttpSink(endpoint(server)).send_batch([("j_a", evs(), HEADER)])


def test_send_batch_treats_a_missing_journey_result_as_a_failure(server):
    server.batch_response = json.dumps({"results": {"j_a": {"ok": True}}}).encode()
    result = HttpSink(endpoint(server)).send_batch(
        [("j_a", evs(), HEADER), ("j_b", evs(), HEADER)]
    )
    assert result["j_a"] is None
    assert "no result" in (result["j_b"] or "")


def test_send_batch_honours_the_429_backoff_window_like_send(server):
    server.status_to_return = 429
    server.retry_after = "60"
    sink = HttpSink(endpoint(server))
    with pytest.raises(HttpSinkError):
        sink.send_batch([("j_a", evs(), HEADER)])

    server.status_to_return = 200
    server.batch_response = json.dumps({"results": {"j_a": {"ok": True}}}).encode()
    with pytest.raises(HttpSinkError, match="backing off"):
        sink.send_batch([("j_a", evs(), HEADER)])


# --------------------------------------------------------------------------
# drain()'s batch_size path, against HttpSink.send_batch
# --------------------------------------------------------------------------


def test_drain_with_batch_size_sends_one_request_for_multiple_journeys(
    server, tmp_path
):
    server.batch_response = json.dumps(
        {"results": {"j_a": {"ok": True}, "j_b": {"ok": True}}}
    ).encode()
    spool = Spool(SpoolConfig(root=tmp_path / "spool"))
    spool.record_all(
        [
            JourneyEvent(
                journey_id="j_a",
                seq=0,
                kind="terminal",
                event_id="ea",
                terminal=Terminal(termination_reason="ENV_DONE"),
            )
        ]
    )
    spool.record_all(
        [
            JourneyEvent(
                journey_id="j_b",
                seq=0,
                kind="terminal",
                event_id="eb",
                terminal=Terminal(termination_reason="ENV_DONE"),
            )
        ]
    )

    result = spool.push(HttpSink(endpoint(server)), batch_size=10)
    assert result.ok
    assert len(server.requests) == 1
    assert spool.watermark("j_a") == 0
    assert spool.watermark("j_b") == 0


def test_drain_with_batch_size_advances_only_the_journeys_the_server_accepted(
    server, tmp_path
):
    server.batch_response = json.dumps(
        {
            "results": {
                "j_a": {"ok": True},
                "j_b": {"ok": False, "error": "boom"},
            }
        }
    ).encode()
    spool = Spool(SpoolConfig(root=tmp_path / "spool"))
    spool.record_all(
        [
            JourneyEvent(
                journey_id="j_a",
                seq=0,
                kind="terminal",
                event_id="ea",
                terminal=Terminal(termination_reason="ENV_DONE"),
            )
        ]
    )
    spool.record_all(
        [
            JourneyEvent(
                journey_id="j_b",
                seq=0,
                kind="terminal",
                event_id="eb",
                terminal=Terminal(termination_reason="ENV_DONE"),
            )
        ]
    )

    result = spool.push(HttpSink(endpoint(server)), batch_size=10)
    assert not result.ok
    assert spool.watermark("j_a") == 0
    assert spool.watermark("j_b") is None


def test_drain_batch_size_1_never_calls_send_batch(server, tmp_path):
    """Default behaviour (batch_size=1) is byte-for-byte the pre-1.7-batching
    path -- send_batch must never be invoked."""

    class TrackingSink(HttpSink):
        def send_batch(self, items):  # type: ignore[override]
            raise AssertionError("send_batch must not be called at batch_size=1")

    spool = Spool(SpoolConfig(root=tmp_path / "spool"))
    spool.record_all(evs(), header=HEADER)
    result = spool.push(TrackingSink(endpoint(server)))
    assert result.ok
