"""Gemini capture: the drop-in client, the opt-in patch, and the same
"providers resend the whole conversation" problem test_integrations.py covers
for Anthropic — see that file's docstring. Gemini's own wrinkles (``contents``
of ``parts`` rather than ``messages``, ``role="model"`` not ``"assistant"``,
system prompt/tools nested under ``config``, a ``function_response`` part
standing in for a dedicated tool role) get their own coverage below.

No network and no real ``google-genai`` install: a fake ``google.genai``
package is injected into ``sys.modules``, which is also what proves the
wrapper never imports the provider at module scope.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

import odyssey

TOOLS = [
    {
        "function_declarations": [
            {
                "name": "book",
                "description": "book a slot",
                "parameters": {
                    "type": "object",
                    "properties": {"day": {"type": "string"}},
                },
            }
        ]
    }
]


class FakeResponse:
    """Shaped like a ``GenerateContentResponse``, including ``model_dump()``."""

    def __init__(
        self,
        parts,
        *,
        role="model",
        model_version="gemini-2.0-flash",
        finish_reason="STOP",
        usage=None,
        response_id="resp_fake",
    ):
        self._d = {
            "candidates": [
                {
                    "content": {"role": role, "parts": parts},
                    "finish_reason": finish_reason,
                }
            ],
            "model_version": model_version,
            "response_id": response_id,
            "usage_metadata": usage
            or {
                "prompt_token_count": 11,
                "response_token_count": 7,
                "total_token_count": 18,
            },
        }

    def model_dump(self):
        return dict(self._d)

    @property
    def candidates(self):
        return self._d["candidates"]


class FakeModels:
    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls: list = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if not self._scripted:
            return FakeResponse([{"text": "ok"}])
        return self._scripted.pop(0)


class FakeAsyncModels(FakeModels):
    async def generate_content(self, **kwargs):  # type: ignore[override]
        return FakeModels.generate_content(self, **kwargs)


class FakeAio:
    def __init__(self, scripted):
        self.models = FakeAsyncModels(scripted)


class FakeClient:
    def __init__(self, scripted=(), **_kw):
        self.models = FakeModels(scripted)
        self.aio = FakeAio(scripted)


@pytest.fixture(autouse=True)
def fake_sdk(monkeypatch):
    """Install a fake ``google.genai`` package and reset odyssey's global state."""
    google_pkg = types.ModuleType("google")
    genai_pkg = types.ModuleType("google.genai")
    genai_pkg.Client = FakeClient  # type: ignore[attr-defined]
    google_pkg.genai = genai_pkg  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google", google_pkg)
    monkeypatch.setitem(sys.modules, "google.genai", genai_pkg)
    odyssey.shutdown()
    yield genai_pkg
    from odyssey.integrations.gemini import uninstrument

    uninstrument()
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


def contents_of(role, text):
    return [{"role": role, "parts": [{"text": text}]}]


# --------------------------------------------------------------------------
# The wrapper stays a wrapper
# --------------------------------------------------------------------------


def test_importing_odyssey_does_not_import_the_provider(monkeypatch):
    """dependencies = [] is only true if `import odyssey` stays provider-free."""
    monkeypatch.delitem(sys.modules, "google", raising=False)
    monkeypatch.delitem(sys.modules, "google.genai", raising=False)
    import importlib

    importlib.reload(importlib.import_module("odyssey"))
    assert "google.genai" not in sys.modules


def test_unknown_attributes_delegate_to_the_real_client(tmp_path, fake_sdk):
    start(tmp_path)
    from odyssey.integrations.gemini import Client

    fake_sdk.Client = lambda **kw: types.SimpleNamespace(
        models=FakeModels([]), aio=FakeAio([]), beta="passthrough"
    )
    client = Client()
    assert client.beta == "passthrough"


def test_the_providers_return_value_is_passed_through_untouched(tmp_path):
    start(tmp_path)
    from odyssey.integrations.gemini import Client

    expected = FakeResponse([{"text": "hi"}])
    client = Client(scripted=[expected])
    with odyssey.journey(id="j"):
        assert client.models.generate_content(model="m", contents="hello") is expected


