"""Augmentation — item 3.5: tool-call perturbation, paraphrase, and
synthetic-negative generation.

`docs/WORKING.md` names three techniques for this stage. Tool-call
perturbation (:func:`perturb_tool_calls`) is deterministic and model-free —
the only one of the three with a well-defined, correct answer without
calling a model, and it doubles as a synthetic negative generator for free: a
call with a dropped required argument is exactly the "missing required
argument" failure mode real traffic rarely produces enough of on its own.

:func:`paraphrase_journey` and :func:`generate_synthetic_negative` need an
LLM in the loop, so both take a caller-injected ``client`` (an
``openai.OpenAI``-shaped object exposing ``.chat.completions.create()``)
rather than importing a provider SDK themselves — the same seam
``data_preparation.collection.collect_from_object_store`` uses for
``boto3``: a real client for production, a fake double for tests, never a
network call this module makes on its own initiative. The optional
``odyssey-dataprep[llm]`` extra (``openai>=1.0``) is a convenience for
building the real one; nothing here imports it directly. Both functions
never raise on a bad or malformed LLM response — one failed augmentation
call must not abort a batch run over many journeys, the same reasoning
``odyssey.integrations``' ``_guard`` pattern uses for provider capture.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

__all__ = [
    "perturb_tool_calls",
    "paraphrase_journey",
    "generate_synthetic_negative",
]


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


def _default_client() -> Any:
    """A real ``openai.OpenAI()``, imported lazily — never at module scope,
    so ``openai`` stays an optional extra (``odyssey-dataprep[llm]``), not a
    dependency of this member's light install. Reads ``OPENAI_API_KEY`` the
    same way the SDK's own default constructor does; nothing here re-derives
    that. OpenAI-*compatible* endpoints (Groq, Together, local vLLM/Ollama,
    ...) work the same way ``odyssey.integrations.openai`` documents: pass
    an already-configured client with a different ``base_url`` instead of
    using this default."""
    # pyrefly: ignore[missing-import]  — optional extra, odyssey-dataprep[llm].
    import openai  # noqa: PLC0415 - opt-in only when no client is injected

    return openai.OpenAI()


def _chat(client: Any, model: str, prompt: str, *, temperature: float) -> str:
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
    )
    return response.choices[0].message.content or ""


_PARAPHRASE_PROMPT = (
    "Rewrite each of the following user messages so it keeps the same "
    "intent and meaning but uses different wording. Reply with nothing but "
    "a JSON array of strings, the same length and order as the input, one "
    "rewritten message per input message.\n\n{texts}"
)


def _paraphrase_texts(
    client: Any, model: str, texts: List[str], *, temperature: float
) -> List[str]:
    """One LLM call rewrites every user turn in a journey at once, rather
    than one call per turn — cheaper, and keeps the rewrites internally
    consistent with each other (a paraphrase of turn 2 that still makes
    sense given the paraphrase already chosen for turn 1)."""
    raw = _chat(
        client,
        model,
        _PARAPHRASE_PROMPT.format(texts=json.dumps(texts)),
        temperature=temperature,
    )
    parsed = json.loads(raw)
    if (
        not isinstance(parsed, list)
        or len(parsed) != len(texts)
        or not all(isinstance(t, str) for t in parsed)
    ):
        raise ValueError(
            "paraphrase response was not a JSON array of " f"{len(texts)} string(s)"
        )
    return parsed


def paraphrase_journey(
    journey: Dict[str, Any],
    *,
    client: Optional[Any] = None,
    model: str = "gpt-4.1-mini",
    n: int = 1,
    temperature: float = 0.8,
) -> List[Dict[str, Any]]:
    """``n`` synthetic journeys with a fresh LLM-generated paraphrase of
    every real user turn in the final step, everything else byte-identical.

    Only user turns are reworded. The trainable target — the assistant's
    own output — has to stay exactly what was recorded, or the label a
    trainer would learn from no longer matches what actually happened; that
    is also why :func:`perturb_tool_calls` only ever touches the arguments
    of a *tool call*, never an assistant's text. A malformed or unparseable
    LLM response for one journey is skipped, not raised — a batch
    augmentation run over many journeys should not abort because one model
    call came back oddly formatted.

    ``client`` is an ``openai.OpenAI``-shaped object (or a test double
    exposing the same one method this needs —
    ``chat.completions.create()``), the same dependency-injection seam
    ``collect_from_object_store`` uses for ``boto3``. Omit it for a real
    ``openai.OpenAI()``, built lazily — see :func:`_default_client`.
    """
    if client is None:
        client = _default_client()
    steps = journey.get("steps") or []
    if not steps:
        return []
    messages = steps[-1].get("messages") or []
    user_indices = [
        i
        for i, m in enumerate(messages)
        if m.get("role") == "user"
        and isinstance(m.get("content"), str)
        and m["content"]
    ]
    if not user_indices:
        return []

    task = journey.get("task") or {}
    source_id = task.get("conversation_id") or task.get("id") or "journey"

    synthetic: List[Dict[str, Any]] = []
    for k in range(n):
        try:
            reworded = _paraphrase_texts(
                client,
                model,
                [messages[i]["content"] for i in user_indices],
                temperature=temperature,
            )
        except (json.JSONDecodeError, ValueError, TypeError, KeyError, AttributeError):
            continue

        new_messages = [dict(m) for m in messages]
        for idx, text in zip(user_indices, reworded):
            new_messages[idx] = {**new_messages[idx], "content": text}

        synthetic.append(
            {
                "task": {
                    **task,
                    "conversation_id": f"{source_id}__paraphrase_{k}",
                },
                "steps": [{"messages": new_messages, "trainable_status": "trainable"}],
                "telemetry": {
                    "source": "augmentation",
                    "data": {
                        "synthetic": True,
                        "augmentation": {
                            "kind": "paraphrase",
                            "source_journey_id": source_id,
                            "model": model,
                        },
                    },
                },
            }
        )
    return synthetic


_SYNTHETIC_NEGATIVE_PROMPT = (
    "You are generating a training example for a preference-learning "
    "dataset. Given the conversation below, write a response that a "
    "reasonable-sounding but meaningfully worse assistant might have given "
    "instead of the real, correct one -- plausible, on-topic, but wrong, "
    "incomplete, or lower-quality in some concrete way. Reply with nothing "
    "but the text of that worse response, no preamble or explanation.\n\n"
    "Conversation so far:\n{context}"
)


def generate_synthetic_negative(
    journey: Dict[str, Any],
    *,
    client: Optional[Any] = None,
    model: str = "gpt-4.1-mini",
    temperature: float = 1.0,
) -> Optional[Dict[str, Any]]:
    """One preference-pair-shaped synthetic journey: the real final
    assistant turn as the winning (``trainable``) candidate, an
    LLM-generated plausible-but-worse response to the same context as a
    losing (``superseded``) candidate immediately before it — the exact
    step-status chain :func:`odyssey.dpo.dpo_pairs` looks for ("a run of
    ``superseded`` steps immediately followed by one ``trainable`` step").

    This is what tool-call perturbation cannot produce: a *content*
    mistake, for a turn where nothing in the recorded trace actually
    failed. Real DPO data needs a rejected candidate that lost on
    substance, not just a malformed tool call.

    ``client`` — see :func:`paraphrase_journey`, same seam.

    Returns ``None`` when the journey has no final assistant turn to pair
    against, or when the LLM call fails or returns something unusable —
    never raises, so a batch run over many journeys is not aborted by one
    bad generation.
    """
    if client is None:
        client = _default_client()
    steps = journey.get("steps") or []
    if not steps:
        return None
    messages = steps[-1].get("messages") or []
    if not messages or messages[-1].get("role") != "assistant":
        return None
    chosen = messages[-1]
    context = messages[:-1]

    try:
        rejected_text = _chat(
            client,
            model,
            _SYNTHETIC_NEGATIVE_PROMPT.format(context=json.dumps(context, indent=2)),
            temperature=temperature,
        )
    except (TypeError, AttributeError):
        return None
    rejected_text = rejected_text.strip()
    if not rejected_text:
        return None

    task = journey.get("task") or {}
    source_id = task.get("conversation_id") or task.get("id") or "journey"
    rejected_message = {**chosen, "content": rejected_text, "tool_calls": None}

    return {
        "task": {**task, "conversation_id": f"{source_id}__synthetic_negative"},
        "steps": [
            {
                "messages": context + [rejected_message],
                "trainable_status": "superseded",
            },
            {"messages": context + [chosen], "trainable_status": "trainable"},
        ],
        "telemetry": {
            "source": "augmentation",
            "data": {
                "synthetic": True,
                "augmentation": {
                    "kind": "synthetic_negative",
                    "source_journey_id": source_id,
                    "model": model,
                },
            },
        },
    }
