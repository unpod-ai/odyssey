"""One provider call, one recorded turn — even with two wrappers attached.

Two capture paths can be attached to the same call at once, and after
``odyssey.init()`` defaulted to ``instrument="auto"`` that combination is the
normal case rather than a mistake: an app that still uses the explicit drop-in
client now *also* has the in-place patch active underneath it. The proxy's
``create`` calls the real ``Completions.create``, which is the patched one, and
without a guard the assistant's turn lands in the corpus twice.

Duplicated turns are the worst kind of corpus bug. The fold deduplicates on
``event_id`` and each recording carries a fresh one, so nothing downstream can
tell the copies apart — a corpus with every answer twice looks like a corpus,
and trains like a broken one.

The outermost attachment wins. It is the one closest to the caller, so it sees
the arguments the application actually passed rather than whatever a client
layer rewrote them into.

A ``ContextVar`` rather than a thread local: the async wrappers await inside the
guarded region, and a thread local would leak the flag across every coroutine
sharing that thread.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_capturing: ContextVar[bool] = ContextVar("odyssey_capturing", default=False)


@contextmanager
def outermost() -> Iterator[bool]:
    """Yield ``True`` when this is the outermost capture of a provider call.

    Yields ``False`` when a capture is already in progress further out, and the
    caller should then run the provider call and record nothing.

    Safe to nest and always restores the previous value, so a provider
    exception inside the block cannot leave capture wedged off for the rest of
    the request.
    """
    if _capturing.get():
        yield False
        return
    token = _capturing.set(True)
    try:
        yield True
    finally:
        _capturing.reset(token)
