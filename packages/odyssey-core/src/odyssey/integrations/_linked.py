"""The provider-call journey that rides alongside a voice call.

LiveKit's and Pipecat's recorders write what was said. The provider SDK patches
(``instrument="auto"``) see something else: the exact request each LLM call
sent and what came back, with tools, parameters, latency and usage. Writing
both into one journey would record every turn twice from two vantage points;
opening a journey per LLM call would scatter one conversation across dozens of
unlinked shards.

So each attached call gets one sibling journey, ``<journey_id>.llm``, carrying
the call's tags plus ``parent_journey_id``. Attaching makes it the linked
journey of the attaching task, which is why ``attach`` must run before the
session or pipeline starts: every task the framework spawns from then on
inherits it, and every provider call those tasks make — the main LLM, a filler
model, a LangGraph node — lands in it.

Linked, not ambient. :func:`odyssey.context.current` is untouched, so
``journey()`` and the application's own recording mean exactly what they did;
only provider captures (``_scope.CallScope``) and the LangChain handler consult
:func:`linked_journey`, and only when no ambient journey is open.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Optional

from odyssey.capture import JourneyHandle
from odyssey.client import require_client
from odyssey.context import JourneyContext, SeqAllocator, bind
from odyssey.primitives import TerminationReason

LLM_SUFFIX = ".llm"
PARENT_KEY = "parent_journey_id"

_LINKED: ContextVar[Optional["LinkedLLMJourney"]] = ContextVar(
    "odyssey_linked_llm", default=None
)


def linked_journey() -> Optional[JourneyContext]:
    """The ``.llm`` journey provider calls in this task should join, if any.

    ``None`` once it has closed, and ``None`` if it belongs to a client that has
    since been shut down — a context var outlives the client that set it.
    """
    link = _LINKED.get()
    if link is None or link.ctx.terminated or link.client is not require_client():
        return None
    return link.ctx


class LinkedLLMJourney:
    """``<parent>.llm``, linked from construction until :meth:`close`."""

    def __init__(self, parent: JourneyContext) -> None:
        self.client: Any = require_client()
        tags = dict(parent.metadata)
        tags[PARENT_KEY] = parent.journey_id
        self.ctx = JourneyContext(
            journey_id=parent.journey_id + LLM_SUFFIX,
            allocator=(
                self.client.allocator
                if self.client is not None
                else SeqAllocator(lambda _jid: None)
            ),
            metadata=tags,
            data_source=parent.data_source,
        )
        # Sampled with its call, never independently: half a call is useless.
        self.ctx.state["_sampled"] = parent.state.get("_sampled", True)
        self._previous = _LINKED.get()
        _LINKED.set(self)
        self._closed = False

    def close(
        self, *, reason: TerminationReason = "NONE", error: Optional[str] = None
    ) -> None:
        """End the journey if anything was recorded, and stop being linked.

        Terminated either way, so a provider call made after the call ended
        opens its own journey instead of appending past this one's end. When
        closed from another task (a session "close" event) the attaching task
        still holds this link, but a terminated one is never joined.
        """
        if self._closed:
            return
        self._closed = True
        if self.ctx._header is not None and not self.ctx.terminated:
            with bind(self.ctx):
                JourneyHandle(self.ctx).close(reason=reason, error=error)
            if self.client is not None:
                self.client.count_journey()
        self.ctx.terminated = True
        if _LINKED.get() is self:
            _LINKED.set(self._previous)
