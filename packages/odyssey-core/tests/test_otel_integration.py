"""OpenTelemetry span bridge (items 0.11 / 0'.3).

No real ``opentelemetry-sdk`` install: a fake ``opentelemetry.sdk.trace``/
``opentelemetry.trace`` package is injected into ``sys.modules``, same
technique ``test_langchain_integration.py`` uses for ``langchain_core`` —
proves the module never imports the real package until
``OdysseySpanProcessor()`` is actually called, and keeps this file runnable
without the optional dependency installed.

Most of the interesting logic lives in ``_Recorder``, which takes plain
values (a hex ``trace_id``, dict attributes, ``(name, attrs)`` event tuples)
rather than real OTel objects, so it's exercised directly in most tests —
only the "does the real wiring translate a span correctly" tests go through
the fake-SDK-backed ``OdysseySpanProcessor()`` factory.
"""

from __future__ import annotations

import enum
import json
import sys
import types

import pytest

import odyssey
from odyssey.integrations.otel import _Recorder


class FakeStatusCode(enum.Enum):
    UNSET = 0
    OK = 1
    ERROR = 2


class FakeStatus:
    def __init__(self, code=FakeStatusCode.UNSET, description=None):
        self.status_code = code
        self.description = description


class FakeSpanContext:
    def __init__(self, trace_id):
        self.trace_id = trace_id


class FakeEvent:
    def __init__(self, name, attributes):
        self.name = name
        self.attributes = attributes


class FakeSpan:
    def __init__(
        self,
        trace_id,
        *,
        parent=None,
        attributes=None,
        events=None,
        status=None,
    ):
        self.context = FakeSpanContext(trace_id)
        self.parent = parent
        self.attributes = attributes or {}
        self.events = events or []
        self.status = status or FakeStatus()

    def get_span_context(self):
        return self.context


@pytest.fixture(autouse=True)
def fake_sdk(monkeypatch):
    """Install a fake ``opentelemetry`` package and reset odyssey's state."""
    trace_sdk_mod = types.ModuleType("opentelemetry.sdk.trace")

    class SpanProcessor:  # the real one is an ABC; a plain base is enough here
        pass

    trace_sdk_mod.SpanProcessor = SpanProcessor  # type: ignore[attr-defined]

    sdk_mod = types.ModuleType("opentelemetry.sdk")
    sdk_mod.trace = trace_sdk_mod  # type: ignore[attr-defined]

    trace_mod = types.ModuleType("opentelemetry.trace")
    trace_mod.StatusCode = FakeStatusCode  # type: ignore[attr-defined]

    otel_mod = types.ModuleType("opentelemetry")
    otel_mod.sdk = sdk_mod  # type: ignore[attr-defined]
    otel_mod.trace = trace_mod  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "opentelemetry", otel_mod)
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk", sdk_mod)
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk.trace", trace_sdk_mod)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", trace_mod)
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


def roles(jid):
    return [e.message.role for e in events(jid) if e.kind == "message" and e.message]


TRACE = "0" * 24 + "abc123"  # any 32-hex-char stand-in for a real trace_id


# --------------------------------------------------------------------------
# The wrapper stays a wrapper
# --------------------------------------------------------------------------


