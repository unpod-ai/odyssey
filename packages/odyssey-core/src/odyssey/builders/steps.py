"""Cumulative step construction from a flat message list."""

from __future__ import annotations

from typing import List, Optional

from odyssey.primitives import Message, Step


def build_cumulative_steps(messages: List[Message]) -> List[Step]:
    """Build cumulative ``Step[]`` — one step per *turn*, per the data model.

    A **turn** is one user→agent exchange: the user speaks, the agent may call
    tools, and the agent answers. A **step** is that turn's snapshot, holding
    every message from the start of the conversation up to the turn's end. So
    step N's message list is step N-1's plus the new turn's messages, and each
    step is a self-contained training example.

    The boundary therefore falls where the *next* turn begins — the next ``user``
    message — not after every agent utterance. That distinction is what makes
    this correct for voice:

    - A tool response does **not** end a step. The agent calls a tool and then
      answers; both belong to the one turn the user opened.
    - Consecutive ``assistant`` messages do **not** each end a step. A voice
      agent emits one item per spoken utterance, so a single turn routinely
      arrives as three or four assistant messages. Snapshotting each one
      produced a step per utterance — 33 near-identical steps for a 15-turn
      call, differing by a single line.
    - Consecutive ``user`` messages do **not** each start a turn. Split STT
      finals and barge-in both produce them, and they are one thing the caller
      said.

    A trailing ``user`` message the agent never answered is an incomplete turn.
    It still gets its own final step: dropping it would silently lose the last
    thing the caller said, whereas keeping it costs one step whose last message
    is a ``user`` message — which ``derive_trainable_status`` marks
    ``not_trainable``, so no pipeline trains on it.

    Two things other than the next user message also end a step, because each
    would otherwise destroy signal the turn boundary cannot carry:

    - **A regenerated answer.** Two assistant messages at *one* decision point
      are not a sequence, they are alternatives, and a flat message list cannot
      tell them apart from a voice agent's two utterances. The ``regenerated`` /
      ``user_edit`` signal can: ``derive_trainable_status`` marks the replaced
      message ``superseded`` before steps are built, so a superseded message
      closes its own step. That is what keeps the rejected/chosen pair a DPO
      exporter needs.
    - **A system swap.** See below.

    **Mid-conversation system messages: copy-on-write semantics.**

    LiveKit agent handoffs swap the instructions mid-call; LangSmith (and most
    OpenAI-wrapping loops) re-send the system prompt at the start of each LLM
    call. Either way the flat message list holds multiple system messages
    interspersed with the conversation. This is legitimate data and must not
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

    Copy-on-write is why a swap can end a step. Waiting for the next user
    message would snapshot the finished turn *after* the prefix was replaced,
    handing the old turn the new prompt — the exact history rewrite the
    semantics exist to prevent. So a swap closes the turn first, but only if the
    agent has actually answered: providers that re-send the prompt between an
    agent's tool call and its reply must not split that turn in half.
    """
    if not messages:
        return []

    running: List[Message] = []
    steps: List[Step] = []
    seen_first_user = False
    in_system_block = False
    prev: Optional[Message] = None
    pending = False  # messages appended since the last snapshot

    for msg in messages:
        if msg.role == "system" and seen_first_user:
            if not in_system_block:
                in_system_block = True
                # Close the finished turn before the prefix moves out from under
                # it; see "Copy-on-write is why a swap can end a step".
                if pending and not _turn_open(prev):
                    steps.append(_snapshot(running))
                    pending = False
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

        # A user message that follows the agent opens the next turn, which means
        # the previous one just ended.
        if (
            msg.role == "user"
            and seen_first_user
            and pending
            and prev is not None
            and prev.role != "user"
        ):
            steps.append(_snapshot(running))
            pending = False

        running.append(msg)
        prev = msg
        pending = True
        if msg.role == "user":
            seen_first_user = True

        # An answer that was regenerated away is an alternative, not a
        # continuation, so it terminates a trajectory of its own.
        if msg.role == "assistant" and msg.trainable_status == "superseded":
            steps.append(_snapshot(running))
            pending = False

    # The last turn has no following user message to close it.
    if pending or not steps:
        steps.append(_snapshot(running))

    return steps


def _turn_open(prev: Optional[Message]) -> bool:
    """Whether the agent still owes a reply after ``prev``.

    Closed only on an assistant message carrying no tool calls: an assistant
    message that *is* a tool call is mid-turn, and so is the tool result it
    produced.
    """
    if prev is None:
        return True
    return not (prev.role == "assistant" and not prev.tool_calls)


def _snapshot(running: List[Message]) -> Step:
    return Step(messages=list(running))
