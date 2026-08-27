"""Cleaning: dedupe, dead-turn drop, encoding repair (item 3.2)."""

from __future__ import annotations

import json

from odyssey_dataprep.cleaning import (
    clean_dir,
    dedupe_journeys,
    drop_dead_turns,
    repair_encoding,
)


def write_journey(dir_, name, steps, content_hash_val="h"):
    doc = {
        "task": {"conversation_id": name},
        "steps": steps,
        "telemetry": {"source": "test", "data": {"content_hash": content_hash_val}},
    }
    path = dir_ / f"{name}.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def m(role, content=None, tool_calls=None):
    return {"role": role, "content": content, "tool_calls": tool_calls}


def test_drop_dead_turns_removes_an_empty_middle_step_and_splices_history():
    journey = {
        "steps": [
            {"messages": [m("user", "hi")]},
            {"messages": [m("user", "hi"), m("assistant", None)]},  # dead delta
            {
                "messages": [
                    m("user", "hi"),
                    m("assistant", None),
                    m("assistant", "real reply"),
                ]
            },
        ]
    }
    cleaned, dropped = drop_dead_turns(journey)
    assert dropped == 1
    assert len(cleaned["steps"]) == 2
    # The dead message is gone from the surviving step's cumulative history too.
    assert cleaned["steps"][-1]["messages"] == [
        m("user", "hi"),
        m("assistant", "real reply"),
    ]


def test_drop_dead_turns_keeps_a_step_with_a_tool_call_even_if_content_is_none():
    journey = {
        "steps": [
            {"messages": [m("user", "hi")]},
            {
                "messages": [
                    m("user", "hi"),
                    m("assistant", None, tool_calls=[{"name": "lookup"}]),
                ]
            },
        ]
    }
    cleaned, dropped = drop_dead_turns(journey)
    assert dropped == 0
    assert len(cleaned["steps"]) == 2


def test_repair_encoding_normalizes_and_strips_control_chars():
    journey = {"steps": [{"messages": [m("assistant", "he\x00llo")]}]}
    cleaned, changed = repair_encoding(journey)
    assert changed == 1
    assert cleaned["steps"][0]["messages"][0]["content"] == "hello"


def test_repair_encoding_reports_zero_changes_for_clean_text():
    journey = {"steps": [{"messages": [m("assistant", "already clean")]}]}
    _, changed = repair_encoding(journey)
    assert changed == 0


def test_dedupe_journeys_keeps_first_by_sorted_path(tmp_path):
    a = write_journey(tmp_path, "a", [], content_hash_val="same")
    b = write_journey(tmp_path, "b", [], content_hash_val="same")
    kept, dropped = dedupe_journeys([b, a])
    assert kept == [a]
    assert dropped == [b]


def test_dedupe_journeys_keeps_distinct_content(tmp_path):
    a = write_journey(tmp_path, "a", [], content_hash_val="h1")
    b = write_journey(tmp_path, "b", [], content_hash_val="h2")
    kept, dropped = dedupe_journeys([a, b])
    assert set(kept) == {a, b}
    assert dropped == []


def test_clean_dir_end_to_end(tmp_path):
    src = tmp_path / "normalized"
    src.mkdir()
    write_journey(
        src,
        "a",
        [
            {"messages": [m("user", "hi")]},
            {"messages": [m("user", "hi"), m("assistant", None)]},
        ],
        content_hash_val="h1",
    )
    write_journey(src, "a_dup", [], content_hash_val="h1")

    result = clean_dir(src, tmp_path / "cleaned")
    assert result.count == 1
    assert result.duplicates_dropped == 1
    assert result.dead_turns_dropped == 1