def test_a_provider_error_propagates_unchanged(tmp_path):
    client_obj = start(tmp_path)
    from odyssey.integrations.gemini import Client

    client = Client()

    def boom(**_kw):
        raise TimeoutError("provider down")

    client.models._inner.generate_content = boom
    with odyssey.journey(id="j"):
        with pytest.raises(TimeoutError, match="provider down"):
            client.models.generate_content(model="m", contents="hi")

    # The prompt is recorded before the call, so a timed-out turn still shows
    # what was asked rather than leaving a hole.
    assert roles("j") == ["user"]
    assert client_obj.stats.capture_errors == 0


# --------------------------------------------------------------------------
# The hard part: no duplicated history
# --------------------------------------------------------------------------


def test_three_turns_record_each_message_exactly_once(tmp_path):
    start(tmp_path)
    from odyssey.integrations.gemini import Client

    client = Client(
        scripted=[
            FakeResponse([{"text": "a1"}]),
            FakeResponse([{"text": "a2"}]),
            FakeResponse([{"text": "a3"}]),
        ]
    )
    contents = contents_of("user", "q1")
    with odyssey.journey(id="j"):
        client.models.generate_content(model="m", contents=contents)
        contents = contents + [{"role": "model", "parts": [{"text": "a1"}]}]
        contents = contents + contents_of("user", "q2")
        client.models.generate_content(model="m", contents=contents)
        contents = contents + [{"role": "model", "parts": [{"text": "a2"}]}]
        contents = contents + contents_of("user", "q3")
        client.models.generate_content(model="m", contents=contents)

    assert roles("j") == ["user", "assistant"] * 3
    texts = [e.message.content for e in events("j") if e.message]
    assert texts == ["q1", "a1", "q2", "a2", "q3", "a3"]


def test_the_system_prompt_is_recorded_once_though_it_is_sent_every_call(tmp_path):
    start(tmp_path)
    from odyssey.integrations.gemini import Client

    client = Client(
        scripted=[FakeResponse([{"text": "a1"}]), FakeResponse([{"text": "a2"}])]
    )
    config = {"system_instruction": "be terse."}
    with odyssey.journey(id="j"):
        client.models.generate_content(
            model="m", contents=contents_of("user", "q1"), config=config
        )
        client.models.generate_content(
            model="m",
            contents=contents_of("user", "q1")
            + [{"role": "model", "parts": [{"text": "a1"}]}]
            + contents_of("user", "q2"),
            config=config,
        )

    assert roles("j") == ["system", "user", "assistant", "user", "assistant"]


def test_a_changed_system_prompt_is_recorded_again(tmp_path):
    start(tmp_path)
    from odyssey.integrations.gemini import Client

    client = Client(
        scripted=[FakeResponse([{"text": "a1"}]), FakeResponse([{"text": "a2"}])]
    )
    with odyssey.journey(id="j"):
        client.models.generate_content(
            model="m",
            contents=contents_of("user", "q1"),
            config={"system_instruction": "be terse."},
        )
        client.models.generate_content(
            model="m",
            contents=contents_of("user", "q1")
            + [{"role": "model", "parts": [{"text": "a1"}]}]
            + contents_of("user", "q2"),
            config={"system_instruction": "be verbose."},
        )

    assert roles("j") == ["system", "user", "assistant", "system", "user", "assistant"]


def test_tool_definitions_are_recorded_once_not_on_every_turn(tmp_path):
    start(tmp_path)
    from odyssey.integrations.gemini import Client

    client = Client(
        scripted=[FakeResponse([{"text": "a1"}]), FakeResponse([{"text": "a2"}])]
    )
    with odyssey.journey(id="j"):
        client.models.generate_content(
            model="m", contents=contents_of("user", "q1"), config={"tools": TOOLS}
        )
        client.models.generate_content(
            model="m",
            contents=contents_of("user", "q1")
            + [{"role": "model", "parts": [{"text": "a1"}]}]
            + contents_of("user", "q2"),
            config={"tools": TOOLS},
        )

    msgs = [e.message for e in events("j") if e.kind == "message" and e.message]
    with_tools = [m for m in msgs if m.tool_definitions]
    assert len(with_tools) == 1
    assert with_tools[0].tool_definitions[0].name == "book"


