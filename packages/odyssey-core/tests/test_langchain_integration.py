"""LangChain callback handler (item 0.10).

No real ``langchain-core`` install: a fake module is injected into
``sys.modules``, same technique ``test_integrations.py`` uses for the
Anthropic SDK — proves the handler never imports the real package until
``OdysseyCallbackHandler()`` is actually called, and keeps this file
runnable without the optional dependency installed.
"""

from __future__ import annotations

import sys
import types
import uuid

import pytest

import odyssey


class FakeMessage:
    def __init__(self, type_, content):
        self.type = type_
        self.content = content


class FakeGeneration:
    def __init__(self, *, text=None, message=None):
        self.text = text
        self.message = message


class FakeLLMResult:
    def __init__(self, generations):
        self.generations = generations


@pytest.fixture(autouse=True)
def fake_sdk(monkeypatch):
    """Install a fake ``langchain_core.callbacks`` module."""
    callbacks_mod = types.ModuleType("langchain_core.callbacks")

    class BaseCallbackHandler:  # the real one has many more no-op defaults
        pass

    callbacks_mod.BaseCallbackHandler = BaseCallbackHandler  # type: ignore[attr-defined]
    core_mod = types.ModuleType("langchain_core")
    core_mod.callbacks = callbacks_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "langchain_core", core_mod)
    monkeypatch.setitem(sys.modules, "langchain_core.callbacks", callbacks_mod)
    odyssey.shutdown()
    yield
    odyssey.shutdown()


def start(tmp_path, **kw):
    return odyssey.init(
        spool_dir=tmp_path / "spool",
        out_dir=tmp_path / "out",
        drain_interval=None,
        **kw,
    )


def events(jid):
    client = odyssey.get_client()
    assert client is not None
    return client.spool.read(jid)


def rid():
    return uuid.uuid4()


def test_importing_odyssey_does_not_import_langchain(monkeypatch):
    monkeypatch.delitem(sys.modules, "langchain_core", raising=False)
    import importlib

    importlib.reload(importlib.import_module("odyssey"))
    assert "langchain_core" not in sys.modules


def test_a_plain_llm_call_is_recorded_as_one_journey(tmp_path):
    from odyssey.integrations.langchain import OdysseyCallbackHandler

    start(tmp_path)
    handler = OdysseyCallbackHandler()
    run_id = rid()

    handler.on_llm_start({}, ["book me a slot"], run_id=run_id)
    handler.on_llm_end(
        FakeLLMResult([[FakeGeneration(text="sure, when?")]]), run_id=run_id
    )

    recorded = events(str(run_id))
    kinds = [e.kind for e in recorded]
    assert kinds == ["message", "message", "terminal"]
    assert recorded[0].message is not None
    assert recorded[0].message.role == "user"
    assert recorded[0].message.content == "book me a slot"
    assert recorded[1].message is not None
    assert recorded[1].message.role == "assistant"
    assert recorded[1].message.content == "sure, when?"
    assert recorded[2].terminal is not None
    assert recorded[2].terminal.termination_reason == "ENV_DONE"


def test_chat_model_messages_map_roles_correctly(tmp_path):
    from odyssey.integrations.langchain import OdysseyCallbackHandler

    start(tmp_path)
    handler = OdysseyCallbackHandler()
    run_id = rid()

    handler.on_chat_model_start(
        {},
        [[FakeMessage("system", "be helpful"), FakeMessage("human", "hi")]],
        run_id=run_id,
    )
    handler.on_llm_end(
        FakeLLMResult([[FakeGeneration(message=FakeMessage("ai", "hello!"))]]),
        run_id=run_id,
    )

    recorded = events(str(run_id))
    roles = [e.message.role for e in recorded if e.kind == "message" and e.message]
    assert roles == ["system", "user", "assistant"]


def test_a_chain_wrapping_an_llm_and_a_tool_is_one_journey(tmp_path):
    from odyssey.integrations.langchain import OdysseyCallbackHandler

    start(tmp_path)
    handler = OdysseyCallbackHandler()
    chain_id, llm_id, tool_id = rid(), rid(), rid()

    handler.on_chain_start({}, {"input": "book"}, run_id=chain_id)
    handler.on_llm_start({}, ["book me a slot"], run_id=llm_id, parent_run_id=chain_id)
    handler.on_llm_end(
        FakeLLMResult([[FakeGeneration(text="calling tool")]]),
        run_id=llm_id,
        parent_run_id=chain_id,
    )
    handler.on_tool_start(
        {"name": "book_slot"}, "tuesday 3pm", run_id=tool_id, parent_run_id=chain_id
    )
    handler.on_tool_end("booked", run_id=tool_id, parent_run_id=chain_id)
    handler.on_chain_end({"output": "done"}, run_id=chain_id)

    # Nothing under the child run_ids -- everything landed under the chain.
    assert events(str(llm_id)) == []
    assert events(str(tool_id)) == []

    recorded = events(str(chain_id))
    kinds = [e.kind for e in recorded]
    assert kinds == ["message", "message", "message", "message", "terminal"]
    assert (
        recorded[2].message is not None and recorded[2].message.tool_calls is not None
    )
    assert recorded[2].message.tool_calls[0].name == "book_slot"
    assert (
        recorded[3].message is not None
        and recorded[3].message.tool_response is not None
    )
    assert recorded[3].message.tool_response.response == "booked"


