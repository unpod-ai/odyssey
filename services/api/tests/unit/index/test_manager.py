from __future__ import annotations

import time

import pytest
from odyssey.jsonl import write_events
from odyssey.primitives import JourneyEvent, JourneyHeader, Message, Terminal

from odyssey_api.index import manager
from odyssey_api.settings import Settings


def _write_journey(journeys_dir, jid):
    date_dir = journeys_dir / "2026-08-28"
    date_dir.mkdir(parents=True, exist_ok=True)
    write_events(
        date_dir / f"{jid}.jsonl",
        [
            JourneyEvent(journey_id=jid, seq=0, kind="message", event_id="e0", message=Message(role="user", content="hi")),
            JourneyEvent(journey_id=jid, seq=1, kind="terminal", event_id="e1", terminal=Terminal(termination_reason="ENV_DONE")),
        ],
        header=JourneyHeader(journey_id=jid, data_source="livekit"),
    )


def test_get_index_runs_full_pass_before_returning(tmp_path):
    manager.reset_for_tests()
    journeys_dir = tmp_path / "journeys"
    _write_journey(journeys_dir, "j1")
    settings = Settings(journeys_dir=journeys_dir, db_uri=f"sqlite:///{tmp_path}/db.sqlite3", index_interval_seconds=3600)

    handle = manager.get_index(settings)

    rows = handle.query("SELECT journey_id FROM journeys")
    assert [r["journey_id"] for r in rows] == ["j1"]
    handle.stop()


def test_get_index_returns_same_handle_for_same_settings(tmp_path):
    manager.reset_for_tests()
    settings = Settings(journeys_dir=tmp_path / "journeys", db_uri=f"sqlite:///{tmp_path}/db.sqlite3", index_interval_seconds=3600)

    first = manager.get_index(settings)
    second = manager.get_index(settings)

    assert first is second
    first.stop()


def test_background_worker_picks_up_new_journey(tmp_path):
    manager.reset_for_tests()
    journeys_dir = tmp_path / "journeys"
    journeys_dir.mkdir()
    settings = Settings(journeys_dir=journeys_dir, db_uri=f"sqlite:///{tmp_path}/db.sqlite3", index_interval_seconds=1)

    handle = manager.get_index(settings)
    assert handle.query("SELECT COUNT(*) AS n FROM journeys")[0]["n"] == 0

    _write_journey(journeys_dir, "j2")
    time.sleep(2.5)  # give the background thread at least one cycle

    rows = handle.query("SELECT journey_id FROM journeys")
    assert [r["journey_id"] for r in rows] == ["j2"]
    handle.stop()


def test_background_worker_survives_exception_in_pass(tmp_path, monkeypatch):
    """Test that an exception in _run_pass is caught, logged, and the loop continues."""
    manager.reset_for_tests()
    journeys_dir = tmp_path / "journeys"
    _write_journey(journeys_dir, "j3")
    settings = Settings(journeys_dir=journeys_dir, db_uri=f"sqlite:///{tmp_path}/db.sqlite3", index_interval_seconds=1)

    handle = manager.get_index(settings)
    initial_count = handle.query("SELECT COUNT(*) AS n FROM journeys")[0]["n"]
    assert initial_count == 1

    # Inject a failure into the next pass
    from odyssey_api.index import journeys_indexer
    original_index = journeys_indexer.index_journeys
    call_count = [0]

    def failing_index_journeys(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("Simulated indexing failure")
        return original_index(*args, **kwargs)

    monkeypatch.setattr("odyssey_api.index.manager.index_journeys", failing_index_journeys)

    # Write a new journey and wait for background cycles
    _write_journey(journeys_dir, "j4")
    time.sleep(2.5)  # allow at least one failed cycle and one successful retry

    # The second successful pass should have picked up both journeys
    rows = handle.query("SELECT journey_id FROM journeys ORDER BY journey_id")
    journey_ids = [r["journey_id"] for r in rows]
    assert journey_ids == ["j3", "j4"], f"Expected ['j3', 'j4'] but got {journey_ids}"
    handle.stop()


def test_index_reconcile_every_zero_does_not_crash(tmp_path):
    """Test that index_reconcile_every=0 does not cause ZeroDivisionError."""
    manager.reset_for_tests()
    journeys_dir = tmp_path / "journeys"
    journeys_dir.mkdir()
    # index_reconcile_every=0 should not crash the background thread
    settings = Settings(
        journeys_dir=journeys_dir,
        db_uri=f"sqlite:///{tmp_path}/db.sqlite3",
        index_interval_seconds=1,
        index_reconcile_every=0,
    )

    handle = manager.get_index(settings)
    # Let the background thread run a few cycles
    time.sleep(2.5)

    # Verify the handle is still working (thread didn't die)
    count = handle.query("SELECT COUNT(*) AS n FROM journeys")[0]["n"]
    assert count == 0  # no journeys written, so count should be 0
    handle.stop()
