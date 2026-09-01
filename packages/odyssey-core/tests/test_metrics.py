from __future__ import annotations

import gzip
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from odyssey.metrics import MetricsReporter, build_snapshot


def test_build_snapshot_has_the_stdlib_sourced_fields():
    snapshot = build_snapshot()
    assert "ts" in snapshot
    assert "hostname" in snapshot
    assert "os" in snapshot
    assert isinstance(snapshot["cpu_count"], int)
    assert "disk_total_bytes" in snapshot
    assert "disk_free_bytes" in snapshot
    assert "project" not in snapshot  # no project passed


def test_build_snapshot_includes_project_when_given():
    snapshot = build_snapshot(project="my-project")
    assert snapshot["project"] == "my-project"


class _CapturingHandler(BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        if self.headers.get("Content-Encoding", "").strip().lower() == "gzip":
            body = gzip.decompress(body)
        type(self).received.append(json.loads(body))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # keep test output quiet
        pass


@pytest.fixture
def capturing_server():
    _CapturingHandler.received = []
    server = HTTPServer(("127.0.0.1", 0), _CapturingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join()


def test_metrics_reporter_posts_on_the_configured_interval(capturing_server):
    host, port = capturing_server.server_address
    reporter = MetricsReporter(
        interval_seconds=0.05, endpoint=f"http://{host}:{port}", project="p"
    )
    reporter.start()
    try:
        time.sleep(0.2)
    finally:
        reporter.stop()
    assert len(_CapturingHandler.received) >= 2
    assert _CapturingHandler.received[0]["project"] == "p"


def test_metrics_reporter_never_raises_on_a_transport_failure():
    errors = []
    reporter = MetricsReporter(
        interval_seconds=0.05,
        endpoint="http://127.0.0.1:1",  # nothing listens here
        on_error=lambda exc: errors.append(exc),
    )
    reporter.start()
    try:
        time.sleep(0.2)
    finally:
        reporter.stop()
    assert len(errors) >= 1


def test_metrics_reporter_stop_is_idempotent(capturing_server):
    host, port = capturing_server.server_address
    reporter = MetricsReporter(interval_seconds=10, endpoint=f"http://{host}:{port}")
    reporter.start()
    reporter.stop()
    reporter.stop()  # must not raise


def test_metrics_reporter_rejects_zero_interval():
    with pytest.raises(ValueError):
        MetricsReporter(interval_seconds=0, endpoint="http://127.0.0.1:1")


def test_metrics_reporter_rejects_negative_interval():
    with pytest.raises(ValueError):
        MetricsReporter(interval_seconds=-1, endpoint="http://127.0.0.1:1")


from odyssey.config import resolve


def test_collect_metrics_defaults_to_false():
    assert resolve().collect_metrics is False


def test_collect_metrics_env_var(monkeypatch):
    monkeypatch.setenv("ODYSSEY_COLLECT_METRICS", "1")
    assert resolve().collect_metrics is True


def test_collect_metrics_explicit_beats_env(monkeypatch):
    monkeypatch.setenv("ODYSSEY_COLLECT_METRICS", "1")
    assert resolve(collect_metrics=False).collect_metrics is False


def test_metrics_interval_default_is_300(monkeypatch):
    monkeypatch.delenv("ODYSSEY_METRICS_INTERVAL", raising=False)
    assert resolve().metrics_interval == 300.0


def test_metrics_interval_env_var(monkeypatch):
    monkeypatch.setenv("ODYSSEY_METRICS_INTERVAL", "60")
    assert resolve().metrics_interval == 60.0


import odyssey


def test_init_with_collect_metrics_starts_a_reporter(tmp_path, monkeypatch):
    monkeypatch.setenv("ODYSSEY_ENDPOINT", "http://127.0.0.1:1")  # never has to connect
    client = odyssey.init(
        spool_dir=tmp_path / "spool",
        out_dir=tmp_path / "out",
        collect_metrics=True,
        metrics_interval=5,
        drain_interval=None,
        force=True,
    )
    try:
        assert client.metrics_reporter is not None
    finally:
        client.shutdown()


def test_init_without_collect_metrics_starts_no_reporter(tmp_path):
    client = odyssey.init(
        spool_dir=tmp_path / "spool",
        out_dir=tmp_path / "out",
        drain_interval=None,
        force=True,
    )
    try:
        assert client.metrics_reporter is None
    finally:
        client.shutdown()


def test_shutdown_stops_the_metrics_reporter(tmp_path, monkeypatch):
    monkeypatch.setenv("ODYSSEY_ENDPOINT", "http://127.0.0.1:1")
    client = odyssey.init(
        spool_dir=tmp_path / "spool",
        out_dir=tmp_path / "out",
        collect_metrics=True,
        drain_interval=None,
        force=True,
    )
    reporter = client.metrics_reporter
    client.shutdown()
    assert reporter is not None
    assert reporter._thread is None  # stop() cleared it
