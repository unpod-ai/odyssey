"""OpenAI capture — a drop-in client, plus an opt-in in-place patch.

Two ways to attach, one implementation of what gets captured (``_openai_base``).

**Drop-in (the default path).** Change the import, nothing else::

    from odyssey.integrations.openai import OpenAI
    client = OpenAI()                          # same args as the real one
    client.chat.completions.create(...)        # recorded

**OpenAI-compatible providers work the same way — no extra code.** Groq,
Together, local vLLM/Ollama servers, DeepSeek and others speak the identical
Chat Completions JSON; this wrapper forwards every constructor argument to
the real ``openai.OpenAI``/``openai.AsyncOpenAI``, so pointing it at a
different host is the whole change::

    client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key="...")

**Patch (opt-in).** For when the call sites cannot be edited::

    odyssey.init(instrument=["openai"])        # existing clients now record

The drop-in is the default because a patched call stack is harder to read in a
traceback and harder to reason about when two libraries patch the same method.
Patching is the escape hatch, not the recommendation.

Both paths never change what the caller sees: the provider's return value is
passed through untouched, and a provider exception propagates unchanged.
Capture failures are swallowed and counted — see ``odyssey.health()``.

Streaming is not wrapped yet — ``create(stream=True)`` is passed through
untouched and unrecorded, same open item as Anthropic's async streaming path
(``docs/WORKING.md`` 0'.5).
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from odyssey.capture import journey
from odyssey.client import require_client
from odyssey.integrations._openai_base import capture_request, capture_response

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
        _safe("openai.request", lambda: capture_request(kwargs))
        result = call()
        _safe(
            "openai.response",
            lambda: capture_response(result, model=kwargs.get("model")),
        )
        return result


class _CompletionsProxy:
    """Wraps ``client.chat.completions``, capturing ``create``.

    ``stream=True`` is passed straight through unrecorded rather than
    partially captured — per-chunk events would flood the spool with
    fragments that are not the turn the model produced, the same reasoning
    Anthropic's ``.stream()`` wrapper uses, just not yet built out here.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def create(self, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("stream"):
            return self._inner.create(*args, **kwargs)
        return _record_call(kwargs, lambda: self._inner.create(*args, **kwargs))

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
        # Absent by design in a default install; that is what keeps core's
        # `dependencies = []` true, so the checker cannot resolve it here.
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
        if kwargs.get("stream"):
            return await self._inner.create(*args, **kwargs)
        with journey():
            _safe("openai.request", lambda: capture_request(kwargs))
            result = await self._inner.create(*args, **kwargs)
            _safe(
                "openai.response",
                lambda: capture_response(result, model=kwargs.get("model")),
            )
            return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


# ---------------------------------------------------------------------------
# Opt-in patching
# ---------------------------------------------------------------------------


def instrument(target: Optional[Any] = None) -> None:
    """Patch ``openai`` in place so existing clients record.

    Idempotent. ``target`` overrides the module to patch, which is what makes
    this testable without the real SDK installed.
    """
    if _patched:
        return
    if target is None:
        import openai.resources.chat.completions as target  # type: ignore[no-redef]

    cls = getattr(target, "Completions", None)
    if cls is None or not hasattr(cls, "create"):
        raise AttributeError(
            "openai.resources.chat.completions.Completions.create not found; "
            "this openai version is not supported by instrument()"
        )

    original = cls.create

    def patched(self: Any, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("stream"):
            return original(self, *args, **kwargs)
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
