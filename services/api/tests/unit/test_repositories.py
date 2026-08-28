"""Filesystem reads against real files on disk (item 8.1/8.2 support layer)."""

from __future__ import annotations

import yaml
from odyssey.jsonl import write_events
from odyssey.primitives import JourneyEvent, JourneyHeader, Message, Terminal

from odyssey_api.repositories import filesystem

JID = "j_repo"
HEADER = JourneyHeader(journey_id=JID, data_source="livekit")


def _events() -> list[JourneyEvent]:
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


def test_list_journeys_empty_dir(tmp_path):
    assert filesystem.list_journeys(tmp_path / "nope") == []


def test_list_journeys_and_find(tmp_path):
    date_dir = tmp_path / "2026-08-28"
    date_dir.mkdir()
    write_events(date_dir / f"{JID}.jsonl", _events(), header=HEADER)

    assert filesystem.list_journeys(tmp_path) == [(JID, "2026-08-28")]
    found = filesystem.find_journey_path(tmp_path, JID)
    assert found == date_dir / f"{JID}.jsonl"
    assert filesystem.find_journey_path(tmp_path, "nope") is None


def test_read_registry_missing_file(tmp_path):
    assert filesystem.read_registry(tmp_path / "registry.yaml", "corpora") == {}


def test_read_registry_real_file(tmp_path):
    path = tmp_path / "registry.yaml"
    path.write_text(
        yaml.safe_dump(
            {"corpora": {"c1": [{"version": 1, "manifest_sha256": "a", "uri": "u"}]}}
        )
    )
    doc = filesystem.read_registry(path, "corpora")
    assert doc["c1"][0]["version"] == 1


def test_list_eval_reports(tmp_path):
    (tmp_path / "a.json").write_text("{}")
    (tmp_path / "b.json").write_text("{}")
    assert [p.name for p in filesystem.list_eval_reports(tmp_path)] == [
        "a.json",
        "b.json",
    ]


def test_list_exports(tmp_path):
    shard = tmp_path / "sft.jsonl"
    shard.write_text('{"messages": []}\n{"messages": []}\n')
    exports = filesystem.list_exports(tmp_path)
    assert len(exports) == 1
    assert exports[0]["name"] == "sft.jsonl"
    assert exports[0]["rows"] == 2
    assert len(exports[0]["sha256"]) == 64