def test_llm_end_does_not_close_a_journey_that_belongs_to_a_chain(tmp_path):
    """The nested LLM call ending must not terminate the chain's journey --
    only the chain's own end/error does."""
    from odyssey.integrations.langchain import OdysseyCallbackHandler

    start(tmp_path)
    handler = OdysseyCallbackHandler()
    chain_id, llm_id = rid(), rid()

    handler.on_chain_start({}, {}, run_id=chain_id)
    handler.on_llm_start({}, ["hi"], run_id=llm_id, parent_run_id=chain_id)
    handler.on_llm_end(
        FakeLLMResult([[FakeGeneration(text="hey")]]),
        run_id=llm_id,
        parent_run_id=chain_id,
    )

    recorded = events(str(chain_id))
    assert "terminal" not in [e.kind for e in recorded]

    handler.on_chain_end({}, run_id=chain_id)
    assert "terminal" in [e.kind for e in events(str(chain_id))]


def test_a_chain_error_closes_the_journey_with_error_reason(tmp_path):
    from odyssey.integrations.langchain import OdysseyCallbackHandler

    start(tmp_path)
    handler = OdysseyCallbackHandler()
    chain_id = rid()

    handler.on_chain_start({}, {}, run_id=chain_id)
    handler.on_chain_error(RuntimeError("boom"), run_id=chain_id)

    recorded = events(str(chain_id))
    terminal = [e for e in recorded if e.kind == "terminal"][0].terminal
    assert terminal is not None
    assert terminal.termination_reason == "ERROR"
    assert "boom" in (terminal.error or "")


def test_a_second_top_level_run_is_a_separate_journey(tmp_path):
    from odyssey.integrations.langchain import OdysseyCallbackHandler

    start(tmp_path)
    handler = OdysseyCallbackHandler()
    run_a, run_b = rid(), rid()

    handler.on_llm_start({}, ["a"], run_id=run_a)
    handler.on_llm_end(FakeLLMResult([[FakeGeneration(text="a-reply")]]), run_id=run_a)
    handler.on_llm_start({}, ["b"], run_id=run_b)
    handler.on_llm_end(FakeLLMResult([[FakeGeneration(text="b-reply")]]), run_id=run_b)

    assert len(events(str(run_a))) == 3
    assert len(events(str(run_b))) == 3


def test_metadata_is_passed_through_to_the_journey_header(tmp_path):
    from odyssey.integrations.langchain import OdysseyCallbackHandler

    client = start(tmp_path)
    handler = OdysseyCallbackHandler(metadata={"tenant": "acme"})
    run_id = rid()
    handler.on_llm_start({}, ["hi"], run_id=run_id)
    handler.on_llm_end(FakeLLMResult([[FakeGeneration(text="hey")]]), run_id=run_id)

    header = client.spool.header(str(run_id))
    assert header is not None
    assert header.journey_metadata == {"tenant": "acme"}
    assert header.data_source == "langchain"


def test_a_capture_failure_never_raises(tmp_path):
    """The never-raise contract every integration in this codebase holds."""
    from odyssey.integrations.langchain import OdysseyCallbackHandler

    start(tmp_path)
    handler = OdysseyCallbackHandler()
    run_id = rid()
    # `generations=None` would break naive iteration; must be swallowed, not raised.
    handler.on_llm_end(FakeLLMResult(None), run_id=run_id)  # no prior on_llm_start


# --------------------------------------------------------------------------
# LangGraph compatibility (item 0'.2) — no extra code, same callback tree
#
# LangGraph dispatches through the exact standard LangChain callback
# interface these tests already exercise: `on_chain_start`/`on_chain_end`
# for the graph's own top-level run *and* for every node's run (parented to
# the graph's run_id), `on_chat_model_start`/`on_llm_end` for a chat model
# called inside a node, and `on_tool_start`/`on_tool_end` for a `ToolNode`.
# The run_id/parent_run_id shapes below are not invented -- they are the
# exact events a real `langgraph.graph.StateGraph(...).compile().invoke(...,
# config={"callbacks": [handler]})` was observed emitting (langgraph 1.2.11,
# langchain-core 1.6.1, both `invoke()` and `ainvoke()`), replayed here
# without needing the real packages installed for this suite to run.
# --------------------------------------------------------------------------


