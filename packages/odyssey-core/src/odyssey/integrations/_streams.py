"""Wrappers that let a provider stream be observed without changing it.

The caller iterates, enters and closes exactly what it would have. Each chunk is
handed to ``on_chunk`` as it passes, and ``on_end`` runs once — when the stream
is drained, closed, exited, or fails (a voice reply cancelled by barge-in
included), with the exception when there was one. Everything else is forwarded
to the wrapped stream.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Optional

OnChunk = Callable[[Any], None]
OnEnd = Callable[[Optional[BaseException]], None]

_OWN = frozenset({"_inner", "_on_chunk", "_on_end", "_ended", "_it", "_ait"})


class _Observed:
    def __init__(self, inner: Any, on_chunk: OnChunk, on_end: OnEnd) -> None:
        self._inner = inner
        self._on_chunk = on_chunk
        self._on_end = on_end
        self._ended = False

    @property
    def inner(self) -> Any:
        return self._inner

    def _end(self, exc: Optional[BaseException] = None) -> None:
        if self._ended:
            return
        self._ended = True
        self._on_end(exc)

    def __getattr__(self, name: str) -> Any:
        if name in _OWN:
            raise AttributeError(name)
        return getattr(self._inner, name)


class ObservedStream(_Observed):
    """A sync stream or generator, observed."""

    def __init__(self, inner: Any, on_chunk: OnChunk, on_end: OnEnd) -> None:
        super().__init__(inner, on_chunk, on_end)
        self._it: Any = None

    def __iter__(self) -> "ObservedStream":
        return self

    def __next__(self) -> Any:
        if self._it is None:
            self._it = iter(self._inner)
        try:
            chunk = next(self._it)
        except StopIteration:
            self._end()
            raise
        except BaseException as exc:
            self._end(exc)
            raise
        self._on_chunk(chunk)
        return chunk

    def __enter__(self) -> "ObservedStream":
        enter = getattr(self._inner, "__enter__", None)
        if enter is not None:
            enter()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        self._end(exc)
        exit_ = getattr(self._inner, "__exit__", None)
        return exit_(exc_type, exc, tb) if exit_ is not None else None

    def close(self) -> None:
        self._end()
        close = getattr(self._inner, "close", None)
        if close is not None:
            close()


class ObservedAsyncStream(_Observed):
    """An async stream or async generator, observed."""

    def __init__(self, inner: Any, on_chunk: OnChunk, on_end: OnEnd) -> None:
        super().__init__(inner, on_chunk, on_end)
        self._ait: Any = None

    def __aiter__(self) -> "ObservedAsyncStream":
        return self

    async def __anext__(self) -> Any:
        if self._ait is None:
            self._ait = self._inner.__aiter__()
        try:
            chunk = await self._ait.__anext__()
        except StopAsyncIteration:
            self._end()
            raise
        except BaseException as exc:
            self._end(exc)
            raise
        self._on_chunk(chunk)
        return chunk

    async def __aenter__(self) -> "ObservedAsyncStream":
        enter = getattr(self._inner, "__aenter__", None)
        if enter is not None:
            await enter()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        self._end(exc)
        exit_ = getattr(self._inner, "__aexit__", None)
        return await exit_(exc_type, exc, tb) if exit_ is not None else None

    async def close(self) -> None:
        self._end()
        await _maybe_await(getattr(self._inner, "close", None))

    async def aclose(self) -> None:
        self._end()
        await _maybe_await(getattr(self._inner, "aclose", None))


async def _maybe_await(fn: Optional[Callable[[], Any]]) -> None:
    if fn is None:
        return
    result = fn()
    if inspect.isawaitable(result):
        await result
