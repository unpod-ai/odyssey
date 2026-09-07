"""How long a provider call took, and who made it — shared by every wrapper.

Timing is measured around the provider call in each integration's
``_record_call`` and stamped onto the response turn here, so the three
provider bases (``_base``, ``_openai_base``, ``_gemini_base``) agree on what a
duration means without importing one another.

**Measured around the call, not inside it.** ``perf_counter`` starts after the
request has been captured and stops before the response is, so the number is
the provider's latency and not odyssey's own bookkeeping. A monotonic clock
rather than ``time.time`` because a wall-clock adjustment mid-call would
otherwise produce a negative duration.

**The response turn carries it, not the request turn.** A request has no
duration of its own, and putting the pair's latency on both halves would double
it for any consumer that sums the column.

**Never overwrites what the caller already set.** A streaming wrapper that
measured its own time-to-first-token knows more than the surrounding timer
does.
"""

from __future__ import annotations

import dataclasses
import time
from typing import Optional

from odyssey.primitives import Message


class Timer:
    """Monotonic stopwatch for one provider call.

    ``first_token()`` is a no-op after the first call, so a streaming wrapper
    can invoke it on every chunk without tracking whether it already has.
    """

    __slots__ = ("_started", "_ttft")

    def __init__(self) -> None:
        self._started = time.perf_counter()
        self._ttft: Optional[float] = None

    def first_token(self) -> None:
        if self._ttft is None:
            self._ttft = (time.perf_counter() - self._started) * 1000

    @property
    def latency_ms(self) -> float:
        return round((time.perf_counter() - self._started) * 1000, 3)

    @property
    def ttft_ms(self) -> Optional[float]:
        return None if self._ttft is None else round(self._ttft, 3)


def stamp(
    message: Message,
    *,
    latency_ms: Optional[float] = None,
    ttft_ms: Optional[float] = None,
    provider: Optional[str] = None,
) -> Message:
    """Attach timing and provider to a turn, leaving set values alone."""
    latency = message.latency_ms if message.latency_ms is not None else latency_ms
    ttft = message.ttft_ms if message.ttft_ms is not None else ttft_ms
    name = message.provider if message.provider is not None else provider
    if (latency, ttft, name) == (
        message.latency_ms,
        message.ttft_ms,
        message.provider,
    ):
        # Nothing to add. Returning the same object keeps the common path free
        # of an allocation, and makes "was this stamped" answerable by identity.
        return message
    return dataclasses.replace(message, latency_ms=latency, ttft_ms=ttft, provider=name)
