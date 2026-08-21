"""The recording surface: ``journey()``, ``observe()``, and the emit path.

Everything here is on the application's hot path, so two rules hold absolutely:

**Never raise.** A failure to record is counted and surfaced through
:func:`odyssey.health`, never propagated. The one exception is ``ODYSSEY_DEBUG=1``,
which re-raises so a developer sees the fault while developing.

**Never block on the network.** Emitting appends to the local spool and returns.
Shipping is the drainer's job.

Why ``observe()`` records nothing by default
--------------------------------------------

Langfuse records every span because it is an observability product. odyssey's
dump is a *training corpus*, and an arbitrary internal function call is not
something a model should learn from — it is noise that later has to be filtered
out of every recipe. So ``@observe()`` establishes journey context and nothing
else; ``@observe(as_tool=True)`` records the call as a tool turn, which *is*
training-relevant because tool use is behaviour we want the model to reproduce.
"""

from __future__ import annotations

import functools
import inspect
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Optional, TypeVar
from uuid import uuid4

from odyssey.client import Client, require_client
from odyssey.context import (
    WRITER_META_KEY,
    JourneyContext,
    SeqAllocator,
    current,
    reset_current,
    set_current,
)
from odyssey.primitives import (
    EventKind,
    JourneyEvent,
    Message,
    Reward,
    Signal,
    SignalKind,
    Terminal,
    TerminationReason,
    ToolResponse,
)

F = TypeVar("F", bound=Callable[..., Any])

# Depth guard for _jsonable: provider payloads and user return values can be
# arbitrarily nested or self-referential, and the encoder must not recurse
# forever on the recording path.
_MAX_JSON_DEPTH = 12


def _null_seed(_journey_id: str) -> Optional[int]:
    return None


# Used when recording is off, so journey() stays a total function instead of
# branching on whether a client exists. Nothing reaches the spool anyway.
_NULL_ALLOCATOR = SeqAllocator(_null_seed)


