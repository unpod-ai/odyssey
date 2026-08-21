"""A booking agent, instrumented. Run it, then look at what odyssey collected.

    python examples/booking_agent.py
    python -m odyssey.cli --spool ./.odyssey show call_7781

Everything below the "your existing code" line is ordinary application code. The
only additions are the two module-level lines and the `with odyssey.journey(...)`
block — no call site names a journey_id, a seq, or a flush.

The LLM is faked so this runs with no API key and no network. For a real client,
the whole change is the import:

    from odyssey.integrations.anthropic import Anthropic
    client = Anthropic()          # every messages.create() is recorded
"""

import odyssey  # <-- 1 of 2

odyssey.init(spool_dir="./.odyssey", out_dir="./logs")  # <-- 2 of 2. That is all.

from odyssey.primitives import Message, ToolCall, ToolResponse  # noqa: E402

# ---------------------------------------------------------------------------
# Your existing code, untouched
# ---------------------------------------------------------------------------

_SCRIPT = [
    {"text": "Sure — what day works for you?"},
    {"text": "Tuesday works. What time?"},
    {"tool": {"name": "book", "args": {"day": "tue", "time": "15:00"}}},
    {"text": "Booked for Tuesday 3pm."},
]
_calls: list = []


def call_llm(history):
    """Stands in for anthropic/openai so the example needs no API key."""
    _calls.append(1)
    return _SCRIPT[min(len(_calls) - 1, len(_SCRIPT) - 1)]


def book(day, time):
    return {"confirmed": True, "ref": "BK-4417"}


# ---------------------------------------------------------------------------
# One journey per call. The only odyssey-shaped code in the app.
# ---------------------------------------------------------------------------


def handle_call(call_id, user_turns):
    system = "You are a booking assistant."
    history = [{"role": "system", "content": system}]

    with odyssey.journey(id=call_id, channel="voice") as trace:
        trace.message(Message(role="system", content=system))

        for text in user_turns:
            history.append({"role": "user", "content": text})
            trace.message(Message(role="user", content=text))

            out = call_llm(history)

            if "tool" in out:
                t = out["tool"]
                trace.message(
                    Message(
                        role="assistant",
                        tool_calls=[
                            ToolCall(id="tc_1", name=t["name"], arguments=t["args"])
                        ],
                    ),
                    model_id="claude-opus-5",
                )
                result = book(**t["args"])
                trace.message(
                    Message(
                        role="tool",
                        tool_response=ToolResponse(
                            id="tc_1",
                            name=t["name"],
                            arguments=t["args"],
                            response=result,
                        ),
                    )
                )
                out = call_llm(history)

            history.append({"role": "assistant", "content": out["text"]})
            trace.message(
                Message(role="assistant", content=out["text"]),
                model_id="claude-opus-5",
            )

        # The caller pressed "regenerate", then approved the second answer.
        # This is what turns a transcript into preference data: `regenerated`
        # marks the rejected side, `thumbs_up` marks the chosen one.
        rejected = trace.context.last_message_seq
        trace.signal("regenerated", target_seq=rejected)
        trace.message(
            Message(role="assistant", content="Done — Tuesday 3pm, ref BK-4417."),
            model_id="claude-opus-5",
        )
        trace.signal("thumbs_up")
        trace.reward(0.9)


if __name__ == "__main__":
    handle_call(
        "call_7781",
        [
            "Hi, I need an appointment.",
            "Tuesday afternoon please.",
            "Yes, 3pm.",
        ],
    )
    print("call handled. odyssey drains on exit — flush() is never called here.")
    print()
    print("now look at what it collected:")
    print("  python -m odyssey.cli --spool ./.odyssey show call_7781")
