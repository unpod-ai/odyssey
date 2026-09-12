"""LangChain (and LangGraph) callback handler — items 0.10 / 0'.2.

::

    from odyssey.integrations.langchain import OdysseyCallbackHandler
    chain.invoke({"input": "book Tuesday at 3"}, config={"callbacks": [OdysseyCallbackHandler()]})

    # LangGraph too, no extra code -- `StateGraph(...).compile()` is itself a
    # Runnable and dispatches through the identical callback interface:
    graph.invoke({"x": 0}, config={"callbacks": [OdysseyCallbackHandler()]})

**LangGraph needs nothing beyond what is already here.** A compiled graph's
own `invoke()`/`ainvoke()` is a top-level chain run, and every node
(including a `langgraph.prebuilt.ToolNode`) is its own nested chain/tool run
parented to it via the same `run_id`/`parent_run_id` LangChain already uses
— that tree collapses into one journey exactly like a plain LangChain chain
wrapping an LLM and a tool does below. A node function calling
`llm.invoke(...)` without explicitly forwarding `config` still lands under
the graph's journey, because LangChain propagates callbacks via contextvars.
Verified against real `langgraph`/`langchain-core` installs (not guessed);
`tests/test_langchain_integration.py`'s "LangGraph compatibility" section
replays the exact run trees observed from that verification without
requiring either package to be installed for the suite to run.

LangChain's callback interface is shaped differently from the Anthropic/
OpenAI drop-in clients (``_base.py``'s request/response pair for a single
wrapped call): every event carries a ``run_id``/``parent_run_id``, because
one invocation can fan out into a tree of chain/LLM/tool spans. That shape
is much closer to ``integrations/livekit.py``'s event-subscriber pattern
than to a wrapped client, so this module follows livekit's approach —
holding an explicit :class:`~odyssey.context.JourneyContext` per journey
and entering it with :func:`odyssey.context.bind` around each recorded
call, rather than the ambient ``with journey():`` block.

One flat journey per **top-level** run (a run with no tracked parent) —
nested chain/agent graph structure is not modeled as separate journeys or
sub-spans; every LLM/tool call anywhere under one top-level run lands as
more turns in that one journey. This is an explicit scope cut: LangChain's
own run tree is a call graph, and this project's corpus is turn-shaped, not
span-shaped.

Requires ``langchain-core`` (an optional extra: ``odyssey[langchain]``),
imported lazily inside :func:`OdysseyCallbackHandler` — never at module
scope, which is what keeps ``odyssey-core``'s ``dependencies = []`` true
for every caller who does not use this integration. Because the handler
must subclass ``langchain_core.callbacks.BaseCallbackHandler`` (LangChain's
own dispatch expects a real subclass, not just a duck-typed object), the
class itself is defined inside the same lazy-import scope — this is why
:func:`OdysseyCallbackHandler` is a factory function rather than a class:
calling it returns an instance of a class that could not exist until the
optional dependency was actually imported.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Set

from odyssey.capture import JourneyHandle, _jsonable
from odyssey.client import require_client
from odyssey.context import JourneyContext, SeqAllocator, bind, current
from odyssey.integrations._linked import linked_journey
from odyssey.integrations._reentry import enter_framework_call, exit_framework_call
from odyssey.integrations._timing import stamp
from odyssey.primitives import Message, Role, TerminationReason, ToolCall, ToolResponse

__all__ = ["OdysseyCallbackHandler"]

_ROLE_BY_TYPE: Dict[str, Role] = {
    "human": "user",
    "ai": "assistant",
    "system": "system",
    "tool": "tool",
    "function": "tool",
}


def _throwaway_allocator() -> SeqAllocator:
    return SeqAllocator(lambda _jid: None)


def _rid(run_id: Any) -> str:
    return str(run_id)


class _Recorder:
    """The capture logic, kept independent of ``BaseCallbackHandler`` so it
    is unit-testable without a real (or even a fake) langchain-core class —
    only :func:`OdysseyCallbackHandler` needs the optional dependency."""

    def __init__(self, *, data_source: str, metadata: Optional[Dict[str, Any]]) -> None:
        self._data_source = data_source
        self._metadata = metadata or {}
        self._journeys: Dict[str, JourneyContext] = {}
        # run_id -> the top-level run_id it belongs to.
        self._roots: Dict[str, str] = {}
        # Roots whose context was borrowed from an ambient journey rather than
        # opened here. Tracked so `_end` never terminates someone else's
        # journey -- see `_ctx_for`.
        self._borrowed: Set[str] = set()
        # run_id -> what the provider patch underneath measured for that model
        # run. Written by `note_provider_call`, read once by `on_llm_end`.
        self._measured: Dict[str, Dict[str, Any]] = {}

    def _guard(self, label: str, fn: Callable[[], Any]) -> None:
        """Run a capture step from inside a LangChain callback. Never raises
        — an exception here must not break the chain it is observing. `fn`'s
        return value is always discarded, `Any` rather than `None` only so a
        call site can hand this a lambda that happens to return something
        (`self._ctx_for(root)`) without a needless `; return None`."""
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - capture is best-effort
            client = require_client()
            if client is not None:
                client.note_error(f"langchain.{label}", exc)

    def _root_for(self, run_id: Any, parent_run_id: Any) -> str:
        rid = _rid(run_id)
        if parent_run_id is not None and _rid(parent_run_id) in self._roots:
            root = self._roots[_rid(parent_run_id)]
        else:
            root = rid
        self._roots[rid] = root
        return root

    def _ctx_for(self, root: str) -> JourneyContext:
        ctx = self._journeys.get(root)
        if ctx is not None:
            return ctx

        # An ambient journey wins over this run's own id. A graph invoked
        # during a voice call is part of that call, and keying it on
        # LangChain's run id instead produced a second journey under a uuid
        # that appears nowhere else -- two halves of one conversation with
        # nothing to associate them by. Only when nothing else is recording
        # does the run id become the journey, which is the standalone case
        # this integration was written for and is unchanged.
        ambient = current()
        if ambient is None or ambient.terminated:
            # A graph run inside an attached voice call: the call's `.llm`
            # journey, where the rest of its provider calls go.
            ambient = linked_journey()
        if ambient is not None:
            self._journeys[root] = ambient
            self._borrowed.add(root)
            return ambient

        client = require_client()
        ctx = JourneyContext(
            journey_id=root,
            allocator=(
                client.allocator if client is not None else _throwaway_allocator()
            ),
            metadata=_jsonable(dict(self._metadata)),
            data_source=self._data_source,
            # What recorded this shard, as distinct from where the
            # conversation came from (`data_source`).
            framework="langchain",
        )
        self._journeys[root] = ctx
        if client is not None:
            client.count_journey()
        return ctx

    def _handle(self, root: str) -> JourneyHandle:
        return JourneyHandle(self._ctx_for(root))

    def _end(
        self,
        root: str,
        *,
        reason: TerminationReason = "ENV_DONE",
        error: Optional[str] = None,
    ) -> None:
        ctx = self._journeys.pop(root, None)
        self._roots = {k: v for k, v in self._roots.items() if v != root}
        # A borrowed context belongs to whoever bound it, and they close it.
        # Terminating it here would end a voice call at its first graph node
        # and strand every turn after it.
        borrowed = root in self._borrowed
        self._borrowed.discard(root)
        if ctx is None or ctx.terminated or borrowed:
            return
        with bind(ctx):
            JourneyHandle(ctx).close(reason=reason, error=error)

    def _is_root(self, run_id: Any) -> bool:
        rid = _rid(run_id)
        return self._roots.get(rid) == rid

    # -- LLM ----------------------------------------------------------

    def note_provider_call(
        self,
        run_id: str,
        *,
        provider: Optional[str] = None,
        latency_ms: Optional[float] = None,
        ttft_ms: Optional[float] = None,
    ) -> None:
        """Timing and provenance for a model run, from the SDK patch below.

        The handler owns the turn -- the patch stays out of a call LangChain is
        already recording -- but only the patch sees which host served it and
        how long it took, so it hands both here and `on_llm_end` stamps them
        onto the turn it writes.

        Last write wins, per field: a run that retried has the answer of its
        final attempt, and a provider that failed over answered from somewhere
        else than the attempt before it.
        """
        measured = self._measured.setdefault(run_id, {})
        for name, value in (
            ("provider", provider),
            ("latency_ms", latency_ms),
            ("ttft_ms", ttft_ms),
        ):
            if value is not None:
                measured[name] = value

    def on_llm_start(
        self,
        serialized: Any,
        prompts: Any,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **_: Any,
    ) -> None:
        root = self._root_for(run_id, parent_run_id)

        def go() -> None:
            with bind(self._ctx_for(root)):
                for prompt in prompts:
                    self._handle(root).message(
                        Message(role="user", content=str(prompt))
                    )

        self._guard("llm_start", go)

    def on_chat_model_start(
        self,
        serialized: Any,
        messages: Any,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **_: Any,
    ) -> None:
        root = self._root_for(run_id, parent_run_id)

        def go() -> None:
            with bind(self._ctx_for(root)):
                for batch in messages:
                    for m in batch:
                        role = _ROLE_BY_TYPE.get(getattr(m, "type", ""), "user")
                        self._handle(root).message(
                            Message(role=role, content=str(getattr(m, "content", "")))
                        )

        self._guard("chat_model_start", go)

    def on_llm_end(
        self, response: Any, *, run_id: Any, parent_run_id: Any = None, **_: Any
    ) -> None:
        root = self._root_for(run_id, parent_run_id)
        # Popped outside `go` so the run leaves nothing behind even if
        # recording it fails.
        measured = self._measured.pop(_rid(run_id), {})

        def go() -> None:
            with bind(self._ctx_for(root)):
                for batch in getattr(response, "generations", None) or []:
                    for gen in batch:
                        message = getattr(gen, "message", None)
                        text = (
                            getattr(message, "content", "")
                            if message is not None
                            else getattr(gen, "text", "")
                        )
                        self._handle(root).message(
                            stamp(
                                Message(role="assistant", content=str(text)), **measured
                            )
                        )
            if self._is_root(run_id):
                self._end(root)

        self._guard("llm_end", go)

    def on_llm_error(
        self, error: BaseException, *, run_id: Any, parent_run_id: Any = None, **_: Any
    ) -> None:
        root = self._root_for(run_id, parent_run_id)
        self._measured.pop(_rid(run_id), None)
        if self._is_root(run_id):
            self._end(root, reason="ERROR", error=f"{type(error).__name__}: {error}")

    # -- Tools ----------------------------------------------------------

    def on_tool_start(
        self,
        serialized: Any,
        input_str: Any,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **_: Any,
    ) -> None:
        root = self._root_for(run_id, parent_run_id)
        rid = _rid(run_id)
        name = (
            (serialized or {}).get("name", "tool")
            if isinstance(serialized, dict)
            else "tool"
        )

        def go() -> None:
            with bind(self._ctx_for(root)):
                self._handle(root).message(
                    Message(
                        role="assistant",
                        tool_calls=[
                            ToolCall(
                                id=rid, name=name, arguments={"input": str(input_str)}
                            )
                        ],
                    )
                )

        self._guard("tool_start", go)

    def on_tool_end(
        self, output: Any, *, run_id: Any, parent_run_id: Any = None, **_: Any
    ) -> None:
        root = self._root_for(run_id, parent_run_id)
        rid = _rid(run_id)

        def go() -> None:
            with bind(self._ctx_for(root)):
                self._handle(root).message(
                    Message(
                        role="tool",
                        tool_response=ToolResponse(
                            id=rid, name="tool", arguments={}, response=str(output)
                        ),
                    )
                )

        self._guard("tool_end", go)

    # -- Chains -----------------------------------------------------------
    #
    # A chain wraps LLM/tool calls but carries no turn of its own to record
    # -- only root-tracking and lifecycle (open on start, close on end/error).

    def on_chain_start(
        self,
        serialized: Any,
        inputs: Any,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **_: Any,
    ) -> None:
        root = self._root_for(run_id, parent_run_id)
        # Eagerly open the journey (not lazily on first message) so a chain
        # that errors before recording anything still gets a diagnosable
        # terminal event, the same way `with odyssey.journey():` always
        # opens a real context whether or not the block records anything.
        self._guard("chain_start", lambda: self._ctx_for(root))

    def on_chain_end(
        self, outputs: Any, *, run_id: Any, parent_run_id: Any = None, **_: Any
    ) -> None:
        root = self._root_for(run_id, parent_run_id)
        if self._is_root(run_id):
            self._end(root)

    def on_chain_error(
        self, error: BaseException, *, run_id: Any, parent_run_id: Any = None, **_: Any
    ) -> None:
        root = self._root_for(run_id, parent_run_id)
        if self._is_root(run_id):
            self._end(root, reason="ERROR", error=f"{type(error).__name__}: {error}")


def OdysseyCallbackHandler(
    *, data_source: str = "langchain", metadata: Optional[Dict[str, Any]] = None
) -> Any:
    """Build a ``langchain_core.callbacks.BaseCallbackHandler`` that records
    every LLM/tool call under one journey per top-level run.

    A factory, not a class — see the module docstring for why ``langchain_core``
    can only be imported here, inside this call, rather than at module scope.
    """
    # pyrefly: ignore[missing-import]  — optional extra, `odyssey[langchain]`.
    from langchain_core.callbacks import BaseCallbackHandler

    recorder = _Recorder(data_source=data_source, metadata=metadata)

    class _Handler(BaseCallbackHandler):
        # Called in the caller's own context rather than handed to an executor.
        # That is what lets a model run's start mark the call for the provider
        # patch underneath it (`_reentry.enter_framework_call`), so `ChatOpenAI`
        # is recorded once, here, and not again by the `openai` patch. A handler
        # run in an executor would set the mark in a copy nobody reads.
        run_inline = True

        def __init__(self) -> None:
            super().__init__()
            self._marks: Dict[str, Any] = {}

        def is_running(self, run_id: str) -> bool:
            return run_id in self._marks

        def _mark(self, kwargs: Dict[str, Any]) -> None:
            rid = _rid(kwargs.get("run_id"))
            if rid in self._marks:
                return
            try:
                self._marks[rid] = enter_framework_call(self, rid)
            except Exception:  # noqa: BLE001 - never break the chain over a mark
                pass

        def observed(self, run_id: str, **measured: Any) -> None:
            """What the provider patch measured for one of this handler's runs.

            Only while the run is still marked: a report arriving after the run
            ended belongs to no turn this handler will write, and keeping it
            would be a dict entry nothing ever pops.
            """
            if self.is_running(run_id):
                recorder.note_provider_call(run_id, **measured)

        def _unmark(self, kwargs: Dict[str, Any]) -> None:
            token = self._marks.pop(_rid(kwargs.get("run_id")), None)
            if token is not None:
                exit_framework_call(token)

        def on_llm_start(self, *args: Any, **kwargs: Any) -> None:
            self._mark(kwargs)
            recorder.on_llm_start(*args, **kwargs)

        def on_chat_model_start(self, *args: Any, **kwargs: Any) -> None:
            self._mark(kwargs)
            recorder.on_chat_model_start(*args, **kwargs)

        def on_llm_end(self, *args: Any, **kwargs: Any) -> None:
            try:
                recorder.on_llm_end(*args, **kwargs)
            finally:
                self._unmark(kwargs)

        def on_llm_error(self, *args: Any, **kwargs: Any) -> None:
            try:
                recorder.on_llm_error(*args, **kwargs)
            finally:
                self._unmark(kwargs)

        def on_tool_start(self, *args: Any, **kwargs: Any) -> None:
            recorder.on_tool_start(*args, **kwargs)

        def on_tool_end(self, *args: Any, **kwargs: Any) -> None:
            recorder.on_tool_end(*args, **kwargs)

        def on_chain_start(self, *args: Any, **kwargs: Any) -> None:
            recorder.on_chain_start(*args, **kwargs)

        def on_chain_end(self, *args: Any, **kwargs: Any) -> None:
            recorder.on_chain_end(*args, **kwargs)

        def on_chain_error(self, *args: Any, **kwargs: Any) -> None:
            recorder.on_chain_error(*args, **kwargs)

    return _Handler()


# ---------------------------------------------------------------------------
# Process-wide attachment
# ---------------------------------------------------------------------------

# The registered hook, kept so `uninstrument()` can clear it. Module-level
# because registering is a process-wide act.
_HOOK: Any = None


def instrument(
    *, data_source: str = "langchain", metadata: Optional[Dict[str, Any]] = None
) -> None:
    """Attach the handler to every LangChain run in this process.

    ``data_source`` and ``metadata`` are the same tags
    :func:`OdysseyCallbackHandler` takes, and they matter more here than on
    the per-call form: a process-wide attachment is the one capture path whose
    journeys nobody chose individually, so without them a shard arrives
    carrying nothing that says which service produced it.

    The alternative is the per-call form — ``config={"callbacks": [...]}`` on
    every ``invoke()`` — which is one edit per call site and silently records
    nothing at the call site somebody forgot. This is the difference between
    "odyssey is installed" and "odyssey is installed everywhere it matters",
    and it is what makes ``odyssey.init()`` the single integration point for a
    LangChain app rather than the first of many.

    Uses ``langchain_core.tracers.context.register_configure_hook``, the same
    mechanism LangSmith and the other tracing integrations attach through: the
    handler is held in a ``ContextVar`` that LangChain's own ``_configure``
    reads when it assembles the callback list for a run, so a run started
    anywhere — including inside a nested chain or a LangGraph node that never
    forwards ``config`` — is covered.

    One handler serves the whole process. That is safe because
    :class:`_Recorder` keys every journey on the run tree's root id rather than
    on instance state, so concurrent runs never see each other's turns.

    Idempotent. Requires ``langchain-core``; a failure to attach is the
    caller's to see through :func:`odyssey.health`, not an exception — an app
    that cannot be traced must still run.
    """
    global _HOOK
    if _HOOK is not None:
        return
    from contextvars import ContextVar

    # pyrefly: ignore[missing-import]  — optional extra, `odyssey[langchain]`.
    from langchain_core.tracers.context import register_configure_hook

    handler = OdysseyCallbackHandler(data_source=data_source, metadata=metadata)
    var: ContextVar = ContextVar("odyssey_langchain_handler", default=None)
    # `inheritable=True`: a run started in a child context — a thread from
    # LangChain's own executor, an asyncio task — inherits the handler. Without
    # it, exactly the fan-out cases that most need tracing would be the ones
    # missing it.
    register_configure_hook(var, True)
    var.set(handler)
    _HOOK = (var, handler)


def uninstrument() -> None:
    """Detach the process-wide handler. Safe to call when nothing was attached.

    The hook itself stays registered — ``register_configure_hook`` appends to a
    module-level list LangChain owns and offers no removal — but the
    ``ContextVar`` it reads is cleared, so it contributes no handler.
    """
    global _HOOK
    if _HOOK is None:
        return
    var, _handler = _HOOK
    _HOOK = None
    try:
        var.set(None)
    except Exception:  # noqa: BLE001 - detaching must always succeed
        pass


def is_instrumented() -> bool:
    return _HOOK is not None
