"""Schema 2.1's provenance and timing, read off a folded journey.

The SDK writes `framework` on the header, `parent_journey_id` in
`journey_metadata` (a voice call's `<call_id>.llm` sibling), and
`provider`/`latency_ms`/`ttft_ms` on each assistant turn. This is the one place
that turns those per-event values into the per-journey ones the index stores
and the API returns, so a row in the `journeys` table and a `GET /journeys/{id}`
response cannot disagree about what a journey's latency was.

Averaged rather than summed: a journey's turns are a conversation, and the
number a deployment tunes against is what one reply costs, not what twenty of
them cost together.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional

from odyssey.fold import FoldResult
from odyssey.primitives import JourneyHeader, Message

__all__ = [
    "Provenance",
    "provenance",
    "turn_messages",
    "join_providers",
    "split_providers",
]


@dataclass(frozen=True)
class Provenance:
    """What recorded a journey, what served it, and how long it took."""

    framework: Optional[str] = None
    parent_journey_id: Optional[str] = None
    providers: List[str] = field(default_factory=list)
    avg_latency_ms: Optional[float] = None
    avg_ttft_ms: Optional[float] = None


def turn_messages(result: FoldResult) -> List[Message]:
    """Every message in the journey, once.

    ``Step.messages`` is cumulative, so the last step already holds the whole
    conversation and the earlier ones are prefixes of it.
    """
    steps = result.journey.steps
    return list(steps[-1].messages) if steps else []


def provenance(
    result: FoldResult, header: Optional[JourneyHeader] = None
) -> Provenance:
    """Fold one journey's provenance and timing into the shape both readers use."""
    tags = (header.journey_metadata if header is not None else None) or {}
    parent = tags.get("parent_journey_id")

    providers: List[str] = []
    latencies: List[float] = []
    ttfts: List[float] = []
    for message in turn_messages(result):
        if message.provider and message.provider not in providers:
            providers.append(message.provider)
        if isinstance(message.latency_ms, (int, float)):
            latencies.append(float(message.latency_ms))
        if isinstance(message.ttft_ms, (int, float)):
            ttfts.append(float(message.ttft_ms))

    return Provenance(
        framework=header.framework if header is not None else None,
        parent_journey_id=str(parent) if parent else None,
        # Sorted so two indexers of the same journey produce the same string.
        providers=sorted(providers),
        avg_latency_ms=_mean(latencies),
        avg_ttft_ms=_mean(ttfts),
    )


def _mean(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return round(sum(values) / len(values), 3)


def join_providers(providers: Any) -> Optional[str]:
    """The providers as one indexable column value."""
    return ",".join(providers) if providers else None


def split_providers(raw: Any) -> List[str]:
    """The column value back as a list. ``None`` and ``""`` are both empty."""
    return [p for p in str(raw or "").split(",") if p]
