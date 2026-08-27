"""OpenAI (and OpenAI-compatible) capture: the drop-in client, the opt-in
patch, and the same "providers resend the whole conversation" problem
test_integrations.py covers for Anthropic — see that file's docstring.

No network and no real ``openai`` install: a fake SDK is injected into
``sys.modules``, which also proves the wrapper never imports the provider at
module scope.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

import odyssey

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "book",
            "description": "book a slot",
            "parameters": {
                "type": "object",
                "properties": {"day": {"type": "string"}},
            },
        },
    }
]


class FakeResponse:
    """Shaped like an OpenAI ChatCompletion, including model_dump()."""

    def __init__(
        self,
        message,
        *,
        model="gpt-4.1-mini",
        finish_reason="stop",
        usage=None,
        id="chatcmpl-fake",
    ):
        self._d = {
            "id": id,
            "model": model,
            "choices": [
                {"index": 0, "message": message, "finish_reason": finish_reason}
            ],
            "usage": usage
            or {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        }

    def model_dump(self):
        return dict(self._d)

    @property
    def choices(self):
        return self._d["choices"]


class FakeCompletions:
    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls: list = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._scripted:
            return FakeResponse({"role": "assistant", "content": "ok"})
        return self._scripted.pop(0)


class FakeAsyncCompletions(FakeCompletions):
    async def create(self, **kwargs):  # type: ignore[override]
        return FakeCompletions.create(self, **kwargs)


class FakeChat:
    def __init__(self, scripted):
        self.completions = FakeCompletions(scripted)


class FakeAsyncChat:
    def __init__(self, scripted):
        self.completions = FakeAsyncCompletions(scripted)


class FakeClient:
    def __init__(self, scripted=(), **_kw):
        self.chat = FakeChat(scripted)


class FakeAsyncClient:
    def __init__(self, scripted=(), **_kw):
        self.chat = FakeAsyncChat(scripted)


@pytest.fixture(autouse=True)
def fake_sdk(monkeypatch):
    """Install a fake ``openai`` module and reset odyssey's global state."""
    module = types.ModuleType("openai")
    module.OpenAI = FakeClient  # type: ignore[attr-defined]
    module.AsyncOpenAI = FakeAsyncClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", module)
    odyssey.shutdown()
    yield module
    from odyssey.integrations.openai import uninstrument

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


# --------------------------------------------------------------------------
# The wrapper stays a wrapper
# --------------------------------------------------------------------------


def test_importing_odyssey_does_not_import_the_provider(monkeypatch):
    """dependencies = [] is only true if `import odyssey` stays provider-free."""
    monkeypatch.delitem(sys.modules, "openai", raising=False)
    import importlib

    importlib.reload(importlib.import_module("odyssey"))
    assert "openai" not in sys.modules


def test_unknown_attributes_delegate_to_the_real_client(tmp_path, fake_sdk):
    start(tmp_path)
    from odyssey.integrations.openai import OpenAI

    fake_sdk.OpenAI = lambda **kw: types.SimpleNamespace(
        chat=FakeChat([]), beta="passthrough"
    )
    client = OpenAI()
    assert client.beta == "passthrough"


def test_the_providers_return_value_is_passed_through_untouched(tmp_path):
    start(tmp_path)
    from odyssey.integrations.openai import OpenAI

    expected = FakeResponse({"role": "assistant", "content": "hi"})
    client = OpenAI(scripted=[expected])
    with odyssey.journey(id="j"):
        result = client.chat.completions.create(model="gpt-4.1-mini", messages=[])
    assert result is expected


def test_a_provider_error_propagates_unchanged(tmp_path):
    client_obj = start(tmp_path)
    from odyssey.integrations.openai import OpenAI

    client = OpenAI()

    def boom(**_kw):
        raise TimeoutError("provider down")

    client.chat.completions._inner.create = boom
    with odyssey.journey(id="j"):
        with pytest.raises(TimeoutError, match="provider down"):
            client.chat.completions.create(
                model="m", messages=[{"role": "user", "content": "hi"}]
            )

    assert roles("j") == ["user"]
    assert client_obj.stats.capture_errors == 0


