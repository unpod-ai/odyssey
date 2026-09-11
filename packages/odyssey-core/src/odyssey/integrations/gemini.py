"""Gemini capture — a drop-in client, plus an opt-in in-place patch.

Two ways to attach, one implementation of what gets captured (``_gemini_base``).

**Drop-in (the default path).** Change the import, nothing else::

    from odyssey.integrations.gemini import Client
    client = Client(api_key=...)                          # same args as the real one
    client.models.generate_content(model="...", contents=...)   # recorded
    await client.aio.models.generate_content(...)               # recorded too

**Patch (opt-in).** For when the call sites cannot be edited::

    odyssey.init(instrument=["gemini"])        # existing clients now record

The drop-in is the default because a patched call stack is harder to read in a
traceback and harder to reason about when two libraries patch the same method.
Patching is the escape hatch, not the recommendation.

Both paths never change what the caller sees: the provider's return value is
passed through untouched, and a provider exception propagates unchanged.
Capture failures are swallowed and counted — see ``odyssey.health()``.

One SDK shape difference from Anthropic/OpenAI worth knowing before extending
this: ``google.genai.Client()`` exposes both the sync (``client.models``) and
async (``client.aio.models``) surfaces off *one* object, not two separate
client classes — so there is one ``Client`` wrapper here, not a ``Client`` /
``AsyncClient`` pair.

``generate_content_stream`` is captured too: chunks are folded back into one
turn once the caller has drained the stream — LiveKit's Google plugin streams.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from odyssey.integrations._call import Capture, capture_async, capture_sync
from odyssey.integrations._gemini_base import (
    PROVIDER,
    ResponseAccumulator,
    capture_request,
    capture_response,
    record_stream,
)
from odyssey.integrations.providers import Provider

# Set by instrument(); cleared by uninstrument(). Module-level because patching
# is a process-wide act and must be reversible exactly once.
_patched: Dict[str, Any] = {}


def _provider(resource: Any) -> Provider:
    """``vertex`` for a Vertex AI client, ``gemini`` for the Developer API."""
    api = getattr(resource, "_api_client", None)
    return Provider("vertex" if getattr(api, "vertexai", False) else PROVIDER)


CAPTURE = Capture(
    label="gemini",
    request=capture_request,
    response=capture_response,
    streamed=record_stream,
    accumulator=ResponseAccumulator,
    provider=_provider,
)


class _ModelsProxy:
    """Wraps ``client.models``, capturing ``generate_content`` and its stream."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def generate_content(self, *args: Any, **kwargs: Any) -> Any:
        return capture_sync(
            CAPTURE,
            self._inner,
            kwargs,
            lambda: self._inner.generate_content(*args, **kwargs),
            streaming=False,
        )

    def generate_content_stream(self, *args: Any, **kwargs: Any) -> Any:
        return capture_sync(
            CAPTURE,
            self._inner,
            kwargs,
            lambda: self._inner.generate_content_stream(*args, **kwargs),
            streaming=True,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _AsyncModelsProxy:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def generate_content(self, *args: Any, **kwargs: Any) -> Any:
        return await capture_async(
            CAPTURE,
            self._inner,
            kwargs,
            lambda: self._inner.generate_content(*args, **kwargs),
            streaming=False,
        )

    async def generate_content_stream(self, *args: Any, **kwargs: Any) -> Any:
        return await capture_async(
            CAPTURE,
            self._inner,
            kwargs,
            lambda: self._inner.generate_content_stream(*args, **kwargs),
            streaming=True,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _AioProxy:
    """Wraps ``client.aio``, the async namespace ``google.genai.Client`` exposes."""

    def __init__(self, inner: Any) -> None:
        self.models = _AsyncModelsProxy(inner.models)
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class Client:
    """Drop-in replacement for ``google.genai.Client``.

    Accepts the same arguments and forwards every attribute it does not wrap,
    so swapping the import is the whole change. The provider is imported here
    rather than at module scope, which is what keeps ``odyssey-core``
    dependency-free.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # pyrefly: ignore[missing-import]  — optional extra, `odyssey[gemini]`.
        # Absent by design in a default install; that is what keeps core's
        # `dependencies = []` true, so the checker cannot resolve it here.
        from google.genai import Client as _Real

        self._inner = _Real(*args, **kwargs)
        self.models = _ModelsProxy(self._inner.models)
        self.aio = _AioProxy(self._inner.aio)

    @property
    def inner(self) -> Any:
        """The wrapped provider client, for anything this proxy does not cover."""
        return self._inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


# ---------------------------------------------------------------------------
# Opt-in patching
# ---------------------------------------------------------------------------


def instrument(target: Optional[Any] = None) -> None:
    """Patch ``google.genai`` in place so existing clients record.

    Patches ``generate_content`` and ``generate_content_stream`` on both
    ``Models`` and ``AsyncModels``, mirroring how ``client.models`` and
    ``client.aio.models`` are one SDK, not two. Idempotent. ``target`` overrides
    the module to patch, which is what makes this testable without the SDK.
    """
    if _patched:
        return
    if target is None:
        import google.genai.models as target  # type: ignore[no-redef]

    sync_cls = getattr(target, "Models", None)
    async_cls = getattr(target, "AsyncModels", None)
    if (
        sync_cls is None
        or not hasattr(sync_cls, "generate_content")
        or async_cls is None
        or not hasattr(async_cls, "generate_content")
    ):
        raise AttributeError(
            "google.genai.models.Models/AsyncModels.generate_content not found; "
            "this google-genai version is not supported by instrument()"
        )

    patches = []
    for cls, is_async in ((sync_cls, False), (async_cls, True)):
        for method, streaming in (
            ("generate_content", False),
            ("generate_content_stream", True),
        ):
            original = getattr(cls, method, None)
            if original is None:
                continue
            patches.append((cls, method, original))
            setattr(
                cls,
                method,
                _patched_method(original, is_async=is_async, streaming=streaming),
            )
    _patched["patches"] = patches


def _patched_method(original: Any, *, is_async: bool, streaming: bool) -> Any:
    if is_async:

        async def patched_async(self: Any, *args: Any, **kwargs: Any) -> Any:
            return await capture_async(
                CAPTURE,
                self,
                kwargs,
                lambda: original(self, *args, **kwargs),
                streaming=streaming,
            )

        patched_async.__wrapped__ = original  # type: ignore[attr-defined]
        return patched_async

    def patched(self: Any, *args: Any, **kwargs: Any) -> Any:
        return capture_sync(
            CAPTURE,
            self,
            kwargs,
            lambda: original(self, *args, **kwargs),
            streaming=streaming,
        )

    patched.__wrapped__ = original  # type: ignore[attr-defined]
    return patched


def uninstrument() -> None:
    """Undo :func:`instrument`. Safe to call when nothing was patched."""
    for cls, method, original in _patched.pop("patches", []):
        setattr(cls, method, original)
    _patched.clear()


def is_instrumented() -> bool:
    return bool(_patched)
