"""Helpers for assembling :class:`Reward` objects from scalar scores.

Only :func:`build_reward_from_scalar` is exposed. A rubric-based variant
existed in earlier drafts but no real customer export carries per-category
rubric scores today; callers with rubric outputs (e.g. LLM-judge results)
can construct :class:`Reward` + :class:`RewardComponent` directly.
"""

from __future__ import annotations

from odyssey.primitives import Reward, RewardComponent


def build_reward_from_scalar(
    value: float,
    *,
    name: str = "score",
    score_range: tuple[float, float] = (0.0, 1.0),
    weight: float = 1.0,
) -> Reward:
    """Build a single-component :class:`Reward` from a bare numeric score.

    ``scaled_value`` normalizes ``value`` to ``[0, 1]`` using ``score_range``.
    Raises ``ValueError`` if ``score_range`` is not strictly increasing.
    """
    lo, hi = score_range
    if hi <= lo:
        raise ValueError(f"score_range must be increasing, got {score_range!r}")
    raw = float(value)
    scaled = (raw - lo) / (hi - lo)
    component = RewardComponent(
        name=name,
        value=raw,
        scaled_value=scaled,
        weight=weight,
        range=(lo, hi),
    )
    return Reward(
        aggregated_value=scaled,
        aggregation_method="identity",
        components=[component],
    )
