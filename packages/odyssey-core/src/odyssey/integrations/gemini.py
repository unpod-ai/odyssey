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

Streaming (``generate_content_stream``) is not wrapped yet — same open item
as Anthropic's/OpenAI's own streaming coverage (``docs/WORKING.md`` 0'.5).
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from odyssey.capture import journey
from odyssey.client import require_client
from odyssey.integrations._gemini_base import capture_request, capture_response

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

    The request is recorded *before* the call so a provider timeout still
    leaves the prompt in the corpus — a journey that shows what was asked and
    then terminates with an error is useful; one that shows nothing is not.
    """
    with journey():
        _safe("gemini.request", lambda: capture_request(kwargs))
        result = call()
        _safe(
            "gemini.response",
            lambda: capture_response(result, model=kwargs.get("model")),
        )
        return result


class _ModelsProxy:
    """Wraps ``client.models``, capturing ``generate_content``."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def generate_content(self, *args: Any, **kwargs: Any) -> Any:
        return _record_call(
            kwargs, lambda: self._inner.generate_content(*args, **kwargs)
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _AsyncModelsProxy:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def generate_content(self, *args: Any, **kwargs: Any) -> Any:
        with journey():
            _safe("gemini.request", lambda: capture_request(kwargs))
            result = await self._inner.generate_content(*args, **kwargs)
            _safe(
                "gemini.response",
                lambda: capture_response(result, model=kwargs.get("model")),
            )
            return result

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

    Idempotent. ``target`` overrides the module to patch, which is what makes
    this testable without the real SDK installed. Patches both the sync
    ``Models.generate_content`` and the async ``AsyncModels.generate_content``
    on the same target module, mirroring how ``client.models``/``client.aio.models``
    are one SDK, not two.
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

    original_sync = sync_cls.generate_content
    original_async = async_cls.generate_content

    def patched_sync(self: Any, *args: Any, **kwargs: Any) -> Any:
        return _record_call(kwargs, lambda: original_sync(self, *args, **kwargs))

    async def patched_async(self: Any, *args: Any, **kwargs: Any) -> Any:
        with journey():
            _safe("gemini.request", lambda: capture_request(kwargs))
            result = await original_async(self, *args, **kwargs)
            _safe(
                "gemini.response",
                lambda: capture_response(result, model=kwargs.get("model")),
            )
            return result

    patched_sync.__wrapped__ = original_sync  # type: ignore[attr-defined]
    patched_async.__wrapped__ = original_async  # type: ignore[attr-defined]
    sync_cls.generate_content = patched_sync
    async_cls.generate_content = patched_async
    _patched["sync_cls"] = sync_cls
    _patched["async_cls"] = async_cls
    _patched["generate_content"] = original_sync
    _patched["generate_content_async"] = original_async


def uninstrument() -> None:
    """Undo :func:`instrument`. Safe to call when nothing was patched."""
    sync_cls = _patched.pop("sync_cls", None)
    async_cls = _patched.pop("async_cls", None)
    original_sync = _patched.pop("generate_content", None)
    original_async = _patched.pop("generate_content_async", None)
    if sync_cls is not None and original_sync is not None:
        sync_cls.generate_content = original_sync
    if async_cls is not None and original_async is not None:
        async_cls.generate_content = original_async


def is_instrumented() -> bool:
    return bool(_patched)
