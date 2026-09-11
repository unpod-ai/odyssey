"""One captured provider call, whatever the SDK.

Every SDK patch does the same things around a call: stay out of calls a
framework integration already records, stay out of the inner of two stacked
wrappers, record the request, run the call, then record the response —
immediately, once a raw response is parsed, or once a stream is drained. Only
reading the SDK's shapes differs, and a :class:`Capture` is where each provider
module plugs that in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from odyssey.integrations._reentry import in_framework_call, outermost
from odyssey.integrations._scope import CallScope, safe
from odyssey.integrations._streams import ObservedAsyncStream, ObservedStream
from odyssey.integrations._timing import Timer
from odyssey.integrations.providers import Provider, resolve

# `with_raw_response.create(...)` on a Stainless-generated SDK (openai,
# anthropic) sets this header and returns a raw response instead of the parsed
# one. See `RawResponse`.
RAW_RESPONSE_HEADER = "X-Stainless-Raw-Response"

_UNSET: Any = object()


@dataclass(frozen=True)
class Capture:
    """How one SDK's calls are read.

    ``request(kwargs, *, key)``; ``response(result, *, model, latency_ms,
    provider, adapt, key)``; ``streamed(acc, *, model, latency_ms, ttft_ms,
    provider, adapt, key, extra_meta)``; ``accumulator()`` returns an object
    with ``add(chunk) -> bool`` (True when the chunk carried output) and a
    ``finished`` flag; ``provider(resource)`` names who served the call.
    """

    label: str
    request: Callable[..., None]
    response: Callable[..., None]
    streamed: Callable[..., None]
    accumulator: Callable[[], Any]
    provider: Callable[[Any], Provider]


def provider_from_base_url(default: str) -> Callable[[Any], Provider]:
    """Name the provider from the resource's client ``base_url``."""

    def find(resource: Any) -> Provider:
        base_url = getattr(getattr(resource, "_client", None), "base_url", None)
        return resolve(base_url) if base_url is not None else Provider(default)

    return find


def is_raw(kwargs: Dict[str, Any]) -> bool:
    headers = kwargs.get("extra_headers")
    return (
        isinstance(headers, dict)
        and str(headers.get(RAW_RESPONSE_HEADER, "")).lower() == "true"
    )


def _client_key(resource: Any) -> str:
    client = getattr(resource, "_client", None) or getattr(
        resource, "_api_client", None
    )
    return "default" if client is None else f"client-{id(client)}"


class _Call:
    __slots__ = (
        "spec",
        "kwargs",
        "provider",
        "key",
        "streaming",
        "scope",
        "timer",
        "acc",
        "_done",
    )

    def __init__(
        self, spec: Capture, resource: Any, kwargs: Dict[str, Any], streaming: bool
    ) -> None:
        self.spec = spec
        self.kwargs = kwargs
        self.provider = spec.provider(resource)
        # One history offset per SDK client inside a shared journey.
        self.key = _client_key(resource)
        self.streaming = streaming
        self.scope = CallScope()
        self.timer = Timer()
        self.acc: Any = None
        self._done = False

    def request(self) -> None:
        self.scope.run(
            f"{self.spec.label}.request",
            lambda: self.spec.request(self.kwargs, key=self.key),
        )
        # Started after the request capture, so the number is the provider's.
        self.timer = Timer()

    def finish(self, result: Any, *, raw_ok: bool = True) -> Any:
        if raw_ok and is_raw(self.kwargs):
            return RawResponse(result, self)
        if self.streaming:
            if hasattr(result, "__aiter__"):
                self.acc = self.spec.accumulator()
                return ObservedAsyncStream(result, self._chunk, self._drained)
            if hasattr(result, "__iter__"):
                self.acc = self.spec.accumulator()
                return ObservedStream(result, self._chunk, self._drained)
        self.respond(result)
        return result

    def respond(self, result: Any) -> None:
        if self._done:
            return
        self._done = True
        elapsed = self.timer.latency_ms
        self.scope.run(
            f"{self.spec.label}.response",
            lambda: self.spec.response(
                result,
                model=self.kwargs.get("model"),
                latency_ms=elapsed,
                provider=self.provider.name,
                adapt=self.provider.adapt,
                key=self.key,
            ),
        )
        self.scope.close()

    def _chunk(self, chunk: Any) -> None:
        acc = self.acc

        def fold() -> None:
            if acc.add(chunk):
                self.timer.first_token()

        safe(f"{self.spec.label}.stream", fold)

    def _drained(self, exc: Optional[BaseException]) -> None:
        if self._done:
            return
        self._done = True
        acc = self.acc
        latency, ttft = self.timer.latency_ms, self.timer.ttft_ms
        meta: Dict[str, Any] = {"streamed": True}
        if not getattr(acc, "finished", False):
            meta["incomplete"] = True
        if exc is not None:
            meta["stream_error"] = type(exc).__name__
        self.scope.run(
            f"{self.spec.label}.response",
            lambda: self.spec.streamed(
                acc,
                model=self.kwargs.get("model"),
                latency_ms=latency,
                ttft_ms=ttft,
                provider=self.provider.name,
                adapt=self.provider.adapt,
                key=self.key,
                extra_meta=meta,
            ),
        )
        self.scope.close(exc)

    def failed(self, exc: BaseException) -> None:
        if self._done:
            return
        self._done = True
        self.scope.close(exc)


