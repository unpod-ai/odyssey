"""One provider call's recording scope, shared by the SDK patches.

A non-streamed call starts and finishes inside the function that made it, so
``with journey():`` fits. A streamed one does not: ``create(stream=True)``
returns before any token has arrived, and the turn exists only once the caller
has drained the stream, possibly much later. Holding ``journey()`` open across
that gap would leave its context set in the caller's own code for as long as
the stream lived. A :class:`CallScope` holds the journey as an object instead
and binds it only around each capture step.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from odyssey.capture import JourneyHandle, _open_context
from odyssey.client import require_client
from odyssey.context import JourneyContext, bind, current
from odyssey.integrations._linked import linked_journey


def safe(label: str, fn: Callable[[], Any]) -> None:
    """Run a capture step. A failure here must never reach the caller."""
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - capture is best-effort by contract
        client = require_client()
        if client is not None:
            client.note_error(label, exc)


class CallScope:
    """Where one provider call records.

    Joins the ambient journey when there is one — the call belongs to whatever
    the caller already scoped — and otherwise the task's linked ``.llm``
    journey (see ``_linked``). Failing both, it opens a journey of its own and
    closes it when the call ends.

    A terminated ambient journey is not joined. A provider call made after a
    recorder closed must not append events past that journey's terminal one.
    """

    __slots__ = ("ctx", "owned", "_closed")

    def __init__(self) -> None:
        ambient = current()
        if ambient is None or ambient.terminated:
            ambient = linked_journey()
        if ambient is not None:
            self.ctx: JourneyContext = ambient
            self.owned = False
        else:
            self.ctx = _open_context(None, data_source=None, trace_id=None, metadata={})
            self.owned = True
        self._closed = False

    def run(self, label: str, fn: Callable[[], Any]) -> None:
        with bind(self.ctx):
            safe(label, fn)

    def close(self, exc: Optional[BaseException] = None) -> None:
        """End the call. Closes the journey only if this scope opened it."""
        if self._closed:
            return
        self._closed = True
        if not self.owned or self.ctx.terminated:
            return

        def end() -> None:
            handle = JourneyHandle(self.ctx)
            if exc is None:
                handle.close()
            else:
                handle.close(reason="ERROR", error=f"{type(exc).__name__}: {exc}")

        self.run("scope.close", end)