def test_importing_odyssey_does_not_import_otel(monkeypatch):
    for name in ("opentelemetry", "opentelemetry.sdk", "opentelemetry.sdk.trace"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    import importlib

    importlib.reload(importlib.import_module("odyssey"))
    assert "opentelemetry" not in sys.modules


# --------------------------------------------------------------------------
# _Recorder — the capture logic, exercised directly
# --------------------------------------------------------------------------


def test_a_genai_span_with_input_output_message_attributes_is_recorded(tmp_path):
    start(tmp_path)
    recorder = _Recorder(data_source="otel", metadata=None)

    recorder.on_start(TRACE)
    recorder.on_end(
        trace_id=TRACE,
        is_root=True,
        attributes={
            "gen_ai.system": "openai",
            "gen_ai.request.model": "gpt-4.1",
            "gen_ai.input.messages": json.dumps(
                [{"role": "user", "content": "book Tuesday"}]
            ),
            "gen_ai.output.messages": json.dumps(
                [{"role": "assistant", "content": "sure, when?"}]
            ),
        },
        events=[],
        ok=True,
        description=None,
    )

    recorded = events(TRACE)
    assert [e.kind for e in recorded] == ["message", "message", "terminal"]
    assert recorded[0].message is not None
    assert recorded[0].message.role == "user"
    assert recorded[0].message.content == "book Tuesday"
    assert recorded[0].model_id == "gpt-4.1"
    assert recorded[1].message is not None
    assert recorded[1].message.role == "assistant"
    assert recorded[1].message.content == "sure, when?"
    assert recorded[2].terminal is not None
    assert recorded[2].terminal.termination_reason == "ENV_DONE"


def test_content_prompt_completion_events_are_used_when_no_attributes(tmp_path):
    """The older, still-widely-implemented event-based shape."""
    start(tmp_path)
    recorder = _Recorder(data_source="otel", metadata=None)

    recorder.on_start(TRACE)
    recorder.on_end(
        trace_id=TRACE,
        is_root=True,
        attributes={"gen_ai.system": "openai", "gen_ai.request.model": "gpt-4.1"},
        events=[
            (
                "gen_ai.content.prompt",
                {"gen_ai.prompt": json.dumps([{"role": "user", "content": "hi"}])},
            ),
            (
                "gen_ai.content.completion",
                {
                    "gen_ai.completion": json.dumps(
                        [{"role": "assistant", "content": "hello"}]
                    )
                },
            ),
        ],
        ok=True,
        description=None,
    )

    assert roles(TRACE) == ["user", "assistant"]


def test_legacy_gen_ai_prompt_completion_attributes_are_the_last_resort(tmp_path):
    start(tmp_path)
    recorder = _Recorder(data_source="otel", metadata=None)

    recorder.on_start(TRACE)
    recorder.on_end(
        trace_id=TRACE,
        is_root=True,
        attributes={
            "gen_ai.system": "openai",
            "gen_ai.prompt": json.dumps([{"role": "user", "content": "hi"}]),
            "gen_ai.completion": json.dumps([{"role": "assistant", "content": "hey"}]),
        },
        events=[],
        ok=True,
        description=None,
    )

    assert roles(TRACE) == ["user", "assistant"]


def test_a_non_genai_span_contributes_no_message_but_still_closes_the_journey(
    tmp_path,
):
    start(tmp_path)
    recorder = _Recorder(data_source="otel", metadata=None)

    recorder.on_start(TRACE)
    recorder.on_end(
        trace_id=TRACE,
        is_root=True,
        attributes={"some.other.attribute": "value"},
        events=[],
        ok=True,
        description=None,
    )

    recorded = events(TRACE)
    assert [e.kind for e in recorded] == ["terminal"]


def test_nested_spans_in_the_same_trace_join_one_journey(tmp_path):
    """Two spans sharing a trace_id -- OTel's own flattening, no root
    tracking of our own needed."""
    start(tmp_path)
    recorder = _Recorder(data_source="otel", metadata=None)

    recorder.on_start(TRACE)
    recorder.on_end(
        trace_id=TRACE,
        is_root=False,  # a child span
        attributes={
            "gen_ai.system": "openai",
            "gen_ai.request.model": "gpt-4.1",
            "gen_ai.input.messages": json.dumps([{"role": "user", "content": "hi"}]),
            "gen_ai.output.messages": json.dumps(
                [{"role": "assistant", "content": "hey"}]
            ),
        },
        events=[],
        ok=True,
        description=None,
    )
    # The orchestration span (the root) carries no GenAI content of its own.
    recorder.on_end(
        trace_id=TRACE,
        is_root=True,
        attributes={},
        events=[],
        ok=True,
        description=None,
    )

    recorded = events(TRACE)
    assert [e.kind for e in recorded] == ["message", "message", "terminal"]


def test_an_error_status_closes_the_journey_with_error_reason(tmp_path):
    start(tmp_path)
    recorder = _Recorder(data_source="otel", metadata=None)

    recorder.on_start(TRACE)
    recorder.on_end(
        trace_id=TRACE,
        is_root=True,
        attributes={},
        events=[],
        ok=False,
        description="boom",
    )

    terminal = [e for e in events(TRACE) if e.kind == "terminal"][0].terminal
    assert terminal is not None
    assert terminal.termination_reason == "ERROR"
    assert terminal.error == "boom"


def test_tool_calls_in_gen_ai_content_are_parsed(tmp_path):
    start(tmp_path)
    recorder = _Recorder(data_source="otel", metadata=None)

    recorder.on_start(TRACE)
    recorder.on_end(
        trace_id=TRACE,
        is_root=True,
        attributes={
            "gen_ai.system": "openai",
            "gen_ai.output.messages": json.dumps(
                [
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "c1",
                                "function": {
                                    "name": "book",
                                    "arguments": json.dumps({"day": "mon"}),
                                },
                            }
                        ],
                    }
                ]
            ),
        },
        events=[],
        ok=True,
        description=None,
    )

    msgs = [e.message for e in events(TRACE) if e.kind == "message" and e.message]
    tool_calls = msgs[0].tool_calls
    assert tool_calls is not None
    assert tool_calls[0].name == "book"
    assert tool_calls[0].arguments == {"day": "mon"}
    assert tool_calls[0].id == "c1"