def test_base_url_is_forwarded_for_openai_compatible_providers(tmp_path, fake_sdk):
    """The whole reason a separate wrapper per compatible provider isn't
    needed: constructor kwargs pass straight through to the real client."""
    start(tmp_path)
    from odyssey.integrations.openai import OpenAI

    seen = {}

    def fake_ctor(**kw):
        seen.update(kw)
        return types.SimpleNamespace(chat=FakeChat([]))

    fake_sdk.OpenAI = fake_ctor
    OpenAI(base_url="https://api.groq.com/openai/v1", api_key="sk-groq")
    assert seen == {"base_url": "https://api.groq.com/openai/v1", "api_key": "sk-groq"}


# --------------------------------------------------------------------------
# The hard part: no duplicated history — and no separate system handling
# --------------------------------------------------------------------------


def test_three_turns_record_each_message_exactly_once(tmp_path):
    start(tmp_path)
    from odyssey.integrations.openai import OpenAI

    client = OpenAI(
        scripted=[
            FakeResponse({"role": "assistant", "content": "Which day?"}),
            FakeResponse(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "book",
                                "arguments": '{"day": "tue"}',
                            },
                        }
                    ],
                },
                finish_reason="tool_calls",
            ),
            FakeResponse({"role": "assistant", "content": "Booked for 3pm."}),
        ]
    )

    with odyssey.journey(id="j") as j:
        msgs = [
            {"role": "system", "content": "You book appointments."},
            {"role": "user", "content": "Book me an appointment."},
        ]
        r1 = client.chat.completions.create(
            model="gpt-4.1-mini", messages=msgs, tools=TOOLS
        )
        msgs.append(r1.choices[0]["message"])

        msgs.append({"role": "user", "content": "Tuesday at 3."})
        r2 = client.chat.completions.create(
            model="gpt-4.1-mini", messages=msgs, tools=TOOLS
        )
        msgs.append(r2.choices[0]["message"])

        msgs.append(
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": '{"ok": true}',
            }
        )
        client.chat.completions.create(model="gpt-4.1-mini", messages=msgs, tools=TOOLS)
        j.signal("thumbs_up")

    assert roles("j") == [
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
        "tool",
        "assistant",
    ]


def test_the_system_message_is_recorded_once_though_resent_every_call(tmp_path):
    """No separate system-tracking needed: it's message[0], covered by the
    same consumed-count logic as every other turn."""
    start(tmp_path)
    from odyssey.integrations.openai import OpenAI

    client = OpenAI()
    with odyssey.journey(id="j"):
        msgs = [{"role": "system", "content": "be helpful"}]
        for i in range(3):
            msgs.append({"role": "user", "content": f"q{i}"})
            r = client.chat.completions.create(model="m", messages=msgs)
            msgs.append(r.choices[0]["message"])

    assert roles("j").count("system") == 1


def test_tool_definitions_are_recorded_once_not_on_every_turn(tmp_path):
    start(tmp_path)
    from odyssey.integrations.openai import OpenAI

    client = OpenAI()
    with odyssey.journey(id="j"):
        msgs = []
        for i in range(3):
            msgs.append({"role": "user", "content": f"q{i}"})
            r = client.chat.completions.create(model="m", messages=msgs, tools=TOOLS)
            msgs.append(r.choices[0]["message"])

    carrying = [
        e.seq
        for e in events("j")
        if e.kind == "message" and e.message and e.message.tool_definitions
    ]
    assert len(carrying) == 1


def test_a_rebuilt_message_list_resyncs_instead_of_duplicating(tmp_path):
    client_obj = start(tmp_path)
    from odyssey.integrations.openai import OpenAI

    client = OpenAI()
    with odyssey.journey(id="j"):
        long = [{"role": "user", "content": f"q{i}"} for i in range(4)]
        client.chat.completions.create(model="m", messages=long)
        client.chat.completions.create(
            model="m", messages=[{"role": "user", "content": "new"}]
        )

    contents = [
        e.message.content
        for e in events("j")
        if e.kind == "message" and e.message and e.message.role == "user"
    ]
    assert contents == ["q0", "q1", "q2", "q3"]
    assert client_obj.stats.capture_errors == 1
    assert "shrank" in client_obj.stats.recent_errors[0]


