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

from typing import Any, Callable, Dict, Optional

from odyssey.capture import JourneyHandle, _jsonable
from odyssey.client import require_client
from odyssey.context import JourneyContext, SeqAllocator, bind
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
        client = require_client()
        ctx = JourneyContext(
            journey_id=root,
            allocator=(
                client.allocator if client is not None else _throwaway_allocator()
            ),
            metadata=_jsonable(dict(self._metadata)),
            data_source=self._data_source,
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
        if ctx is None or ctx.terminated:
            return
        with bind(ctx):
            JourneyHandle(ctx).close(reason=reason, error=error)

    def _is_root(self, run_id: Any) -> bool:
        rid = _rid(run_id)
        return self._roots.get(rid) == rid

    # -- LLM ----------------------------------------------------------

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
                            Message(role="assistant", content=str(text))
                        )
            if self._is_root(run_id):
                self._end(root)

        self._guard("llm_end", go)

    def on_llm_error(
        self, error: BaseException, *, run_id: Any, parent_run_id: Any = None, **_: Any
    ) -> None:
        root = self._root_for(run_id, parent_run_id)
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
        def on_llm_start(self, *args: Any, **kwargs: Any) -> None:
            recorder.on_llm_start(*args, **kwargs)

        def on_chat_model_start(self, *args: Any, **kwargs: Any) -> None:
            recorder.on_chat_model_start(*args, **kwargs)

        def on_llm_end(self, *args: Any, **kwargs: Any) -> None:
            recorder.on_llm_end(*args, **kwargs)

        def on_llm_error(self, *args: Any, **kwargs: Any) -> None:
            recorder.on_llm_error(*args, **kwargs)

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