def test_malformed_gen_ai_content_is_reported_not_fatal(tmp_path):
    client_obj = start(tmp_path)
    recorder = _Recorder(data_source="otel", metadata=None)

    recorder.on_start(TRACE)
    recorder.on_end(
        trace_id=TRACE,
        is_root=True,
        attributes={
            "gen_ai.system": "openai",
            "gen_ai.input.messages": "not json at all {",
        },
        events=[],
        ok=True,
        description=None,
    )

    recorded = events(TRACE)
    assert [e.kind for e in recorded] == ["terminal"]
    # Reported (counted), not fatal -- the journey still closes cleanly.
    assert client_obj.stats.capture_errors == 1


def test_a_second_top_level_trace_is_a_separate_journey(tmp_path):
    start(tmp_path)
    recorder = _Recorder(data_source="otel", metadata=None)

    trace_a, trace_b = "a" * 32, "b" * 32
    for trace_id in (trace_a, trace_b):
        recorder.on_start(trace_id)
        recorder.on_end(
            trace_id=trace_id,
            is_root=True,
            attributes={},
            events=[],
            ok=True,
            description=None,
        )

    assert len(events(trace_a)) == 1
    assert len(events(trace_b)) == 1


def test_metadata_is_passed_through_to_the_journey_header(tmp_path):
    client_obj = start(tmp_path)
    recorder = _Recorder(data_source="otel", metadata={"tenant": "acme"})

    recorder.on_start(TRACE)
    recorder.on_end(
        trace_id=TRACE,
        is_root=True,
        attributes={},
        events=[],
        ok=True,
        description=None,
    )

    header = client_obj.spool.header(TRACE)
    assert header is not None
    assert header.journey_metadata == {"tenant": "acme"}
    assert header.data_source == "otel"


def test_a_capture_failure_never_raises(tmp_path):
    """The never-raise contract every integration in this codebase holds."""
    start(tmp_path)
    recorder = _Recorder(data_source="otel", metadata=None)
    # No prior on_start -- on_end must not blow up building a fresh context.
    recorder.on_end(
        trace_id=TRACE,
        is_root=True,
        attributes={},
        events=[],
        ok=True,
        description=None,
    )


# --------------------------------------------------------------------------
# OdysseySpanProcessor() — the real wiring, against the fake SDK base class
# --------------------------------------------------------------------------


def test_the_processor_translates_a_root_span_end_to_end(tmp_path):
    start(tmp_path)
    from odyssey.integrations.otel import OdysseySpanProcessor

    processor = OdysseySpanProcessor()
    span = FakeSpan(
        trace_id=0xABCDEF,
        parent=None,
        attributes={
            "gen_ai.system": "openai",
            "gen_ai.request.model": "gpt-4.1",
            "gen_ai.input.messages": json.dumps([{"role": "user", "content": "hi"}]),
            "gen_ai.output.messages": json.dumps(
                [{"role": "assistant", "content": "hey"}]
            ),
        },
        status=FakeStatus(FakeStatusCode.OK),
    )

    processor.on_start(span)
    processor.on_end(span)

    jid = format(0xABCDEF, "032x")
    assert roles(jid) == ["user", "assistant"]
    terminal = [e for e in events(jid) if e.kind == "terminal"][0].terminal
    assert terminal is not None
    assert terminal.termination_reason == "ENV_DONE"


def test_the_processor_maps_error_status_to_the_error_reason(tmp_path):
    start(tmp_path)
    from odyssey.integrations.otel import OdysseySpanProcessor

    processor = OdysseySpanProcessor()
    span = FakeSpan(
        trace_id=0x1,
        parent=None,
        status=FakeStatus(FakeStatusCode.ERROR, "provider timeout"),
    )

    processor.on_start(span)
    processor.on_end(span)

    jid = format(0x1, "032x")
    terminal = [e for e in events(jid) if e.kind == "terminal"][0].terminal
    assert terminal is not None
    assert terminal.termination_reason == "ERROR"
    assert terminal.error == "provider timeout"


def test_the_processor_treats_a_span_with_a_parent_as_non_root(tmp_path):
    start(tmp_path)
    from odyssey.integrations.otel import OdysseySpanProcessor

    processor = OdysseySpanProcessor()
    child = FakeSpan(trace_id=0x2, parent=object())

    processor.on_start(child)
    processor.on_end(child)

    jid = format(0x2, "032x")
    # No terminal yet -- only the (absent, in this test) root's end closes it.
    assert "terminal" not in [e.kind for e in events(jid)]


def test_shutdown_and_force_flush_are_safe_no_ops(tmp_path):
    start(tmp_path)
    from odyssey.integrations.otel import OdysseySpanProcessor

    processor = OdysseySpanProcessor()
    processor.shutdown()
    assert processor.force_flush() is True
