"""Opt-in, off-by-default server/host telemetry -- its own channel,
entirely independent of journey capture (see the design spec's "Metrics
channel" decision). Nothing in this module runs unless a caller
explicitly starts a :class:`MetricsReporter`; ``odyssey.init()`` only
does so when ``collect_metrics=True``.

Only stdlib-sourced fields: hostname, OS, CPU count, disk usage. Memory
is Linux-only (``/proc/meminfo``) and simply omitted elsewhere -- a
partial snapshot beats a failed one. The SDK never determines or reports
its own public IP; ``services/collector``'s ``POST /metrics`` handler
records that server-side, from the real TCP peer address.
"""

from __future__ import annotations

import gzip
import json
import os
import platform
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from odyssey.sinks import HttpSinkError, HttpTransport

MIN_METRICS_INTERVAL = 5.0
MAX_METRICS_INTERVAL = 86400.0


def _read_meminfo() -> Dict[str, int]:
    """Linux-only: total/available memory in bytes from ``/proc/meminfo``.
    Returns ``{}`` on any platform/condition where it can't be read."""
    path = Path("/proc/meminfo")
    if not path.exists():
        return {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return {}
    values: Dict[str, int] = {}
    for line in lines:
        if line.startswith("MemTotal:"):
            values["memory_total_bytes"] = int(line.split()[1]) * 1024
        elif line.startswith("MemAvailable:"):
            values["memory_available_bytes"] = int(line.split()[1]) * 1024
    return values


def build_snapshot(project: Optional[str] = None) -> Dict[str, Any]:
    """One point-in-time host snapshot. Never raises -- individual fields
    are simply omitted if their source is unavailable on this platform."""
    snapshot: Dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "hostname": platform.node(),
        "os": platform.platform(),
        "cpu_count": os.cpu_count(),
    }
    snapshot.update(_read_meminfo())
    try:
        usage = shutil.disk_usage(Path.cwd())
        snapshot["disk_total_bytes"] = usage.total
        snapshot["disk_free_bytes"] = usage.free
    except OSError:
        pass
    if project is not None:
        snapshot["project"] = project
    return snapshot


class _MetricsTransport(HttpTransport):
    """Posts one snapshot to ``{endpoint}/metrics``. Reuses HttpTransport's
    connection/gzip/backoff exactly as HttpSink does -- see
    odyssey/sinks.py."""

    def post(self, snapshot: Dict[str, Any]) -> None:
        self._check_backoff("metrics")

        payload = json.dumps(snapshot).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self.compress:
            payload = gzip.compress(payload)
            headers["Content-Encoding"] = "gzip"
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        path = f"{self._base_path}/metrics"
        with self._lock:
            status, retry_after, _body, error = self._request(path, payload, headers)

        if error is not None:
            raise HttpSinkError(f"metrics: could not reach {self.endpoint}: {error}")
        if status == 429:
            self._note_retry_after(retry_after)
            raise HttpSinkError(f"metrics: rate limited by {self.endpoint}")
        if status is None or status >= 300:
            raise HttpSinkError(f"metrics: HTTP {status} from {self.endpoint}")


class MetricsReporter:
    """Background thread that posts one :func:`build_snapshot` per
    ``interval_seconds``, modeled directly on
    ``odyssey.spool.IntervalDrainer`` -- same shape (daemon thread, sleeps
    the interval, wakes, does the one thing, repeats).

    Never raises out of the background loop (ADR 0004's "never crash the
    host") -- a failed post calls ``on_error`` if one was given, and tries
    again next interval. ``on_error`` is how ``Client`` wires this into
    its own error-counting (``note_error``) without this module depending
    on ``odyssey.client``.
    """

    def __init__(
        self,
        *,
        interval_seconds: float,
        project: Optional[str] = None,
        endpoint: Optional[str] = None,
        api_key: Optional[str] = None,
        on_error: Optional[Callable[[BaseException], None]] = None,
    ) -> None:
        # Not clamped to MIN_METRICS_INTERVAL/MAX_METRICS_INTERVAL here --
        # those are documentation of the intended production range;
        # callers (and tests) that pass a smaller/larger interval get
        # exactly what they asked for.
        self._interval = interval_seconds
        self._project = project
        self._on_error = on_error
        # A snapshot is a small, plain JSON object -- gzip's fixed overhead
        # isn't worth it here the way it is for a journey's event stream,
        # so this transport doesn't compress by default.
        self._transport = _MetricsTransport(endpoint, api_key=api_key, compress=False)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("metrics reporter already started")
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        self._transport.close()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._transport.post(build_snapshot(project=self._project))
            except Exception as exc:  # noqa: BLE001 - never crash the host
                if self._on_error is not None:
                    self._on_error(exc)
