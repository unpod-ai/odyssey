"""Pure helpers for counting and aggregating journey metrics."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from odyssey.primitives import Message


def count_tool_calls(messages: List[Message]) -> int:
    """Total tool calls issued across assistant messages."""
    return sum(
        len(m.tool_calls) for m in messages if m.role == "assistant" and m.tool_calls
    )


def count_tool_failures(messages: List[Message]) -> int:
    """Tool responses that carry an error payload."""
    return sum(
        1
        for m in messages
        if m.role == "tool" and m.tool_response and m.tool_response.error
    )


def tool_error_rate(messages: List[Message]) -> float:
    """Ratio of failed tool responses to total tool calls; 0.0 when no calls."""
    calls = count_tool_calls(messages)
    if calls == 0:
        return 0.0
    return count_tool_failures(messages) / calls


def parse_total_time(start: Optional[str], end: Optional[str]) -> Optional[float]:
    """Seconds between two ISO-8601 timestamps, or None if unparseable."""
    if not start or not end:
        return None
    try:
        t0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(end.replace("Z", "+00:00"))
        return (t1 - t0).total_seconds()
    except (ValueError, TypeError, AttributeError):
        return None


def aggregate_tokens(runs: List[Dict[str, Any]]) -> Dict[str, int]:
    """Sum ``total_tokens``/``prompt_tokens``/``completion_tokens`` across runs."""
    return {
        k: sum(r.get(k) or 0 for r in runs)
        for k in ("total_tokens", "prompt_tokens", "completion_tokens")
    }


def aggregate_cost(runs: List[Dict[str, Any]]) -> float:
    """Sum ``total_cost`` across runs."""
    return sum(float(r.get("total_cost") or 0) for r in runs)