def test_a_langgraph_run_collapses_into_one_journey(tmp_path):
    """graph -> node 'a' -> node 'b', each its own chain run, one journey."""
    from odyssey.integrations.langchain import OdysseyCallbackHandler

    start(tmp_path)
    handler = OdysseyCallbackHandler()
    graph_id, node_a_id, node_b_id = rid(), rid(), rid()

    handler.on_chain_start({}, {"x": 0}, run_id=graph_id)
    handler.on_chain_start({}, {"x": 0}, run_id=node_a_id, parent_run_id=graph_id)
    handler.on_chain_end({"x": 1}, run_id=node_a_id, parent_run_id=graph_id)
    handler.on_chain_start({}, {"x": 1}, run_id=node_b_id, parent_run_id=graph_id)
    handler.on_chain_end({"x": 11}, run_id=node_b_id, parent_run_id=graph_id)
    handler.on_chain_end({"x": 11}, run_id=graph_id)

    assert events(str(node_a_id)) == []
    assert events(str(node_b_id)) == []
    recorded = events(str(graph_id))
    assert [e.kind for e in recorded] == ["terminal"]


def test_a_chat_model_called_inside_a_langgraph_node_joins_the_graphs_journey(
    tmp_path,
):
    """A node function calling `llm.invoke(...)` without passing `config`
    still lands under the graph's journey -- LangChain propagates callbacks
    via contextvars, and the run tree already reflects that by the time it
    reaches this handler (parent_run_id is the node's run_id either way)."""
    from odyssey.integrations.langchain import OdysseyCallbackHandler

    start(tmp_path)
    handler = OdysseyCallbackHandler()
    graph_id, node_id, llm_id = rid(), rid(), rid()

    handler.on_chain_start({}, {}, run_id=graph_id)
    handler.on_chain_start({}, {}, run_id=node_id, parent_run_id=graph_id)
    handler.on_chat_model_start(
        {},
        [[FakeMessage("human", "hello")]],
        run_id=llm_id,
        parent_run_id=node_id,
    )
    handler.on_llm_end(
        FakeLLMResult([[FakeGeneration(message=FakeMessage("ai", "hi there"))]]),
        run_id=llm_id,
        parent_run_id=node_id,
    )
    handler.on_chain_end({}, run_id=node_id, parent_run_id=graph_id)
    handler.on_chain_end({}, run_id=graph_id)

    recorded = events(str(graph_id))
    roles = [e.message.role for e in recorded if e.kind == "message" and e.message]
    assert roles == ["user", "assistant"]
    assert recorded[-1].kind == "terminal"


def test_a_toolnode_inside_a_langgraph_run_records_the_tool_call(tmp_path):
    """`ToolNode` dispatches `on_tool_start`/`on_tool_end` parented to the
    tools node's own run_id, itself parented to the graph's run_id."""
    from odyssey.integrations.langchain import OdysseyCallbackHandler

    start(tmp_path)
    handler = OdysseyCallbackHandler()
    graph_id, model_node_id, tools_node_id, tool_id = rid(), rid(), rid(), rid()

    handler.on_chain_start({}, {}, run_id=graph_id)
    handler.on_chain_start({}, {}, run_id=model_node_id, parent_run_id=graph_id)
    handler.on_chain_end({}, run_id=model_node_id, parent_run_id=graph_id)
    handler.on_chain_start({}, {}, run_id=tools_node_id, parent_run_id=graph_id)
    handler.on_tool_start(
        {"name": "book"},
        '{"day": "mon"}',
        run_id=tool_id,
        parent_run_id=tools_node_id,
    )
    handler.on_tool_end("booked mon", run_id=tool_id, parent_run_id=tools_node_id)
    handler.on_chain_end({}, run_id=tools_node_id, parent_run_id=graph_id)
    handler.on_chain_end({}, run_id=graph_id)

    recorded = events(str(graph_id))
    tool_calls = [
        e.message
        for e in recorded
        if e.kind == "message" and e.message and e.message.tool_calls
    ]
    tool_responses = [
        e.message
        for e in recorded
        if e.kind == "message" and e.message and e.message.tool_response
    ]
    assert tool_calls[0].tool_calls is not None
    assert tool_calls[0].tool_calls[0].name == "book"
    assert tool_responses[0].tool_response is not None
    assert tool_responses[0].tool_response.response == "booked mon"
    assert recorded[-1].kind == "terminal"
