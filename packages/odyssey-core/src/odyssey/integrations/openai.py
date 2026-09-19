"""OpenAI capture — and every provider reached through the ``openai`` SDK.

**In place (the default).** ``odyssey.init()``'s ``instrument="auto"`` patches
the SDK, so every existing client records — sync and async, streamed and not,
including ``with_raw_response`` (LangChain's path). Groq, xAI, Cerebras,
OpenRouter, DeepInfra, Sarvam, Azure, Ollama and any other OpenAI-compatible
host are the same SDK pointed at another ``base_url``, so they are captured by
the same code; :mod:`odyssey.integrations.providers` names each one from that
URL and is where per-provider logic is registered.

**Drop-in.** The explicit wrapper still works and still reads better in a
traceback::

    from odyssey.integrations.openai import OpenAI
    client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key="...")

A streamed call is recorded once the caller has drained it: chunks are folded
back into one assistant turn, time-to-first-token included. A stream cut off
early — a voice barge-in cancels it — still records what arrived, marked
``incomplete``.

Calls a framework integration is already recording (LangChain's handler) are
left to it. The provider's return value is always passed through untouched
(wrapped only to observe a stream), and a provider exception propagates
unchanged; capture failures are counted in ``odyssey.health()``.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from odyssey.integrations._call import (
    Capture,
    capture_async,
    capture_sync,
    provider_from_base_url,
)
from odyssey.integrations._openai_base import (
    PROVIDER,
    ChunkAccumulator,
    capture_request,
    capture_response,
    record_stream,
)

# Set by instrument(); cleared by uninstrument(). Module-level because patching
# is a process-wide act and must be reversible exactly once.
_patched: Dict[str, Any] = {}

CAPTURE = Capture(
    label="openai",
    request=capture_request,
    response=capture_response,
    streamed=record_stream,
    accumulator=ChunkAccumulator,
    provider=provider_from_base_url(PROVIDER),
)


# ---------------------------------------------------------------------------
# The drop-in client
# ---------------------------------------------------------------------------


class _CompletionsProxy:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def create(self, *args: Any, **kwargs: Any) -> Any:
        return capture_sync(
            CAPTURE, self._inner, kwargs, lambda: self._inner.create(*args, **kwargs)
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _ChatProxy:
    def __init__(self, inner: Any) -> None:
        self.completions = _CompletionsProxy(inner.completions)
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class OpenAI:
    """Drop-in replacement for ``openai.OpenAI``.

    Accepts the same arguments and forwards every attribute it does not wrap,
    so swapping the import is the whole change. The provider is imported here
    rather than at module scope, which is what keeps ``odyssey-core``
    dependency-free.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # pyrefly: ignore[missing-import]  — optional extra, `odyssey[openai]`.
        from openai import OpenAI as _Real

        self._inner = _Real(*args, **kwargs)
        self.chat = _ChatProxy(self._inner.chat)

    @property
    def inner(self) -> Any:
        """The wrapped provider client, for anything this proxy does not cover."""
        return self._inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class AsyncOpenAI:
    """Drop-in replacement for ``openai.AsyncOpenAI``."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # pyrefly: ignore[missing-import]  — optional extra, see OpenAI above.
        from openai import AsyncOpenAI as _Real

        self._inner = _Real(*args, **kwargs)
        self.chat = _AsyncChatProxy(self._inner.chat)

    @property
    def inner(self) -> Any:
        return self._inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _AsyncChatProxy:
    def __init__(self, inner: Any) -> None:
        self.completions = _AsyncCompletionsProxy(inner.completions)
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _AsyncCompletionsProxy:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def create(self, *args: Any, **kwargs: Any) -> Any:
        return await capture_async(
            CAPTURE, self._inner, kwargs, lambda: self._inner.create(*args, **kwargs)
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


# ---------------------------------------------------------------------------
# In-place patching
# ---------------------------------------------------------------------------


def instrument(target: Optional[Any] = None) -> None:
    """Patch ``openai`` in place so every existing client records.

    Patches ``Completions.create`` and, when present, ``AsyncCompletions.create``
    — every OpenAI-compatible provider, LiveKit's and Pipecat's LLM services
    included, goes through one of the two. Idempotent. ``target`` overrides the
    module to patch, which is what makes this testable without the real SDK.
    """
    if _patched:
        return
    if target is None:
        import openai.resources.chat.completions as target  # type: ignore[no-redef]

    sync_cls = getattr(target, "Completions", None)
    if sync_cls is None or not hasattr(sync_cls, "create"):
        raise AttributeError(
            "openai.resources.chat.completions.Completions.create not found; "
            "this openai version is not supported by instrument()"
        )

    original = sync_cls.create

    def patched(self: Any, *args: Any, **kwargs: Any) -> Any:
        return capture_sync(
            CAPTURE, self, kwargs, lambda: original(self, *args, **kwargs)
        )

    patched.__wrapped__ = original  # type: ignore[attr-defined]
    sync_cls.create = patched
    _patched["cls"] = sync_cls
    _patched["create"] = original

    async_cls = getattr(target, "AsyncCompletions", None)
    if async_cls is not None and hasattr(async_cls, "create"):
        original_async = async_cls.create

        async def patched_async(self: Any, *args: Any, **kwargs: Any) -> Any:
            return await capture_async(
                CAPTURE, self, kwargs, lambda: original_async(self, *args, **kwargs)
            )

        patched_async.__wrapped__ = original_async  # type: ignore[attr-defined]
        async_cls.create = patched_async
        _patched["async_cls"] = async_cls
        _patched["create_async"] = original_async


def uninstrument() -> None:
    """Undo :func:`instrument`. Safe to call when nothing was patched."""
    for cls_key, fn_key in (("cls", "create"), ("async_cls", "create_async")):
        cls = _patched.pop(cls_key, None)
        original = _patched.pop(fn_key, None)
        if cls is not None and original is not None:
            cls.create = original


def is_instrumented() -> bool:
    return bool(_patched)