# --------------------------------------------------------------------------
# Gemini's own shape: parts, thought, function_call/function_response
# --------------------------------------------------------------------------


def test_a_function_call_becomes_a_tool_call(tmp_path):
    start(tmp_path)
    from odyssey.integrations.gemini import Client

    response = FakeResponse(
        [{"function_call": {"name": "book", "args": {"day": "mon"}, "id": "c1"}}]
    )
    client = Client(scripted=[response])
    with odyssey.journey(id="j"):
        client.models.generate_content(model="m", contents="book monday")

    msgs = [e.message for e in events("j") if e.kind == "message" and e.message]
    assistant = [m for m in msgs if m.role == "assistant"][-1]
    assert assistant.tool_calls[0].name == "book"
    assert assistant.tool_calls[0].arguments == {"day": "mon"}
    assert assistant.tool_calls[0].id == "c1"


def test_a_function_response_becomes_a_tool_message(tmp_path):
    start(tmp_path)
    from odyssey.integrations.gemini import Client

    client = Client(scripted=[FakeResponse([{"text": "booked"}])])
    contents = [
        {"role": "user", "parts": [{"text": "book monday"}]},
        {
            "role": "model",
            "parts": [
                {"function_call": {"name": "book", "args": {"day": "mon"}, "id": "c1"}}
            ],
        },
        {
            "role": "user",
            "parts": [
                {
                    "function_response": {
                        "id": "c1",
                        "name": "book",
                        "response": {"status": "ok"},
                    }
                }
            ],
        },
    ]
    with odyssey.journey(id="j"):
        client.models.generate_content(model="m", contents=contents)

    msgs = [e.message for e in events("j") if e.kind == "message" and e.message]
    tool_msgs = [m for m in msgs if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_response.name == "book"
    assert tool_msgs[0].tool_response.id == "c1"


def test_a_thought_part_lands_on_reasoning_not_content(tmp_path):
    start(tmp_path)
    from odyssey.integrations.gemini import Client

    response = FakeResponse(
        [{"text": "thinking it through", "thought": True}, {"text": "the answer"}]
    )
    client = Client(scripted=[response])
    with odyssey.journey(id="j"):
        client.models.generate_content(model="m", contents="hi")

    msgs = [e.message for e in events("j") if e.kind == "message" and e.message]
    assistant = [m for m in msgs if m.role == "assistant"][-1]
    assert assistant.content == "the answer"
    assert assistant.reasoning == "thinking it through"


def test_an_unknown_part_type_is_reported_not_fatal(tmp_path):
    client_obj = start(tmp_path)
    from odyssey.integrations.gemini import Client

    response = FakeResponse([{"text": "hi"}, {"inline_data": {"data": "..."}}])
    client = Client(scripted=[response])
    with odyssey.journey(id="j"):
        client.models.generate_content(model="m", contents="hi")

    msgs = [e for e in events("j") if e.kind == "message" and e.message]
    assert any(e.metadata and "unknown_parts" in e.metadata for e in msgs)
    assert client_obj.stats.capture_errors == 0


def test_usage_and_finish_reason_survive(tmp_path):
    start(tmp_path)
    from odyssey.integrations.gemini import Client

    response = FakeResponse(
        [{"text": "hi"}],
        finish_reason="MAX_TOKENS",
        usage={
            "prompt_token_count": 3,
            "response_token_count": 2,
            "total_token_count": 5,
        },
    )
    client = Client(scripted=[response])
    with odyssey.journey(id="j"):
        client.models.generate_content(model="m", contents="hi")

    assistant = [e.message for e in events("j") if e.kind == "message" and e.message][
        -1
    ]
    assert assistant.finish_reason == "MAX_TOKENS"
    assert assistant.usage == {
        "prompt_token_count": 3,
        "response_token_count": 2,
        "total_token_count": 5,
    }


def test_the_model_is_attributed_per_event(tmp_path):
    start(tmp_path)
    from odyssey.integrations.gemini import Client

    client = Client(scripted=[FakeResponse([{"text": "hi"}])])
    with odyssey.journey(id="j"):
        client.models.generate_content(model="gemini-2.0-flash", contents="hi")

    ids = {e.model_id for e in events("j") if e.kind == "message"}
    assert ids == {"gemini-2.0-flash"}


# --------------------------------------------------------------------------
# Journey scoping
# --------------------------------------------------------------------------


def test_a_call_outside_a_journey_still_gets_recorded(tmp_path):
    client_obj = start(tmp_path)
    from odyssey.integrations.gemini import Client

    client = Client(scripted=[FakeResponse([{"text": "hi"}])])
    client.models.generate_content(model="m", contents="hi")

    ids = client_obj.spool.journey_ids()
    assert len(ids) == 1
    assert roles(ids[0]) == ["user", "assistant"]


def test_calls_inside_a_journey_all_join_it(tmp_path):
    start(tmp_path)
    from odyssey.integrations.gemini import Client

    client = Client(
        scripted=[FakeResponse([{"text": "a1"}]), FakeResponse([{"text": "a2"}])]
    )
    with odyssey.journey(id="j"):
        contents = contents_of("user", "q1")
        client.models.generate_content(model="m", contents=contents)
        contents = (
            contents
            + [{"role": "model", "parts": [{"text": "a1"}]}]
            + contents_of("user", "q2")
        )
        client.models.generate_content(model="m", contents=contents)

    assert roles("j") == ["user", "assistant", "user", "assistant"]


def test_the_async_client_records_too(tmp_path):
    start(tmp_path)
    from odyssey.integrations.gemini import Client

    client = Client(scripted=[FakeResponse([{"text": "hi"}])])

    async def main():
        with odyssey.journey(id="j"):
            return await client.aio.models.generate_content(
                model="m", contents=contents_of("user", "q")
            )

    asyncio.run(main())
    assert roles("j") == ["user", "assistant"]


# --------------------------------------------------------------------------
# Opt-in patching
# --------------------------------------------------------------------------


def make_patch_target():
    """A stand-in for google.genai.models, with Models/AsyncModels."""
    module = types.ModuleType("fake_target")

    class Models:
        def __init__(self):
            self.seen = []

        def generate_content(self, **kwargs):
            self.seen.append(kwargs)
            return FakeResponse([{"text": "patched"}])

    class AsyncModels:
        def __init__(self):
            self.seen = []

        async def generate_content(self, **kwargs):
            self.seen.append(kwargs)
            return FakeResponse([{"text": "patched-async"}])

    module.Models = Models  # type: ignore[attr-defined]
    module.AsyncModels = AsyncModels  # type: ignore[attr-defined]
    return module


def test_patching_records_without_touching_the_call_site(tmp_path):
    start(tmp_path)
    from odyssey.integrations.gemini import instrument

    target = make_patch_target()
    instrument(target=target)

    models = target.Models()
    with odyssey.journey(id="j"):
        result = models.generate_content(model="m", contents="hi")

    assert result.candidates[0]["content"]["parts"][0]["text"] == "patched"
    assert roles("j") == ["user", "assistant"]


def test_patching_is_idempotent(tmp_path):
    start(tmp_path)
    from odyssey.integrations.gemini import instrument, is_instrumented

    target = make_patch_target()
    instrument(target=target)
    first = target.Models.generate_content
    instrument(target=make_patch_target())
    assert target.Models.generate_content is first
    assert is_instrumented()


def test_uninstrument_restores_the_original(tmp_path):
    start(tmp_path)
    from odyssey.integrations.gemini import instrument, is_instrumented, uninstrument

    target = make_patch_target()
    original = target.Models.generate_content
    instrument(target=target)
    assert target.Models.generate_content is not original
    uninstrument()
    assert target.Models.generate_content is original
    assert not is_instrumented()


def test_patching_an_unsupported_sdk_shape_raises_for_its_caller(tmp_path):
    start(tmp_path)
    from odyssey.integrations.gemini import instrument

    module = types.ModuleType("broken_target")
    with pytest.raises(AttributeError):
        instrument(target=module)


def test_init_never_dies_because_instrumentation_failed(tmp_path, monkeypatch):
    """A bad instrumentation target is recorded, not fatal to init()."""
    odyssey.shutdown()
    client = odyssey.init(
        spool_dir=tmp_path / "spool",
        out_dir=tmp_path / "out",
        drain_interval=None,
        instrument=["gemini-typo"],
    )
    assert client.stats.capture_errors == 1
