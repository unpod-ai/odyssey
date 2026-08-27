"""Augmentation: deterministic tool-call perturbation (item 3.5)."""

from __future__ import annotations

from odyssey_dataprep.augmentation import perturb_tool_calls


def test_perturb_tool_calls_drops_the_first_sorted_argument():
    journey = {
        "task": {"conversation_id": "j1"},
        "steps": [
            {
                "messages": [
                    {"role": "user", "content": "book me"},
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "name": "book",
                                "arguments": {"time": "3pm", "date": "Tuesday"},
                            }
                        ],
                    },
                ]
            }
        ],
    }
    synthetic = perturb_tool_calls(journey)
    assert len(synthetic) == 1
    call = synthetic[0]["steps"][0]["messages"][1]["tool_calls"][0]
    # "date" sorts before "time".
    assert call["arguments"] == {"time": "3pm"}


def test_perturb_tool_calls_marks_synthetic_and_labels_provenance():
    journey = {
        "task": {"conversation_id": "j1"},
        "steps": [
            {
                "messages": [
                    {
                        "role": "assistant",
                        "tool_calls": [{"name": "book", "arguments": {"a": 1}}],
                    }
                ]
            }
        ],
    }
    synthetic = perturb_tool_calls(journey)[0]
    assert synthetic["task"]["conversation_id"] == "j1__synthetic_0"
    assert synthetic["telemetry"]["data"]["synthetic"] is True
    assert synthetic["telemetry"]["data"]["augmentation"]["source_journey_id"] == "j1"
    assert synthetic["telemetry"]["data"]["augmentation"]["dropped_argument"] == "a"


def test_perturb_tool_calls_skips_calls_with_no_arguments():
    journey = {
        "task": {"conversation_id": "j1"},
        "steps": [
            {
                "messages": [
                    {
                        "role": "assistant",
                        "tool_calls": [{"name": "ping", "arguments": {}}],
                    }
                ]
            }
        ],
    }
    assert perturb_tool_calls(journey) == []


def test_perturb_tool_calls_returns_one_per_call():
    journey = {
        "task": {"conversation_id": "j1"},
        "steps": [
            {
                "messages": [
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {"name": "a", "arguments": {"x": 1}},
                            {"name": "b", "arguments": {"y": 2}},
                        ],
                    }
                ]
            }
        ],
    }
    synthetic = perturb_tool_calls(journey)
    assert len(synthetic) == 2
    assert {s["telemetry"]["data"]["augmentation"]["tool"] for s in synthetic} == {
        "a",
        "b",
    }


def test_perturb_tool_calls_returns_empty_for_no_steps():
    assert perturb_tool_calls({"task": {}, "steps": []}) == []