# --------------------------------------------------------------------------
# Content
# --------------------------------------------------------------------------


def test_a_tool_call_is_parsed(tmp_path):
    start(tmp_path)
    from odyssey.integrations.openai import OpenAI

    client = OpenAI(
        scripted=[
            FakeResponse(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "book", "arguments": '{"day": "tue"}'},
                        }
                    ],
                },
                finish_reason="tool_calls",
            )
        ]
    )
    with odyssey.journey(id="j"):
        client.chat.completions.create(model="m", messages=[])

    call = [
        e.message.tool_calls[0]
        for e in events("j")
        if e.kind == "message" and e.message and e.message.tool_calls
    ][0]
    assert (call.id, call.name, call.arguments) == ("call_1", "book", {"day": "tue"})


def test_usage_and_finish_reason_survive(tmp_path):
    start(tmp_path)
    from odyssey.integrations.openai import OpenAI

    client = OpenAI(
        scripted=[
            FakeResponse(
                {"role": "assistant", "content": "hi"},
                finish_reason="length",
                usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
            )
        ]
    )
    with odyssey.journey(id="j"):
        client.chat.completions.create(model="m", messages=[])

    msg = [e.message for e in events("j") if e.kind == "message" and e.message][0]
    assert msg.usage == {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}
    assert msg.finish_reason == "length"


def test_sampling_parameters_ride_along_in_metadata(tmp_path):
    start(tmp_path)
    from odyssey.integrations.openai import OpenAI

    client = OpenAI()
    with odyssey.journey(id="j"):
        client.chat.completions.create(
            model="m",
            messages=[{"role": "user", "content": "q"}],
            temperature=0.2,
            max_tokens=256,
        )

    params = [
        (e.metadata or {}).get("params")
        for e in events("j")
        if (e.metadata or {}).get("params")
    ]
    assert params[0] == {"temperature": 0.2, "max_tokens": 256}


def test_the_model_is_attributed_per_event(tmp_path):
    start(tmp_path)
    from odyssey.integrations.openai import OpenAI

    client = OpenAI(
        scripted=[FakeResponse({"role": "assistant", "content": "hi"}, model="gpt-4.1")]
    )
    with odyssey.journey(id="j"):
        client.chat.completions.create(
            model="gpt-4.1", messages=[{"role": "user", "content": "q"}]
        )

    result = odyssey.fold(events("j"), data_source="openai")
    assert result.model_ids == ["gpt-4.1"]


def test_a_malformed_response_entry_is_captured_not_lost(tmp_path):
    """messages_from_openai_chat raises on a missing role; the auto-capture
    path degrades to a best-effort message instead of losing the turn."""
    client_obj = start(tmp_path)
    from odyssey.integrations.openai import OpenAI

    client = OpenAI(scripted=[FakeResponse({"role": None, "content": "hi"})])
    with odyssey.journey(id="j"):
        client.chat.completions.create(model="m", messages=[])

    recorded = [e for e in events("j") if e.kind == "message"]
    assert len(recorded) == 1
    assert (recorded[0].metadata or {}).get("unknown_blocks")
    assert client_obj.stats.capture_errors == 0


# --------------------------------------------------------------------------
# Streaming is passed through, not captured
# --------------------------------------------------------------------------


def test_stream_true_is_not_captured(tmp_path):
    start(tmp_path)
    from odyssey.integrations.openai import OpenAI

    client = OpenAI()
    with odyssey.journey(id="j"):
        client.chat.completions.create(
            model="m", messages=[{"role": "user", "content": "q"}], stream=True
        )
    assert [e for e in events("j") if e.kind == "message"] == []


# --------------------------------------------------------------------------
# Journey scoping
# --------------------------------------------------------------------------