def capture_sync(
    spec: Capture,
    resource: Any,
    kwargs: Dict[str, Any],
    call: Callable[[], Any],
    *,
    streaming: Optional[bool] = None,
) -> Any:
    """Record one sync call. ``streaming`` defaults to ``kwargs["stream"]``.

    The request is recorded *before* the call, so a provider timeout still
    leaves the prompt in the corpus.
    """
    if in_framework_call():
        return call()
    with outermost() as mine:
        if not mine:
            # An outer wrapper is already recording this call -- a drop-in
            # client over the in-place patch. See `_reentry`.
            return call()
        rec = _Call(spec, resource, kwargs, _streaming(kwargs, streaming))
        rec.request()
        try:
            result = call()
        except BaseException as exc:
            rec.failed(exc)
            raise
        return rec.finish(result)


async def capture_async(
    spec: Capture,
    resource: Any,
    kwargs: Dict[str, Any],
    call: Callable[[], Any],
    *,
    streaming: Optional[bool] = None,
) -> Any:
    """Record one awaited call; see :func:`capture_sync`."""
    if in_framework_call():
        return await call()
    with outermost() as mine:
        if not mine:
            return await call()
        rec = _Call(spec, resource, kwargs, _streaming(kwargs, streaming))
        rec.request()
        try:
            result = await call()
        except BaseException as exc:
            rec.failed(exc)
            raise
        return rec.finish(result)


def _streaming(kwargs: Dict[str, Any], explicit: Optional[bool]) -> bool:
    return bool(kwargs.get("stream")) if explicit is None else explicit


class RawResponse:
    """A raw response, recording once parsed.

    The raw object carries headers and the HTTP response; the parsed result —
    or the stream — only exists after ``.parse()``, which the SDK caches.
    Recording happens there, once, for whichever the caller got.
    """

    def __init__(self, inner: Any, rec: _Call) -> None:
        self._inner = inner
        self._rec = rec
        self._parsed: Any = _UNSET

    @property
    def inner(self) -> Any:
        return self._inner

    def parse(self, *args: Any, **kwargs: Any) -> Any:
        result = self._inner.parse(*args, **kwargs)
        if args or kwargs:
            # Cast to a caller-chosen type; not a shape this capture reads.
            return result
        if self._parsed is _UNSET:
            self._parsed = self._rec.finish(result, raw_ok=False)
        return self._parsed

    def __getattr__(self, name: str) -> Any:
        if name in ("_inner", "_rec", "_parsed"):
            raise AttributeError(name)
        return getattr(self._inner, name)
