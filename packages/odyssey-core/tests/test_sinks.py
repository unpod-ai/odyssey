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
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: object) -> None:  # keep test output quiet
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
# Connection reuse across journeys (item 1.7's "cross-journey overhead" fix
# — see HttpSink's docstring for why this replaces payload batching)
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