def test_a_call_outside_a_journey_still_gets_recorded(tmp_path):
    start(tmp_path)
    from odyssey.integrations.openai import OpenAI

    client = OpenAI()
    client.chat.completions.create(
        model="m", messages=[{"role": "user", "content": "q"}]
    )

    inner = odyssey.get_client()
    assert inner is not None
    ids = inner.spool.journey_ids()
    assert len(ids) == 1
    assert odyssey.fold(events(ids[0]), data_source="openai").trainable


def test_calls_inside_a_journey_all_join_it(tmp_path):
    start(tmp_path)
    from odyssey.integrations.openai import OpenAI

    client = OpenAI()
    with odyssey.journey(id="one-call"):
        msgs = []
        for i in range(2):
            msgs.append({"role": "user", "content": f"q{i}"})
            r = client.chat.completions.create(model="m", messages=msgs)
            msgs.append(r.choices[0]["message"])

    inner = odyssey.get_client()
    assert inner is not None
    assert inner.spool.journey_ids() == ["one-call"]


def test_the_async_client_records_too(tmp_path):
    start(tmp_path)
    from odyssey.integrations.openai import AsyncOpenAI

    client = AsyncOpenAI()

    async def main():
        with odyssey.journey(id="j"):
            return await client.chat.completions.create(
                model="m", messages=[{"role": "user", "content": "q"}]
            )

    asyncio.run(main())
    assert roles("j") == ["user", "assistant"]


def test_the_async_client_does_not_capture_streamed_calls(tmp_path):
    start(tmp_path)
    from odyssey.integrations.openai import AsyncOpenAI

    client = AsyncOpenAI()

    async def main():
        with odyssey.journey(id="j"):
            await client.chat.completions.create(
                model="m", messages=[{"role": "user", "content": "q"}], stream=True
            )

    asyncio.run(main())
    assert [e for e in events("j") if e.kind == "message"] == []


# --------------------------------------------------------------------------
# Opt-in patching
# --------------------------------------------------------------------------


def make_patch_target():
    """A stand-in for openai.resources.chat.completions, with a Completions."""
    module = types.ModuleType("fake_target")

    class Completions:
        def __init__(self):
            self.seen = []

        def create(self, **kwargs):
            self.seen.append(kwargs)
            return FakeResponse({"role": "assistant", "content": "patched"})

    module.Completions = Completions  # type: ignore[attr-defined]
    return module


def test_patching_records_without_touching_the_call_site(tmp_path):
    start(tmp_path)
    from odyssey.integrations.openai import instrument, is_instrumented

    target = make_patch_target()
    instrument(target)
    assert is_instrumented()

    plain = target.Completions()
    with odyssey.journey(id="j"):
        result = plain.create(model="m", messages=[{"role": "user", "content": "q"}])

    assert result.choices[0]["message"]["content"] == "patched"
    assert roles("j") == ["user", "assistant"]


def test_patching_is_idempotent(tmp_path):
    start(tmp_path)
    from odyssey.integrations.openai import instrument

    target = make_patch_target()
    instrument(target)
    first = target.Completions.create
    instrument(target)
    assert target.Completions.create is first


def test_uninstrument_restores_the_original(tmp_path):
    start(tmp_path)
    from odyssey.integrations.openai import instrument, is_instrumented, uninstrument

    target = make_patch_target()
    original = target.Completions.create
    instrument(target)
    assert target.Completions.create is not original
    uninstrument()
    assert target.Completions.create is original
    assert not is_instrumented()


def test_patching_an_unsupported_sdk_shape_raises_for_its_caller(tmp_path):
    start(tmp_path)
    from odyssey.integrations.openai import instrument

    empty = types.ModuleType("no_completions")
    with pytest.raises(AttributeError, match="not supported"):
        instrument(empty)


def test_init_never_dies_because_instrumentation_failed(tmp_path, monkeypatch):
    monkeypatch.delitem(sys.modules, "openai", raising=False)
    monkeypatch.setitem(
        sys.modules, "openai.resources.chat.completions", types.ModuleType("broken")
    )
    client = start(tmp_path, instrument=["openai"])
    assert client.stats.capture_errors == 1
