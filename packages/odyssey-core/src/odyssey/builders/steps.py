"""Cumulative step construction from a flat message list."""

from __future__ import annotations

from typing import List

from odyssey.primitives import Message, Step


def build_cumulative_steps(messages: List[Message]) -> List[Step]:
    """Build cumulative ``Step[]`` matching the engine's step lifecycle.

    Each step ends after a complete agent turn: either a tool response or a
    final assistant message (no ``tool_calls``). Step 0 includes the initial
    context (system + user) plus the first agent turn's response.

    **Mid-conversation system messages: copy-on-write semantics.**

    LangSmith (and many other providers) resend the system prompt at the
    start of each LLM call. When the parser collapses turns into a flat
    message list, the result contains multiple system messages interspersed
    with user/assistant/tool messages. This is legitimate data and must not
    fail journey construction.

    Semantics when a ``system`` message appears after the first ``user``
    message:

    1. Earlier ``Step`` objects (already snapshotted) are immutable -- each
       ``Step`` holds its own ``list(running)`` copy -- so they keep the
       **original** system prefix they captured.
    2. The running context's system prefix is *replaced* (not appended to)
       by the new system messages, so all subsequent snapshots use the
       **new** system prefix.

    Net effect: pre-swap steps see the pre-swap system prompt, post-swap
    steps see the post-swap system prompt. This matches the engine's own
    behavior and is the "right answer" for context compaction / prompt
    refresh, without rewriting history.

    A contiguous block of system messages (e.g. several consecutive system
    messages from a re-sent prompt) is treated as a single replacement:
    only the first clears the existing prefix, and the rest stack in.
    """
    if not messages:
        return []

    running: List[Message] = []
    steps: List[Step] = []
    seen_first_user = False
    in_system_block = False

    for msg in messages:
        if msg.role == "system" and seen_first_user:
            if not in_system_block:
                in_system_block = True
                running = [m for m in running if m.role != "system"]
            # Keep system messages at the front of the running context, ordered
            # as they appeared in the input.
            running.insert(
                sum(1 for m in running if m.role == "system"),
                msg,
            )
            continue

        if msg.role != "system":
            in_system_block = False

        running.append(msg)

        if not seen_first_user:
            if msg.role == "user":
                seen_first_user = True
                steps.append(_snapshot(running))
            continue

        if msg.role == "tool":
            steps.append(_snapshot(running))
        elif msg.role == "assistant" and not msg.tool_calls:
            steps.append(_snapshot(running))

    if not steps or len(running) > len(steps[-1].messages):
        steps.append(_snapshot(running))

    return steps


def _snapshot(running: List[Message]) -> Step:
    return Step(messages=list(running))
