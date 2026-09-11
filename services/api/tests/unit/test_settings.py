from __future__ import annotations

from odyssey_api.settings import Settings


def test_db_uri_default():
    assert Settings().db_uri == "sqlite:///./odyssey.sqlite3"


def test_db_uri_from_env(monkeypatch):
    monkeypatch.setenv("ODYSSEY_DB_URI", "sqlite:///tmp/other.sqlite3")
    assert Settings().db_uri == "sqlite:///tmp/other.sqlite3"


def test_index_interval_default():
    assert Settings().index_interval_seconds == 5


def test_index_reconcile_every_default():
    assert Settings().index_reconcile_every == 20
