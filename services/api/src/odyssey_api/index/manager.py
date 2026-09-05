"""One IndexHandle per distinct ODYSSEY_DB_URI, lazily created on first
access and cached for the process's lifetime. See this module's note in
the implementation plan for why this replaces a literal
"block at server startup" hook -- services/api's create_app()/Settings
override pattern has no single point where the real settings are known
before the first request.
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Dict

from odyssey_store.db import connect

from odyssey_api.index.exports_indexer import index_exports
from odyssey_api.index.journeys_indexer import index_journeys
from odyssey_api.index.metrics_indexer import index_metrics
from odyssey_api.index.reconcile import reconcile
from odyssey_api.settings import Settings


class IndexHandle:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._run_pass(full_reconcile=False)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _run_pass(self, full_reconcile: bool) -> None:
        conn = connect(self._settings.db_uri)
        try:
            with self._lock:
                index_journeys(conn, self._settings.journeys_dir)
                index_metrics(conn, self._settings.journeys_dir)
                index_exports(conn, self._settings.exports_dir)
                if full_reconcile:
                    reconcile(conn)
        finally:
            conn.close()

    def _loop(self) -> None:
        cycles = 0
        while not self._stop_event.wait(self._settings.index_interval_seconds):
            cycles += 1
            full_reconcile = cycles % self._settings.index_reconcile_every == 0
            self._run_pass(full_reconcile=full_reconcile)

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        conn = connect(self._settings.db_uri)
        try:
            with self._lock:
                return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=5)


_registry: Dict[str, IndexHandle] = {}
_registry_lock = threading.Lock()


def get_index(settings: Settings) -> IndexHandle:
    with _registry_lock:
        handle = _registry.get(settings.db_uri)
        if handle is None:
            handle = IndexHandle(settings)
            _registry[settings.db_uri] = handle
        return handle


def reset_for_tests() -> None:
    """Test-only: stops and forgets every cached handle so each test gets
    a fresh index scoped to its own tmp_path settings."""
    with _registry_lock:
        for handle in _registry.values():
            handle.stop()
        _registry.clear()
