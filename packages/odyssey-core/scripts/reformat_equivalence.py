#!/usr/bin/env python3
"""Reformat-equivalence probe for the trajectory_sdk port (task 2.5).

Drives every ported adapter and builder over a fixed fixture and dumps the
result as canonical JSON. Run once before `black`/`isort`, once after, and diff
the two outputs: a reformat that changed behaviour cannot produce the same bytes.

    python scripts/reformat_equivalence.py > /tmp/before.json
    task fmt
    python scripts/reformat_equivalence.py > /tmp/after.json
    diff /tmp/before.json /tmp/after.json

Deliberately dependency-free and deterministic: no clock, no uuid, no network.
Timestamps are passed in so `total_time` is reproducible.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from typing import Any

sys.path.insert(0, __import__("os").environ.get("ODYSSEY_SRC", "src"))

from odyssey.builders.journey import build_journey_from_messages  # noqa: E402
from odyssey.builders.messages import (  # noqa: E402
    flatten_text_content,
    messages_from_anthropic_messages,
    messages_from_openai_chat,
    messages_from_prompt_response,
    messages_from_role_content_pairs,
    messages_from_vercel_ai_sdk,
    normalize_role,
    parse_tool_arguments,
)
from odyssey.builders.reward import build_reward_from_scalar  # noqa: E402
from odyssey.builders.steps import build_cumulative_steps  # noqa: E402

OPENAI_CHAT: list[dict[str, Any]] = [
    {"role": "system", "content": "You book appointments."},
    {"role": "user", "content": "Book me for Tuesday at 3."},
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "check_slot",
                    "arguments": '{"day": "tuesday", "hour": 15}',
                },
            }
        ],
    },
    {"role": "tool", "tool_call_id": "call_1", "content": '{"available": true}'},
    {"role": "assistant", "content": "Booked for Tuesday at 3pm."},
]

ANTHROPIC: list[dict[str, Any]] = [
    {"role": "user", "content": [{"type": "text", "text": "What is 2+2?"}]},
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Let me compute that."},
            {
                "type": "tool_use",
                "id": "tu_1",
                "name": "calc",
                "input": {"expr": "2+2"},
            },
        ],
    },
    {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "4"},
        ],
    },
    {"role": "assistant", "content": [{"type": "text", "text": "It is 4."}]},
]

VERCEL: list[dict[str, Any]] = [
    {"role": "user", "content": "ping"},
    {"role": "assistant", "content": "pong"},
]

ROLE_PAIRS = [("user", "hello"), ("assistant", "hi there")]


def _probe(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Call fn and encode either its result or the exception it raised.

    Raising IS behaviour worth pinning — `normalize_role(None)` is specified to
    reject, and a reformat that changed which branch raised would be a silent
    regression the happy path never sees.
    """
    try:
        return {"ok": _encode(fn(*args, **kwargs))}
    except Exception as exc:  # noqa: BLE001 - pinning the raise is the point
        return {"raised": type(exc).__name__, "msg": str(exc)}


def _encode(obj: Any) -> Any:
    """Recursively convert dataclasses/tuples to JSON-safe structures."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _encode(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): _encode(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_encode(v) for v in obj]
    return obj


# Every raise the fixture is SUPPOSED to provoke, as "<key>[<index>]".
# Anything else raising means the fixture is broken, not that behaviour changed —
# and a broken fixture still diffs clean, which is the failure mode this guards.
EXPECTED_RAISES: frozenset[str] = frozenset(
    {
        "normalize_role[5]",  # "" -> role is required (messages.py:54)
        "normalize_role[6]",  # None -> role is required (messages.py:54)
        # Unparseable tool-argument JSON rejects by design — messages.py:176-179
        # is explicit that there is "no silent {"_raw": ...} escape hatch".
        "parse_tool_arguments[2]",
        "build_reward_from_scalar[1]",  # non-increasing score_range (reward.py:27)
    }
)


def _assert_raises_only(out: dict[str, Any], expected: frozenset[str]) -> None:
    """Fail loudly if the set of raising probes is not exactly `expected`."""
    actual: set[str] = set()
    for key, value in out.items():
        entries = value if isinstance(value, list) else [value]
        for i, entry in enumerate(entries):
            if isinstance(entry, dict) and "raised" in entry:
                actual.add(f"{key}[{i}]")
    unexpected = actual - expected
    missing = expected - actual
    if unexpected or missing:
        lines = ["reformat_equivalence fixture is not sound:"]
        for name in sorted(unexpected):
            lines.append(f"  UNEXPECTED raise at {name} — the fixture is wrong")
        for name in sorted(missing):
            lines.append(f"  EXPECTED raise missing at {name} — behaviour changed")
        raise SystemExit("\n".join(lines))


def main() -> None:
    out: dict[str, Any] = {}

    # Pure helpers — cheap, and they are what the adapters are built on.
    out["normalize_role"] = [
        _probe(normalize_role, r)
        for r in ["system", "user", "assistant", "tool", "human", "", None]
    ]
    out["flatten_text_content"] = [
        _probe(flatten_text_content, "plain"),
        _probe(
            flatten_text_content,
            [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
        ),
        _probe(flatten_text_content, None),
    ]
    out["parse_tool_arguments"] = [
        _probe(parse_tool_arguments, '{"a": 1}'),
        _probe(parse_tool_arguments, {"b": 2}),
        _probe(parse_tool_arguments, "not json"),
    ]

    # The five framework adapters.
    oa = messages_from_openai_chat(OPENAI_CHAT)
    an = messages_from_anthropic_messages(ANTHROPIC)
    out["messages_from_openai_chat"] = _encode(oa)
    out["messages_from_anthropic_messages"] = _encode(an)
    out["messages_from_vercel_ai_sdk"] = _encode(messages_from_vercel_ai_sdk(VERCEL))
    out["messages_from_prompt_response"] = _probe(
        messages_from_prompt_response, "q", "a", system="sys"
    )
    out["messages_from_role_content_pairs"] = _encode(
        messages_from_role_content_pairs(ROLE_PAIRS)
    )

    # Reward + cumulative steps + the whole-journey builder.
    # NOTE the kwarg is `score_range`, not `range`. An earlier version of this
    # probe passed `range=` and _probe faithfully recorded the resulting
    # TypeError as a pinned "raised" entry — a green equivalence check over a
    # fixture that never actually called the function. Hence _assert_raises_only.
    out["build_reward_from_scalar"] = [
        _probe(
            build_reward_from_scalar, 0.75, name="task_success", score_range=(0.0, 1.0)
        ),
        # A non-increasing range is specified to reject (reward.py:27-28).
        _probe(build_reward_from_scalar, 0.5, name="bad_range", score_range=(1.0, 1.0)),
    ]
    out["build_cumulative_steps_openai"] = _encode(build_cumulative_steps(oa))
    out["build_cumulative_steps_anthropic"] = _encode(build_cumulative_steps(an))

    out["build_journey_from_messages"] = _encode(
        build_journey_from_messages(
            oa,
            conversation_id="conv_fixture_1",
            data_source="reformat_probe",
            reward=build_reward_from_scalar(1.0, name="booked"),
            task_metadata={"num_turns": 2, "total_tokens": 128, "total_cost": 0.0004},
            start_time="2026-01-01T00:00:00+00:00",
            end_time="2026-01-01T00:00:12+00:00",
            termination_reason="ENV_DONE",
            trace_id="trace_fixture_1",
            model_id="openai/gpt-4.1-mini",
        )
    )

    _assert_raises_only(out, EXPECTED_RAISES)
    json.dump(out, sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
