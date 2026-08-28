"""Augmentation: deterministic tool-call perturbation, paraphrase, and
synthetic-negative generation (item 3.5).

No real ``openai`` install for the LLM-backed tests: a fake client double
exposing only ``chat.completions.create()`` is injected via ``client=``,
the same seam ``collect_from_object_store`` uses for ``boto3`` — proves
these functions never import the real SDK when a client is supplied, and
keeps the suite runnable without the optional dependency installed.
"""

from __future__ import annotations

import json

from odyssey_dataprep.augmentation import (
    generate_synthetic_negative,
    paraphrase_journey,
    perturb_tool_calls,
)


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


# --------------------------------------------------------------------------
# Fake OpenAI-shaped client
# --------------------------------------------------------------------------


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(self._replies.pop(0))


class _FakeChat:
    def __init__(self, replies):
        self.completions = _FakeCompletions(replies)


class FakeClient:
    def __init__(self, *replies):
        self.chat = _FakeChat(list(replies))


def _journey(*, user="book me a slot", answer="sure, when?"):
    return {
        "task": {"conversation_id": "j1"},
        "steps": [
            {
                "messages": [
                    {"role": "user", "content": user},
                    {"role": "assistant", "content": answer},
                ]
            }
        ],
    }


# --------------------------------------------------------------------------
# paraphrase_journey
# --------------------------------------------------------------------------


def test_paraphrase_journey_rewrites_only_user_turns():
    client = FakeClient(json.dumps(["may I get a slot?"]))
    synthetic = paraphrase_journey(_journey(), client=client, n=1)

    assert len(synthetic) == 1
    messages = synthetic[0]["steps"][0]["messages"]
    assert messages[0] == {"role": "user", "content": "may I get a slot?"}
    assert messages[1] == {"role": "assistant", "content": "sure, when?"}


def test_paraphrase_journey_generates_n_variants_with_n_llm_calls():
    client = FakeClient(
        json.dumps(["variant one"]),
        json.dumps(["variant two"]),
    )
    synthetic = paraphrase_journey(_journey(), client=client, n=2)

    assert len(synthetic) == 2
    assert synthetic[0]["steps"][0]["messages"][0]["content"] == "variant one"
    assert synthetic[1]["steps"][0]["messages"][0]["content"] == "variant two"
    assert len(client.chat.completions.calls) == 2


def test_paraphrase_journey_marks_synthetic_and_labels_provenance():
    client = FakeClient(json.dumps(["reworded"]))
    synthetic = paraphrase_journey(_journey(), client=client, model="gpt-x")[0]

    assert synthetic["task"]["conversation_id"] == "j1__paraphrase_0"
    assert synthetic["telemetry"]["data"]["synthetic"] is True
    aug = synthetic["telemetry"]["data"]["augmentation"]
    assert aug == {
        "kind": "paraphrase",
        "source_journey_id": "j1",
        "model": "gpt-x",
    }


def test_paraphrase_journey_skips_a_malformed_llm_response():
    client = FakeClient("not json at all")
    assert paraphrase_journey(_journey(), client=client, n=1) == []


def test_paraphrase_journey_skips_a_wrong_length_response():
    client = FakeClient(json.dumps(["one", "two"]))  # journey has 1 user turn
    assert paraphrase_journey(_journey(), client=client, n=1) == []


def test_paraphrase_journey_returns_empty_when_no_user_turns():
    journey = {
        "task": {"conversation_id": "j1"},
        "steps": [{"messages": [{"role": "assistant", "content": "hi"}]}],
    }
    client = FakeClient()
    assert paraphrase_journey(journey, client=client, n=1) == []


def test_paraphrase_journey_returns_empty_for_no_steps():
    client = FakeClient()
    assert paraphrase_journey({"task": {}, "steps": []}, client=client) == []


# --------------------------------------------------------------------------
# generate_synthetic_negative
# --------------------------------------------------------------------------


def test_generate_synthetic_negative_produces_a_superseded_then_trainable_chain():
    client = FakeClient("a plausible but wrong answer")
    synthetic = generate_synthetic_negative(_journey(), client=client)

    assert synthetic is not None
    statuses = [s["trainable_status"] for s in synthetic["steps"]]
    assert statuses == ["superseded", "trainable"]

    rejected_step, chosen_step = synthetic["steps"]
    assert rejected_step["messages"][-1]["content"] == "a plausible but wrong answer"
    assert chosen_step["messages"][-1]["content"] == "sure, when?"
    # Both candidates answer the same prompt.
    assert rejected_step["messages"][:-1] == chosen_step["messages"][:-1]


def test_generate_synthetic_negative_marks_synthetic_and_labels_provenance():
    client = FakeClient("a worse answer")
    synthetic = generate_synthetic_negative(_journey(), client=client, model="gpt-x")

    assert synthetic["task"]["conversation_id"] == "j1__synthetic_negative"
    assert synthetic["telemetry"]["data"]["synthetic"] is True
    aug = synthetic["telemetry"]["data"]["augmentation"]
    assert aug == {
        "kind": "synthetic_negative",
        "source_journey_id": "j1",
        "model": "gpt-x",
    }


def test_generate_synthetic_negative_returns_none_without_a_final_assistant_turn():
    journey = {
        "task": {"conversation_id": "j1"},
        "steps": [{"messages": [{"role": "user", "content": "hi"}]}],
    }
    client = FakeClient()
    assert generate_synthetic_negative(journey, client=client) is None


def test_generate_synthetic_negative_returns_none_on_an_empty_response():
    client = FakeClient("   ")
    assert generate_synthetic_negative(_journey(), client=client) is None


def test_generate_synthetic_negative_returns_none_for_no_steps():
    client = FakeClient()
    assert generate_synthetic_negative({"task": {}, "steps": []}, client=client) is None
