"""Augmentation — item 3.5: deterministic, model-free tool-call perturbation.

`docs/WORKING.md` names three techniques for this stage: paraphrase,
synthetic negatives, tool-call perturbation. Only the third is implemented
here — it is the only one of the three that has a well-defined, correct
answer without calling a model. Paraphrasing and general synthetic-negative
generation need an LLM in the loop, which is a real dependency and a real
cost this stage does not have a justification to add speculatively (same
"a dependency nothing imports is a phantom dep" reasoning `packages/
odyssey-core`'s own `pyproject.toml` states for itself). Tool-call
perturbation doubles as a synthetic negative generator for free: a call
with a dropped required argument is exactly the "missing required
argument" failure mode real traffic rarely produces enough of on its own.
"""

from __future__ import annotations

from typing import Any, Dict, List

__all__ = ["perturb_tool_calls"]


def perturb_tool_calls(journey: Dict[str, Any]) -> List[Dict[str, Any]]:
    """One synthetic negative journey per real tool call in ``journey``'s
    final (full-history) step, each with that call's first argument
    (sorted by key, for determinism) dropped.

    Built from the last step's cumulative message list rather than the
    step-by-step history, so there is no cumulative-prefix invariant to
    preserve across steps (`cleaning.drop_dead_turns` carries that
    complexity for a real reason; a single synthetic example does not need
    it). The result is a single-step journey — enough to train "this
    argument was missing" as a case, not a multi-turn replay.
    """
    steps = journey.get("steps") or []
    if not steps:
        return []
    messages = steps[-1].get("messages") or []

    task = journey.get("task") or {}
    source_id = task.get("conversation_id") or task.get("id") or "journey"

    synthetic: List[Dict[str, Any]] = []
    for i, message in enumerate(messages):
        for call in message.get("tool_calls") or []:
            args = call.get("arguments") or {}
            if not args:
                continue
            dropped_key = sorted(args)[0]
            perturbed_args = {k: v for k, v in args.items() if k != dropped_key}

            new_messages = [dict(m) for m in messages]
            new_message = dict(message)
            new_message["tool_calls"] = [
                {**call, "arguments": perturbed_args} if c is call else c
                for c in message["tool_calls"]
            ]
            new_messages[i] = new_message

            synthetic.append(
                {
                    "task": {
                        **task,
                        "conversation_id": f"{source_id}__synthetic_{len(synthetic)}",
                    },
                    "steps": [
                        {"messages": new_messages, "trainable_status": "trainable"}
                    ],
                    "telemetry": {
                        "source": "augmentation",
                        "data": {
                            "synthetic": True,
                            "augmentation": {
                                "kind": "tool_call_perturbation",
                                "source_journey_id": source_id,
                                "tool": call.get("name"),
                                "dropped_argument": dropped_key,
                            },
                        },
                    },
                }
            )
    return synthetic
