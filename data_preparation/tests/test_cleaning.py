"""Cleaning: dedupe, dead-turn drop, encoding repair (item 3.2)."""

from __future__ import annotations

import json

from odyssey.primitives import PiiPolicy

from odyssey_dataprep.cleaning import (
    clean_dir,
    dedupe_journeys,
    drop_dead_turns,
    repair_encoding,
    scrub_pii_content,
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


def test_scrub_pii_content_redacts_content_per_policy():
    journey = {"steps": [{"messages": [m("user", "call me at 555-123-4567")]}]}
    policy = PiiPolicy(name="p", rules=["PHONE"])
    cleaned, changed = scrub_pii_content(journey, policy)
    assert changed == 1
    assert "555-123-4567" not in cleaned["steps"][0]["messages"][0]["content"]
    assert "[REDACTED_PHONE]" in cleaned["steps"][0]["messages"][0]["content"]


def test_scrub_pii_content_reports_zero_changes_for_clean_text():
    journey = {"steps": [{"messages": [m("assistant", "book Tuesday at 3")]}]}
    _, changed = scrub_pii_content(journey, PiiPolicy(name="p", rules=["EMAIL"]))
    assert changed == 0


def test_clean_dir_pii_policy_is_opt_in(tmp_path):
    src = tmp_path / "normalized"
    src.mkdir()
    write_journey(
        src,
        "a",
        [{"messages": [m("user", "email me at a@b.com")]}],
        content_hash_val="h1",
    )

    without_policy = clean_dir(src, tmp_path / "no-scrub")
    assert without_policy.pii_scrubs == 0
    cleaned_doc = json.loads((tmp_path / "no-scrub" / "a.json").read_text())
    assert cleaned_doc["steps"][0]["messages"][0]["content"] == "email me at a@b.com"

    policy = PiiPolicy(name="p", rules=["EMAIL"])
    with_policy = clean_dir(src, tmp_path / "scrub", pii_policy=policy)
    assert with_policy.pii_scrubs == 1
    scrubbed_doc = json.loads((tmp_path / "scrub" / "a.json").read_text())
    assert "a@b.com" not in scrubbed_doc["steps"][0]["messages"][0]["content"]


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
