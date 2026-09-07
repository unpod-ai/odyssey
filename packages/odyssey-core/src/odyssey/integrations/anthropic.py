"""Anthropic capture — a drop-in client, plus an opt-in in-place patch.

Two ways to attach, one implementation of what gets captured (``_base``).

**Drop-in (the default path).** Change the import, nothing else::

    from odyssey.integrations.anthropic import Anthropic
    client = Anthropic()                       # same args as the real one
    client.messages.create(...)                # recorded

**Patch (opt-in).** For when the call sites cannot be edited::

    odyssey.init(instrument=["anthropic"])     # existing clients now record

The drop-in is the default because a patched call stack is harder to read in a
traceback and harder to reason about when two libraries patch the same method.
Patching is the escape hatch, not the recommendation.

Both paths never change what the caller sees: the provider's return value is
passed through untouched, and a provider exception propagates unchanged. Capture
failures are swallowed and counted — see ``odyssey.health()``.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from odyssey.capture import journey
from odyssey.client import require_client
from odyssey.integrations._base import capture_request, capture_response
from odyssey.integrations._reentry import outermost
from odyssey.integrations._timing import Timer

# Set by instrument(); cleared by uninstrument(). Module-level because patching
# is a process-wide act and must be reversible exactly once.
_patched: Dict[str, Any] = {}


def _safe(label: str, fn: Callable[[], None]) -> None:
    """Run a capture step. A failure here must never reach the caller."""
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - capture is best-effort by contract
        client = require_client()
        if client is not None:
            client.note_error(label, exc)


def _record_call(kwargs: Dict[str, Any], call: Callable[[], Any]) -> Any:
    """Capture request, run the provider call, capture response. Order matters.

    The request is recorded *before* the call so a provider timeout still leaves
    the prompt in the corpus — a journey that shows what was asked and then
    terminates with an error is useful; one that shows nothing is not.
    """
    with outermost() as mine:
        if not mine:
            # An outer wrapper is already recording this call -- the drop-in
            # client with the in-place patch underneath it. See `_reentry`.
            return call()
        with journey():
            _safe("anthropic.request", lambda: capture_request(kwargs))
            timer = Timer()
            result = call()
            # Read before the capture step, so the number is the provider's
            # latency and not odyssey's own parsing of the response.
            elapsed = timer.latency_ms
            _safe(
                "anthropic.response",
                lambda: capture_response(
                    result, model=kwargs.get("model"), latency_ms=elapsed
                ),
            )
            return result


class _MessagesProxy:
    """Wraps ``client.messages``, capturing ``create`` and ``stream``."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def create(self, *args: Any, **kwargs: Any) -> Any:
        return _record_call(kwargs, lambda: self._inner.create(*args, **kwargs))

    def stream(self, *args: Any, **kwargs: Any) -> Any:
        """Capture the assembled final message, not individual chunks.

        Per-token events would flood the spool and are not training data — the
        corpus wants the turn the model produced, not how it arrived.
        """
        return _StreamProxy(self._inner.stream(*args, **kwargs), kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _StreamProxy:
    """Defers capture until the stream closes and a final message exists."""

    def __init__(self, inner: Any, kwargs: Dict[str, Any]) -> None:
        self._inner = inner
        self._kwargs = kwargs
        self._journey: Any = None
        self._handle: Any = None

    def __enter__(self) -> Any:
        self._journey = journey()
        self._handle = self._journey.__enter__()
        _safe("anthropic.request", lambda: capture_request(self._kwargs))
        # Started after the request capture and carried into the body, because
        # a stream's latency is measured from the call to the final message,
        # which arrives in a different object than the one that opened it.
        return _StreamBody(self._inner.__enter__(), self._kwargs, Timer())

    def __exit__(self, *exc: Any) -> Any:
        try:
            return self._inner.__exit__(*exc)
        finally:
            if self._journey is not None:
                self._journey.__exit__(*exc)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _StreamBody:
    """The object a ``with client.messages.stream(...)`` block receives."""

    def __init__(
        self, inner: Any, kwargs: Dict[str, Any], timer: Optional[Timer] = None
    ) -> None:
        self._inner = inner
        self._kwargs = kwargs
        self._captured = False
        self._timer = timer

    def get_final_message(self) -> Any:
        message = self._inner.get_final_message()
        self._capture(message)
        return message

    def _capture(self, message: Any) -> None:
        if self._captured:
            return
        self._captured = True
        timer = self._timer
        latency = timer.latency_ms if timer is not None else None
        ttft = timer.ttft_ms if timer is not None else None
        _safe(
            "anthropic.response",
            lambda: capture_response(
                message,
                model=self._kwargs.get("model"),
                latency_ms=latency,
                ttft_ms=ttft,
            ),
        )

    def _tick(self, chunk: Any) -> Any:
        """Mark time-to-first-token. Idempotent, so it can run per chunk."""
        if self._timer is not None:
            self._timer.first_token()
        return chunk

    @property
    def text_stream(self) -> Any:
        return (self._tick(chunk) for chunk in self._inner.text_stream)

    def __iter__(self) -> Any:
        return (self._tick(event) for event in self._inner)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class Anthropic:
    """Drop-in replacement for ``anthropic.Anthropic``.

    Accepts the same arguments and forwards every attribute it does not wrap, so
    swapping the import is the whole change. The provider is imported here rather
    than at module scope, which is what keeps ``odyssey-core`` dependency-free.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # pyrefly: ignore[missing-import]  — optional extra, `odyssey[anthropic]`.
        # Absent by design in a default install; that is what keeps core's
        # `dependencies = []` true, so the checker cannot resolve it here.
        from anthropic import Anthropic as _Real

        self._inner = _Real(*args, **kwargs)
        self.messages = _MessagesProxy(self._inner.messages)

    @property
    def inner(self) -> Any:
        """The wrapped provider client, for anything this proxy does not cover."""
        return self._inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class AsyncAnthropic:
    """Drop-in replacement for ``anthropic.AsyncAnthropic``."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # pyrefly: ignore[missing-import]  — optional extra, see Anthropic above.
        from anthropic import AsyncAnthropic as _Real

        self._inner = _Real(*args, **kwargs)
        self.messages = _AsyncMessagesProxy(self._inner.messages)

    @property
    def inner(self) -> Any:
        return self._inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _AsyncMessagesProxy:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def create(self, *args: Any, **kwargs: Any) -> Any:
        with outermost() as mine:
            if not mine:
                return await self._inner.create(*args, **kwargs)
            with journey():
                _safe("anthropic.request", lambda: capture_request(kwargs))
                timer = Timer()
                result = await self._inner.create(*args, **kwargs)
                # Read before the capture step, so the number is the provider's
                # latency and not odyssey's own parsing of the response.
                elapsed = timer.latency_ms
                _safe(
                    "anthropic.response",
                    lambda: capture_response(
                        result, model=kwargs.get("model"), latency_ms=elapsed
                    ),
                )
                return result

    def stream(self, *args: Any, **kwargs: Any) -> Any:
        """Async counterpart to :meth:`_MessagesProxy.stream` — same deferred
        capture, same "final message, not chunks" rule."""
        return _AsyncStreamProxy(self._inner.stream(*args, **kwargs), kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _AsyncStreamProxy:
    """Async counterpart to :class:`_StreamProxy`."""

    def __init__(self, inner: Any, kwargs: Dict[str, Any]) -> None:
        self._inner = inner
        self._kwargs = kwargs
        self._journey: Any = None
        self._handle: Any = None

    async def __aenter__(self) -> Any:
        self._journey = journey()
        self._handle = self._journey.__enter__()
        _safe("anthropic.request", lambda: capture_request(self._kwargs))
        return _AsyncStreamBody(await self._inner.__aenter__(), self._kwargs, Timer())

    async def __aexit__(self, *exc: Any) -> Any:
        try:
            return await self._inner.__aexit__(*exc)
        finally:
            if self._journey is not None:
                self._journey.__exit__(*exc)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _AsyncStreamBody:
    """Async counterpart to :class:`_StreamBody`."""

    def __init__(
        self, inner: Any, kwargs: Dict[str, Any], timer: Optional[Timer] = None
    ) -> None:
        self._inner = inner
        self._kwargs = kwargs
        self._captured = False
        self._timer = timer

    async def get_final_message(self) -> Any:
        message = await self._inner.get_final_message()
        self._capture(message)
        return message

    def _capture(self, message: Any) -> None:
        if self._captured:
            return
        self._captured = True
        timer = self._timer
        latency = timer.latency_ms if timer is not None else None
        ttft = timer.ttft_ms if timer is not None else None
        _safe(
            "anthropic.response",
            lambda: capture_response(
                message,
                model=self._kwargs.get("model"),
                latency_ms=latency,
                ttft_ms=ttft,
            ),
        )

    def _tick(self, chunk: Any) -> Any:
        if self._timer is not None:
            self._timer.first_token()
        return chunk

    async def _ticked(self, source: Any) -> Any:
        async for chunk in source:
            yield self._tick(chunk)

    @property
    def text_stream(self) -> Any:
        return self._ticked(self._inner.text_stream)

    def __aiter__(self) -> Any:
        return self._ticked(self._inner)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


# ---------------------------------------------------------------------------
# Opt-in patching
# ---------------------------------------------------------------------------


def instrument(target: Optional[Any] = None) -> None:
    """Patch ``anthropic`` in place so existing clients record.

    Idempotent. ``target`` overrides the module to patch, which is what makes
    this testable without the real SDK installed.
    """
    if _patched:
        return
    if target is None:
        import anthropic.resources.messages as target  # type: ignore[no-redef]

    cls = getattr(target, "Messages", None)
    if cls is None or not hasattr(cls, "create"):
        raise AttributeError(
            "anthropic.resources.messages.Messages.create not found; "
            "this anthropic version is not supported by instrument()"
        )

    original = cls.create

    def patched(self: Any, *args: Any, **kwargs: Any) -> Any:
        return _record_call(kwargs, lambda: original(self, *args, **kwargs))

    patched.__wrapped__ = original  # type: ignore[attr-defined]
    cls.create = patched
    _patched["cls"] = cls
    _patched["create"] = original


def uninstrument() -> None:
    """Undo :func:`instrument`. Safe to call when nothing was patched."""
    cls = _patched.pop("cls", None)
    original = _patched.pop("create", None)
    if cls is not None and original is not None:
        cls.create = original


def is_instrumented() -> bool:
    return bool(_patched)
