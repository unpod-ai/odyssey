"""Tool-call accuracy (item 7.3) — reuses `odyssey.primitives.JourneyMetrics`'
already-computed `tool_error_rate` rather than re-deriving correctness from
raw text. Scores a captured journey's execution (the caller passes the
journey's `metrics.tool_error_rate`, e.g. via `fold()`'s own output), not a
free-text completion — a different shape from `exact_match`, which is why
this is a separate metric module rather than a branch inside one.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def score(
    response: Dict[str, Any], reference: Optional[Dict[str, Any]] = None
) -> float:
    """``response`` is expected to carry a `tool_error_rate` key (as produced
    by `JourneyMetrics.tool_error_rate`). ``reference`` is accepted for
    signature symmetry with other metrics but unused — tool-call accuracy is
    self-contained in the journey's own execution, not compared against a
    ground truth. No tool calls at all (``tool_error_rate`` is ``None``)
    scores 1.0 — nothing failed."""
    error_rate = response.get("tool_error_rate")
    if error_rate is None:
        return 1.0
    return 1.0 - error_rate
