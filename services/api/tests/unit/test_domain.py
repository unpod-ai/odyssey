"""Use-cases — the same layer routers call, no fastapi involved."""

from __future__ import annotations

import json

import pytest
import yaml
from odyssey.jsonl import write_events
from odyssey.primitives import JourneyEvent, JourneyHeader, Message, Terminal

from odyssey_api.domain import eval_runs, exports, journeys, registries

JID = "j_domain"
HEADER = JourneyHeader(journey_id=JID, data_source="livekit")


def _write_journey(date_dir):
    date_dir.mkdir(parents=True, exist_ok=True)
    write_events(
        date_dir / f"{JID}.jsonl",
        [
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
        ],
        header=HEADER,
    )


def test_get_journey_not_found_raises(tmp_path):
    with pytest.raises(journeys.JourneyNotFoundError):
        journeys.get_journey(tmp_path, "nope")


def test_get_journey_real_shard(tmp_path):
    _write_journey(tmp_path / "2026-08-28")
    result = journeys.get_journey(tmp_path, JID)
    assert result.journey_id == JID
    assert result.complete is True


def test_list_journeys_with_status(tmp_path):
    _write_journey(tmp_path / "2026-08-28")
    out = journeys.list_journeys_with_status(tmp_path)
    assert out == [(JID, "2026-08-28", True)]


def test_list_datasets(tmp_path):
    path = tmp_path / "registry.yaml"
    path.write_text(yaml.safe_dump({"corpora": {"c1": [{"version": 1}]}}))
    assert registries.list_datasets(path) == {"c1": [{"version": 1}]}


def test_list_models(tmp_path):
    path = tmp_path / "registry.yaml"
    path.write_text(yaml.safe_dump({"models": {"m1": [{"version": 1}]}}))
    assert registries.list_models(path) == {"m1": [{"version": 1}]}


def test_list_eval_runs(tmp_path):
    report = {
        "benchmark": "b1",
        "metric": "exact_match",
        "mean_score": 0.5,
        "tasks": [],
    }
    (tmp_path / "b1.json").write_text(json.dumps(report))
    out = eval_runs.list_eval_runs(tmp_path)
    assert out == [
        {
            "benchmark_name": "b1",
            "metric_name": "exact_match",
            "mean_score": 0.5,
            "report_path": str(tmp_path / "b1.json"),
        }
    ]


def test_list_exports(tmp_path):
    (tmp_path / "sft.jsonl").write_text('{"a": 1}\n')
    out = exports.list_exports(tmp_path)
    assert out[0]["rows"] == 1
