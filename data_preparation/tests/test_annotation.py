"""Annotation: human-in-loop queue adapters (item 3.4)."""

from __future__ import annotations

import json

from odyssey_dataprep.annotation import apply_reviews, build_queue


def write_journey(dir_, jid, content_hash_val="h"):
    doc = {
        "task": {"conversation_id": jid},
        "steps": [
            {
                "messages": [
                    {"role": "user", "content": "book me"},
                    {"role": "assistant", "content": "booked"},
                ]
            }
        ],
        "telemetry": {"source": "test", "data": {"content_hash": content_hash_val}},
    }
    (dir_ / f"{jid}.json").write_text(json.dumps(doc), encoding="utf-8")


def read_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_build_queue_writes_one_line_per_journey(tmp_path):
    src = tmp_path / "normalized"
    src.mkdir()
    write_journey(src, "j1", content_hash_val="h1")
    write_journey(src, "j2", content_hash_val="h2")

    n = build_queue(src, tmp_path / "queue.jsonl")
    assert n == 2
    lines = read_lines(tmp_path / "queue.jsonl")
    assert {line["journey_id"] for line in lines} == {"j1", "j2"}
    assert lines[0]["preview"].startswith("user: 'book me'")


def test_apply_reviews_sets_reward_and_annotation(tmp_path):
    src = tmp_path / "normalized"
    src.mkdir()
    write_journey(src, "j1")

    decisions = tmp_path / "decisions.jsonl"
    decisions.write_text(
        json.dumps(
            {"journey_id": "j1", "approved": True, "score": 0.8, "notes": "good"}
        )
        + "\n",
        encoding="utf-8",
    )

    result = apply_reviews(src, decisions, tmp_path / "annotated")
    assert result.count == 1
    assert result.skipped == []

    doc = json.loads(result.applied[0].read_text())
    assert doc["reward"]["aggregated_value"] == 0.8
    assert doc["telemetry"]["data"]["annotation"] == {"approved": True, "notes": "good"}


def test_apply_reviews_reports_decisions_with_no_matching_journey(tmp_path):
    src = tmp_path / "normalized"
    src.mkdir()
    write_journey(src, "j1")

    decisions = tmp_path / "decisions.jsonl"
    decisions.write_text(
        json.dumps({"journey_id": "ghost", "approved": False}) + "\n", encoding="utf-8"
    )

    result = apply_reviews(src, decisions, tmp_path / "annotated")
    assert result.count == 0
    assert result.skipped == ["ghost"]


def test_apply_reviews_without_score_leaves_reward_untouched(tmp_path):
    src = tmp_path / "normalized"
    src.mkdir()
    write_journey(src, "j1")

    decisions = tmp_path / "decisions.jsonl"
    decisions.write_text(
        json.dumps({"journey_id": "j1", "approved": False}) + "\n", encoding="utf-8"
    )

    result = apply_reviews(src, decisions, tmp_path / "annotated")
    doc = json.loads(result.applied[0].read_text())
    assert "reward" not in doc
    assert doc["telemetry"]["data"]["annotation"]["approved"] is False
