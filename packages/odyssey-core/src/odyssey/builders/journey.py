"""Assemble :class:`Journey` objects from structured inputs.

Two entry points:

- :func:`build_journey_from_messages` -- start from a flat list of
  :class:`Message` objects. This is what customer ingestion scripts should
  use when their source data is CSV/JSONL/etc. rather than a provider API.
- :func:`build_journey_from_parsed` -- start from a
  :class:`ParsedConversation`. Provider subclasses delegate to this so the
  ``ParsedConversation → Journey`` logic has exactly one implementation.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, List, Optional

from odyssey.builders.metrics import (
    count_tool_calls,
    count_tool_failures,
    parse_total_time,
)
from odyssey.builders.steps import build_cumulative_steps
from odyssey.hashing import content_hash, idempotency_key
from odyssey.primitives import (
    ExecutionMetrics,
    Journey,
    JourneyMetrics,
    Message,
    ParsedConversation,
    Reward,
    Task,
    Telemetry,
    TerminationReason,
)


def build_journey_from_messages(
    messages: List[Message],
    *,
    conversation_id: str,
    data_source: str,
    reward: Optional[Reward] = None,
    task_metadata: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    termination_reason: Optional[TerminationReason] = None,
    extra_telemetry: Optional[Dict[str, Any]] = None,
    trace_id: Optional[str] = None,
    model_id: Optional[str] = None,
) -> Journey:
    """Build a :class:`Journey` from a flat list of :class:`Message` objects.

    The cumulative step list, tool-call metrics, and telemetry (content hash,
    idempotency key) are computed automatically.

    ``task_metadata`` is flattened into :class:`Task` as ``num_turns`` /
    ``total_tokens`` / ``total_cost`` when those keys are present; anything
    else is ignored so callers can freely pass richer dicts without failing.
    """
    steps = build_cumulative_steps(messages)

    metadata = task_metadata or {}
    task = Task(
        id=f"{data_source}:{conversation_id}",
        data_source=data_source,
        conversation_id=conversation_id,
        num_turns=metadata.get("num_turns"),
        num_steps=len(steps),
        total_tokens=metadata.get("total_tokens"),
        total_cost=metadata.get("total_cost"),
    )

    n_calls = count_tool_calls(messages)
    n_fails = count_tool_failures(messages)
    journey_metrics = JourneyMetrics(
        steps=len(steps),
        num_tool_calls=n_calls,
        num_tool_failures=n_fails,
        tool_error_rate=(n_fails / n_calls) if n_calls > 0 else 0.0,
        tokens_generated=metadata.get("completion_tokens"),
    )

    total_time = parse_total_time(start_time, end_time)
    resolved_termination: Optional[TerminationReason] = termination_reason
    if resolved_termination is None:
        resolved_termination = "ERROR" if error else "ENV_DONE"
    execution_metrics = ExecutionMetrics(
        total_time=total_time,
        termination_reason=resolved_termination,
    )

    telemetry_data: Dict[str, Any] = {
        "conversation_id": conversation_id,
    }
    if extra_telemetry:
        telemetry_data.update(extra_telemetry)
    if trace_id is not None:
        telemetry_data["trace_id"] = trace_id

    traj_dict_for_hash = asdict(
        Journey(
            task=task,
            steps=steps,
            model_id=model_id,
            reward=reward,
            metrics=journey_metrics,
            execution_metrics=execution_metrics,
        )
    )
    c_hash = content_hash(traj_dict_for_hash)
    telemetry_data["content_hash"] = c_hash
    telemetry_data["idempotency_key"] = idempotency_key(
        data_source, conversation_id, c_hash
    )

    return Journey(
        task=task,
        steps=steps,
        trace_id=trace_id,
        model_id=model_id,
        reward=reward,
        metrics=journey_metrics,
        execution_metrics=execution_metrics,
        telemetry=Telemetry(source=data_source, data=telemetry_data),
        error=error,
    )


def build_journey_from_parsed(parsed: ParsedConversation) -> Journey:
    """Build a :class:`Journey` from a :class:`ParsedConversation`.

    Mirrors the legacy ``BaseProvider._build_journey`` behavior exactly so
    provider subclasses can delegate here without semantic drift.
    """
    all_messages: List[Message] = []
    for turn in parsed.turns:
        all_messages.extend(turn.messages)

    steps = build_cumulative_steps(all_messages)

    total_tokens = (
        sum(t.token_counts.get("total_tokens", 0) for t in parsed.turns) or None
    )
    total_cost = sum(t.total_cost or 0 for t in parsed.turns) or None
    completion_tokens = (
        sum(t.token_counts.get("completion_tokens", 0) for t in parsed.turns) or None
    )

    task = Task(
        id=f"{parsed.data_source}:{parsed.conversation_id}",
        data_source=parsed.data_source,
        conversation_id=parsed.conversation_id,
        num_turns=parsed.num_turns,
        num_steps=len(steps),
        total_tokens=total_tokens,
        total_cost=total_cost,
    )

    first_start = next((t.start_time for t in parsed.turns if t.start_time), None)
    last_end = next((t.end_time for t in reversed(parsed.turns) if t.end_time), None)
    total_time = parse_total_time(first_start, last_end)

    has_error = any(t.error for t in parsed.turns)
    execution_metrics = ExecutionMetrics(
        total_time=total_time,
        termination_reason="ERROR" if has_error else "ENV_DONE",
    )

    n_calls = count_tool_calls(all_messages)
    n_fails = count_tool_failures(all_messages)
    journey_metrics = JourneyMetrics(
        steps=len(steps),
        num_tool_calls=n_calls,
        num_tool_failures=n_fails,
        tool_error_rate=(n_fails / n_calls) if n_calls > 0 else 0.0,
        tokens_generated=completion_tokens,
    )

    traj_dict_for_hash = asdict(
        Journey(
            task=task,
            steps=steps,
            metrics=journey_metrics,
            execution_metrics=execution_metrics,
        )
    )
    c_hash = content_hash(traj_dict_for_hash)
    i_key = idempotency_key(parsed.data_source, parsed.conversation_id, c_hash)

    telemetry_data: Dict[str, Any] = {
        "source_run_ids": [t.source_run_id for t in parsed.turns],
        "conversation_id": parsed.conversation_id,
        "content_hash": c_hash,
        "idempotency_key": i_key,
    }
    if parsed.trace_id is not None:
        telemetry_data["trace_id"] = parsed.trace_id

    telemetry = Telemetry(source=parsed.data_source, data=telemetry_data)

    first_error = next((t.error for t in parsed.turns if t.error), None)

    return Journey(
        task=task,
        steps=steps,
        trace_id=parsed.trace_id,
        metrics=journey_metrics,
        execution_metrics=execution_metrics,
        telemetry=telemetry,
        error=first_error,
    )
