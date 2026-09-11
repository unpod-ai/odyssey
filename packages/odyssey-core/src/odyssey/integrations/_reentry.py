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

import weakref
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any, Iterator, Tuple

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


# ---------------------------------------------------------------------------
# Calls a framework integration is already recording
# ---------------------------------------------------------------------------

# LangChain's handler records a model call itself, with the run tree and tool
# calls around it. The provider patch underneath (`ChatOpenAI` calls `openai`)
# would record the same turn a second time, into a different journey. The
# handler marks each of its model runs, and every provider patch stays out of a
# call made while one is still running.
#
# A mark names its owner and run rather than being a bare counter, and counts
# only while the owner says that run is still going. A context can end up
# holding a mark whose end it never saw -- a callback delivered elsewhere, a
# test driving callbacks by hand -- and a bare counter would then silence
# provider capture for the rest of that task.
_framework_marks: ContextVar[Tuple[Tuple[Any, str], ...]] = ContextVar(
    "odyssey_framework_llm", default=()
)


def enter_framework_call(owner: Any, run_id: str) -> Token:
    """Mark that ``owner`` is recording its model run ``run_id``.

    ``owner`` must expose ``is_running(run_id) -> bool``. Held weakly, so a
    discarded handler's marks lapse with it.
    """
    mark = (weakref.ref(owner), run_id)
    return _framework_marks.set(_framework_marks.get() + (mark,))


def exit_framework_call(token: Token) -> None:
    """Undo :func:`enter_framework_call` in the context that set it. Never raises."""
    try:
        _framework_marks.reset(token)
    except ValueError:
        # A different context. The mark lapses there once its run ends.
        pass


def in_framework_call() -> bool:
    for ref, run_id in _framework_marks.get():
        owner = ref()
        if owner is None:
            continue
        try:
            if owner.is_running(run_id):
                return True
        except Exception:  # noqa: BLE001 - a broken owner must not block capture
            continue
    return False