def _jsonable(value: Any, depth: int = 0) -> Any:
    """Coerce anything into something ``json.dumps`` accepts.

    Auto-capture sees whatever the application happens to return. A single
    non-serializable object would otherwise fail the encode and lose the event,
    so unknown types degrade to ``repr()`` rather than to nothing.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if depth >= _MAX_JSON_DEPTH:
        return repr(value)[:500]
    if isinstance(value, dict):
        return {str(k): _jsonable(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v, depth + 1) for v in value]
    for attr in ("model_dump", "dict", "to_dict"):
        fn = getattr(value, attr, None)
        if callable(fn):
            try:
                return _jsonable(fn(), depth + 1)
            except Exception:  # noqa: BLE001 - fall through to repr
                break
    return repr(value)[:500]


# ---------------------------------------------------------------------------
# Emit
# ---------------------------------------------------------------------------


def _emit(
    kind: EventKind,
    *,
    message: Optional[Message] = None,
    signal: Optional[Signal] = None,
    reward: Optional[Reward] = None,
    terminal: Optional[Terminal] = None,
    model_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    """Append one event to the ambient journey. Returns its ``seq``, or ``None``.

    ``None`` means nothing was recorded — uninitialised, disabled, no active
    journey, or a swallowed failure. Every one of those increments a counter.
    """
    client = require_client()
    if client is None or not client.config.enabled:
        return None
    ctx = current()
    if ctx is None:
        # Deliberately not auto-creating a journey: a one-event journey with no
        # terminal is never `complete`, so it would be untrainable noise in the
        # spool. Callers that want an implicit journey use `journey()`, which
        # joins an existing one or opens a real one.
        client.count_dropped()
        return None
    try:
        meta: Dict[str, Any] = {WRITER_META_KEY: client.writer_id}
        if ctx.metadata:
            meta.update(_jsonable(ctx.metadata))
        if metadata:
            meta.update(_jsonable(metadata))

        seq = ctx.next_seq()
        client.spool.record(
            JourneyEvent(
                journey_id=ctx.journey_id,
                seq=seq,
                kind=kind,
                message=message,
                signal=signal,
                reward=reward,
                terminal=terminal,
                model_id=model_id,
                metadata=meta,
            )
        )
        if kind == "message":
            ctx.last_message_seq = seq
        client.count_recorded()
        return seq
    except Exception as exc:  # noqa: BLE001 - recording never breaks the caller
        client.count_dropped()
        client.note_error(f"emit:{kind}", exc)
        return None


# ---------------------------------------------------------------------------
# The journey handle
# ---------------------------------------------------------------------------


class JourneyHandle:
    """What ``with odyssey.journey(...)`` hands back.

    Thin by design: it forwards to the emit path and holds no state the context
    does not already hold, so a handle captured in a closure stays correct.
    """

    def __init__(self, ctx: JourneyContext) -> None:
        self._ctx = ctx

    @property
    def id(self) -> str:
        return self._ctx.journey_id

    @property
    def context(self) -> JourneyContext:
        return self._ctx

    def message(
        self,
        message: Message,
        *,
        model_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[int]:
        """Record one message turn."""
        return _emit("message", message=message, model_id=model_id, metadata=metadata)

    def signal(
        self,
        signal: SignalKind,
        *,
        target_seq: Optional[int] = None,
        regen_order: Optional[int] = None,
        edited_output: Optional[str] = None,
    ) -> Optional[int]:
        """Attach human feedback to a turn — the raw material for DPO.

        ``target_seq`` defaults to the most recent message, which is what a
        thumbs-up in a UI almost always means.
        """
        target = target_seq if target_seq is not None else self._ctx.last_message_seq
        if target is None:
            client = require_client()
            if client is not None:
                client.count_dropped()
                client.note_error(
                    "signal",
                    ValueError(
                        f"signal {signal!r} has no target: no message recorded "
                        f"yet in journey {self._ctx.journey_id!r}"
                    ),
                )
            return None
        return _emit(
            "signal",
            signal=Signal(
                signal=signal,
                target_seq=target,
                regen_order=regen_order,
                edited_output=edited_output,
            ),
        )

    def reward(self, reward: Reward | float) -> Optional[int]:
        """Attach a scalar or structured reward. The last one folded wins."""
        if isinstance(reward, (int, float)):
            from odyssey.builders.reward import build_reward_from_scalar

            reward = build_reward_from_scalar(float(reward))
        return _emit("reward", reward=reward)

    def close(
        self,
        *,
        reason: TerminationReason = "ENV_DONE",
        error: Optional[str] = None,
    ) -> Optional[int]:
        """Emit the terminal event. Idempotent.

        Until this lands, ``fold()`` reports the journey as incomplete — it
        cannot tell "still running" from "lost the tail".
        """
        if self._ctx.terminated:
            return None
        self._ctx.terminated = True
        seq = _emit(
            "terminal", terminal=Terminal(termination_reason=reason, error=error)
        )
        client = require_client()
        if client is not None:
            # Release the shard handle and the seq entry: this journey is done,
            # and a long-lived process should not accumulate either.
            client.spool.close(self._ctx.journey_id)
            client.allocator.forget(self._ctx.journey_id)
        return seq


@contextmanager
def journey(
    id: Optional[str] = None,
    *,
    terminal: bool = True,
    **metadata: Any,
) -> Iterator[JourneyHandle]:
    """Scope a journey. Everything recorded inside belongs to it.

    ::

        with odyssey.journey(id=platform_call_id, user_id="u_42") as j:
            ...
            j.signal("thumbs_up")

    Nesting joins the parent rather than opening a second journey, so a decorated
    helper called from inside a block does not split the conversation. Only the
    outermost block emits the terminal event.

    ``id`` is the caller's to choose because journey boundaries are domain
    knowledge — one phone call, one session, one task. Passing the platform's own
    call id also makes recording idempotent across a restart. Omit it and a uuid
    is generated.

    An exception escaping the block closes the journey with
    ``termination_reason="ERROR"`` and re-raises unchanged.
    """
    existing = current()
    joining = existing is not None and (id is None or id == existing.journey_id)

    if joining and existing is not None:
        existing.depth += 1
        if metadata:
            existing.metadata.update(metadata)
        try:
            yield JourneyHandle(existing)
        finally:
            existing.depth -= 1
        return

    client = require_client()
    ctx = JourneyContext(
        journey_id=id or uuid4().hex,
        allocator=(
            client.allocator
            if client is not None
            # Disabled/uninitialised: a throwaway allocator keeps the API total,
            # and _emit() drops before it ever reaches the spool.
            else _NULL_ALLOCATOR
        ),
        metadata=dict(metadata),
    )
    if client is not None:
        client.count_journey()

    handle = JourneyHandle(ctx)
    token = set_current(ctx)
    try:
        yield handle
    except GeneratorExit:
        # The scope was abandoned rather than failed — a caller kept the handle
        # but let the context manager be garbage-collected, so CPython threw this
        # into the suspended generator. Recording it as ERROR would put a fake
        # application failure (with an empty message) into the corpus. STALE is
        # what it actually is.
        if terminal:
            handle.close(
                reason="STALE",
                error="journey scope was abandoned without exiting its "
                "`with` block; use `with odyssey.journey(...)`",
            )
        raise
    except BaseException as exc:
        if terminal:
            handle.close(reason="ERROR", error=f"{type(exc).__name__}: {exc}")
        raise
    else:
        if terminal:
            handle.close()
    finally:
        reset_current(token)


# ---------------------------------------------------------------------------
# Convenience wrappers over the ambient journey
# ---------------------------------------------------------------------------


def signal(signal: SignalKind, **kw: Any) -> Optional[int]:
    """Signal on the ambient journey. No-op outside one."""
    ctx = current()
    return None if ctx is None else JourneyHandle(ctx).signal(signal, **kw)


def reward(value: Reward | float) -> Optional[int]:
    ctx = current()
    return None if ctx is None else JourneyHandle(ctx).reward(value)


def message(message: Message, **kw: Any) -> Optional[int]:
    ctx = current()
    return None if ctx is None else JourneyHandle(ctx).message(message, **kw)


# ---------------------------------------------------------------------------
# @observe
# ---------------------------------------------------------------------------


def observe(
    *,
    name: Optional[str] = None,
    as_tool: bool = False,
    journey_id: Optional[str] = None,
) -> Callable[[F], F]:
    """Establish journey context around a function.

    ``@observe()`` records **no event** — it opens or joins a journey so that
    anything recording inside (a wrapped provider client, an explicit
    ``signal()``) lands in the right place. See the module docstring for why a
    generic span is not written to a training corpus.

    ``@observe(as_tool=True)`` additionally records the call as a tool turn:
    arguments in, return value or error out. Use it for functions the model
    invokes as tools, because that behaviour *is* worth training on.

    Works on sync and async functions alike.
    """

    def decorate(fn: F) -> F:
        label = name or getattr(fn, "__name__", "observed")
        signature = _safe_signature(fn)

        def record_tool(
            bound: Dict[str, Any],
            result: Any,
            error: Optional[str],
            started: float,
        ) -> None:
            _emit(
                "message",
                message=Message(
                    role="tool",
                    tool_response=ToolResponse(
                        id=f"obs_{uuid4().hex[:8]}",
                        name=label,
                        arguments=_jsonable(bound),
                        response=None if error else _jsonable(result),
                        error=error,
                        metadata={
                            "duration_ms": round(
                                (time.perf_counter() - started) * 1000, 3
                            )
                        },
                    ),
                ),
            )

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                with journey(journey_id, terminal=journey_id is not None):
                    bound = _bind(signature, args, kwargs) if as_tool else {}
                    started = time.perf_counter()
                    try:
                        result = await fn(*args, **kwargs)
                    except BaseException as exc:
                        if as_tool:
                            record_tool(
                                bound, None, f"{type(exc).__name__}: {exc}", started
                            )
                        raise
                    if as_tool:
                        record_tool(bound, result, None, started)
                    return result

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            with journey(journey_id, terminal=journey_id is not None):
                bound = _bind(signature, args, kwargs) if as_tool else {}
                started = time.perf_counter()
                try:
                    result = fn(*args, **kwargs)
                except BaseException as exc:
                    if as_tool:
                        record_tool(
                            bound, None, f"{type(exc).__name__}: {exc}", started
                        )
                    raise
                if as_tool:
                    record_tool(bound, result, None, started)
                return result

        return sync_wrapper  # type: ignore[return-value]

    return decorate


def _safe_signature(fn: Callable[..., Any]) -> Optional[inspect.Signature]:
    try:
        return inspect.signature(fn)
    except (TypeError, ValueError):
        return None


def _bind(
    signature: Optional[inspect.Signature],
    args: Any,
    kwargs: Dict[str, Any],
) -> Dict[str, Any]:
    """Named arguments for a call, best-effort. Never raises."""
    if signature is None:
        return {"args": _jsonable(list(args)), "kwargs": _jsonable(kwargs)}
    try:
        bound = signature.bind_partial(*args, **kwargs)
        return {
            k: _jsonable(v)
            for k, v in bound.arguments.items()
            if k not in ("self", "cls")
        }
    except TypeError:
        return {"args": _jsonable(list(args)), "kwargs": _jsonable(kwargs)}


def dropped_because_no_journey(client: Client) -> int:
    """Exposed for diagnostics: events discarded for having nowhere to go."""
    return client.stats.events_dropped


__all__: List[str] = [
    "JourneyHandle",
    "journey",
    "observe",
    "signal",
    "reward",
    "message",
]
