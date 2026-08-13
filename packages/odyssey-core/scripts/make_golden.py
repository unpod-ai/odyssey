#!/usr/bin/env python3
"""Generate the golden journey fixture — the artifact both projects test against.

This is the shared contract made concrete: superdialog must be able to *produce*
this file, and odyssey must be able to *consume* it. Nothing here is random or
clock-dependent, so the committed bytes are stable and a diff means a real change.

    python scripts/make_golden.py            # rewrite the fixture
    python scripts/make_golden.py --check    # fail if it would change

It encodes a realistic preference chain: a tool-calling turn, a regeneration, a
user edit, and a thumbs-up on the accepted answer — which is the minimum a DPO
exporter needs to find a (chosen, rejected) pair.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from odyssey.jsonl import encode_event, header_line  # noqa: E402
from odyssey.primitives import (  # noqa: E402
    JourneyEvent,
    Message,
    Reward,
    RewardComponent,
    Signal,
    Terminal,
    ToolCall,
    ToolResponse,
)

FIXTURE = Path(__file__).resolve().parent.parent / "tests/fixtures/golden_journey.jsonl"
JID = "j_golden_0001"
MODEL = "openai/gpt-4.1-mini"


def _ts(seq: int) -> str:
    return f"2026-01-01T09:00:{seq:02d}+00:00"


def _msg(seq: int, message: Message, model_id: str | None = None) -> JourneyEvent:
    return JourneyEvent(
        journey_id=JID,
        seq=seq,
        kind="message",
        event_id=f"golden-e{seq:02d}",
        ts=_ts(seq),
        message=message,
        model_id=model_id,
    )


def _sig(seq: int, signal: Signal) -> JourneyEvent:
    return JourneyEvent(
        journey_id=JID,
        seq=seq,
        kind="signal",
        event_id=f"golden-e{seq:02d}",
        ts=_ts(seq),
        signal=signal,
    )


def golden_events() -> list[JourneyEvent]:
    return [
        _msg(0, Message(role="system", content="You book appointments.")),
        _msg(1, Message(role="user", content="Book me for Tuesday at 3.")),
        # A tool call, with id/type preserved so tool_call_id correlation survives.
        _msg(
            2,
            Message(
                role="assistant",
                content=None,
                tool_calls=[
                    ToolCall(
                        name="check_slot",
                        arguments={"day": "tuesday", "hour": 15},
                        id="call_slot_1",
                    )
                ],
                usage={"prompt_tokens": 42, "completion_tokens": 18},
                finish_reason="tool_calls",
            ),
            model_id=MODEL,
        ),
        _msg(
            3,
            Message(
                role="tool",
                tool_response=ToolResponse(
                    id="call_slot_1",
                    name="check_slot",
                    arguments={"day": "tuesday", "hour": 15},
                    response={"available": True},
                ),
            ),
        ),
        # First answer — regenerated, so it becomes the `rejected` half of a pair.
        _msg(4, Message(role="assistant", content="Booked!"), model_id=MODEL),
        _sig(5, Signal(signal="regenerated", target_seq=4, regen_order=0)),
        # Second answer — then edited by the user.
        _msg(
            6,
            Message(role="assistant", content="Booked for Tuesday at 3pm."),
            model_id=MODEL,
        ),
        _sig(
            7,
            Signal(
                signal="user_edit",
                target_seq=6,
                regen_order=1,
                edited_output="You're all set for Tuesday at 3pm.",
            ),
        ),
        # The accepted answer, thumbs-upped — the `chosen` half.
        _msg(
            8,
            Message(role="assistant", content="You're all set for Tuesday at 3pm."),
            model_id=MODEL,
        ),
        _sig(9, Signal(signal="thumbs_up", target_seq=8)),
        JourneyEvent(
            journey_id=JID,
            seq=10,
            kind="reward",
            event_id="golden-e10",
            ts=_ts(10),
            reward=Reward(
                aggregated_value=0.92,
                aggregation_method="weighted",
                components=[
                    RewardComponent(
                        name="task_success",
                        value=1.0,
                        scaled_value=1.0,
                        weight=2.0,
                        range=(0.0, 1.0),
                    ),
                    RewardComponent(
                        name="efficiency",
                        value=0.76,
                        scaled_value=0.76,
                        weight=1.0,
                        range=(0.0, 1.0),
                    ),
                ],
            ),
        ),
        JourneyEvent(
            journey_id=JID,
            seq=11,
            kind="terminal",
            event_id="golden-e11",
            ts=_ts(11),
            terminal=Terminal(termination_reason="ENV_DONE"),
        ),
    ]


def render() -> str:
    return (
        header_line() + "\n" + "".join(encode_event(e) + "\n" for e in golden_events())
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="fail if the fixture is stale")
    args = ap.parse_args()

    text = render()
    if args.check:
        if not FIXTURE.exists() or FIXTURE.read_text(encoding="utf-8") != text:
            print(f"{FIXTURE} is stale — run scripts/make_golden.py", file=sys.stderr)
            return 1
        print("golden fixture is current")
        return 0

    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(text, encoding="utf-8")
    print(f"wrote {FIXTURE} ({len(golden_events())} events)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
