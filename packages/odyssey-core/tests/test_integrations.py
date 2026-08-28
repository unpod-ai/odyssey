"""Provider capture: the drop-in client, the opt-in patch, and the hard part.

The hard part is that providers resend the whole conversation on every call.
Recording each request verbatim would multiply the corpus with duplicate turns
that the fold cannot detect — it deduplicates on ``event_id``, and re-recorded
history carries fresh ones. Most of this file is about that.

No network and no real ``anthropic`` install: a fake SDK is injected into
``sys.modules``, which is also what proves the wrapper never imports the provider
at module scope.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

import odyssey

TOOLS = [
    {
        "name": "book",
        "description": "book a slot",
        "input_schema": {"type": "object", "properties": {"day": {"type": "string"}}},
    }
]
SYSTEM = "You book appointments."


class FakeResponse:
    """Shaped like an anthropic Message, including the model_dump() accessor."""

    def __init__(self, blocks, model="claude-opus-5", stop="end_turn"):
        self._d = {
            "id": "msg_fake",
            "role": "assistant",
            "content": blocks,
            "model": model,
            "stop_reason": stop,
            "usage": {"input_tokens": 11, "output_tokens": 7},
        }

    def model_dump(self):
        return dict(self._d)

    @property
    def content(self):
        return self._d["content"]


class FakeStreamBody:
    """Shaped like anthropic's ``MessageStream``: chunks plus a final message."""

    def __init__(self, response):
        self._response = response

    def get_final_message(self):
        return self._response

    @property
    def text_stream(self):
        return iter(["ok"])

    def __iter__(self):
        return iter([])


class FakeStreamManager:
    """Shaped like anthropic's ``MessageStreamManager`` — a plain context manager."""

    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return FakeStreamBody(self._response)

    def __exit__(self, *exc):
        return False


async def _fake_aiter(items):
    for item in items:
        yield item


class FakeAsyncStreamBody:
    """Async counterpart to :class:`FakeStreamBody`."""

    def __init__(self, response):
        self._response = response

    async def get_final_message(self):
        return self._response

    @property
    def text_stream(self):
        return _fake_aiter(["ok"])

    def __aiter__(self):
        return _fake_aiter([])


class FakeAsyncStreamManager:
    """Async counterpart to :class:`FakeStreamManager`."""

    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return FakeAsyncStreamBody(self._response)

    async def __aexit__(self, *exc):
        return False


class FakeMessages:
    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls: list = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._scripted:
            return FakeResponse([{"type": "text", "text": "ok"}])
        return self._scripted.pop(0)

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        if not self._scripted:
            response = FakeResponse([{"type": "text", "text": "ok"}])
        else:
            response = self._scripted.pop(0)
        return FakeStreamManager(response)


class FakeAsyncMessages(FakeMessages):
    async def create(self, **kwargs):  # type: ignore[override]
        return FakeMessages.create(self, **kwargs)

    def stream(self, **kwargs):  # type: ignore[override]
        self.calls.append(kwargs)
        if not self._scripted:
            response = FakeResponse([{"type": "text", "text": "ok"}])
        else:
            response = self._scripted.pop(0)
        return FakeAsyncStreamManager(response)


class FakeClient:
    def __init__(self, scripted=(), **_kw):
        self.messages = FakeMessages(scripted)


class FakeAsyncClient:
    def __init__(self, scripted=(), **_kw):
        self.messages = FakeAsyncMessages(scripted)


@pytest.fixture(autouse=True)
def fake_sdk(monkeypatch):
    """Install a fake ``anthropic`` module and reset odyssey's global state."""
    module = types.ModuleType("anthropic")
    module.Anthropic = FakeClient  # type: ignore[attr-defined]
    module.AsyncAnthropic = FakeAsyncClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", module)
    odyssey.shutdown()
    yield module
    from odyssey.integrations.anthropic import uninstrument

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
    monkeypatch.delitem(sys.modules, "anthropic", raising=False)
    import importlib

    importlib.reload(importlib.import_module("odyssey"))
    assert "anthropic" not in sys.modules


def test_unknown_attributes_delegate_to_the_real_client(tmp_path, fake_sdk):
    start(tmp_path)
    from odyssey.integrations.anthropic import Anthropic

    fake_sdk.Anthropic = lambda **kw: types.SimpleNamespace(
        messages=FakeMessages([]), beta="passthrough"
    )
    client = Anthropic()
    assert client.beta == "passthrough"


def test_the_providers_return_value_is_passed_through_untouched(tmp_path):
    start(tmp_path)
    from odyssey.integrations.anthropic import Anthropic

    expected = FakeResponse([{"type": "text", "text": "hi"}])
    client = Anthropic(scripted=[expected])
    with odyssey.journey(id="j"):
        assert client.messages.create(model="m", messages=[]) is expected


def test_a_provider_error_propagates_unchanged(tmp_path):
    """We never swallow the application's own failure."""
    client_obj = start(tmp_path)
    from odyssey.integrations.anthropic import Anthropic

    client = Anthropic()

    def boom(**_kw):
        raise TimeoutError("provider down")

    client.messages._inner.create = boom
    with odyssey.journey(id="j"):
        with pytest.raises(TimeoutError, match="provider down"):
            client.messages.create(
                model="m", messages=[{"role": "user", "content": "hi"}]
            )

    # The prompt is recorded before the call, so a timed-out turn still shows
    # what was asked rather than leaving a hole.
    assert roles("j") == ["user"]
    assert client_obj.stats.capture_errors == 0


# --------------------------------------------------------------------------
# The hard part: no duplicated history
# --------------------------------------------------------------------------


def test_three_turns_record_each_message_exactly_once(tmp_path):
    start(tmp_path)
    from odyssey.integrations.anthropic import Anthropic

    client = Anthropic(
        scripted=[
            FakeResponse([{"type": "text", "text": "Which day?"}]),
            FakeResponse(
                [
                    {"type": "thinking", "thinking": "tuesday 3pm"},
                    {
                        "type": "tool_use",
                        "id": "tc_1",
                        "name": "book",
                        "input": {"day": "tue"},
                    },
                ],
                stop="tool_use",
            ),
            FakeResponse([{"type": "text", "text": "Booked for 3pm."}]),
        ]
    )

    with odyssey.journey(id="j") as j:
        msgs = [{"role": "user", "content": "Book me an appointment."}]
        r1 = client.messages.create(
            model="claude-opus-5", system=SYSTEM, messages=msgs, tools=TOOLS
        )
        msgs.append({"role": "assistant", "content": r1.content})

        msgs.append({"role": "user", "content": "Tuesday at 3."})
        r2 = client.messages.create(
            model="claude-opus-5", system=SYSTEM, messages=msgs, tools=TOOLS
        )
        msgs.append({"role": "assistant", "content": r2.content})

        msgs.append(
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tc_1", "content": "ok"}
                ],
            }
        )
        client.messages.create(
            model="claude-opus-5", system=SYSTEM, messages=msgs, tools=TOOLS
        )
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


def test_the_system_prompt_is_recorded_once_though_it_is_sent_every_call(tmp_path):
    start(tmp_path)
    from odyssey.integrations.anthropic import Anthropic

    client = Anthropic()
    with odyssey.journey(id="j"):
        msgs = []
        for i in range(3):
            msgs.append({"role": "user", "content": f"q{i}"})
            r = client.messages.create(model="m", system=SYSTEM, messages=msgs)
            msgs.append({"role": "assistant", "content": r.content})

    assert roles("j").count("system") == 1


def test_a_changed_system_prompt_is_recorded_again(tmp_path):
    """Prompt refresh is legitimate data — the step builder handles it on read."""
    start(tmp_path)
    from odyssey.integrations.anthropic import Anthropic

    client = Anthropic()
    with odyssey.journey(id="j"):
        msgs = [{"role": "user", "content": "q"}]
        client.messages.create(model="m", system="first", messages=msgs)
        msgs.append({"role": "assistant", "content": "a"})
        msgs.append({"role": "user", "content": "q2"})
        client.messages.create(model="m", system="second", messages=msgs)

    systems = [
        e.message.content
        for e in events("j")
        if e.kind == "message" and e.message and e.message.role == "system"
    ]
    assert systems == ["first", "second"]


def test_tool_definitions_are_recorded_once_not_on_every_turn(tmp_path):
    start(tmp_path)
    from odyssey.integrations.anthropic import Anthropic

    client = Anthropic()
    with odyssey.journey(id="j"):
        msgs = []
        for i in range(3):
            msgs.append({"role": "user", "content": f"q{i}"})
            r = client.messages.create(model="m", messages=msgs, tools=TOOLS)
            msgs.append({"role": "assistant", "content": r.content})

    carrying = [
        e.seq
        for e in events("j")
        if e.kind == "message" and e.message and e.message.tool_definitions
    ]
    assert len(carrying) == 1


def test_a_rebuilt_message_list_resyncs_instead_of_duplicating(tmp_path):
    """Duplicated turns are silent corruption; a skipped one is only a hole."""
    client_obj = start(tmp_path)
    from odyssey.integrations.anthropic import Anthropic

    client = Anthropic()
    with odyssey.journey(id="j"):
        long = [{"role": "user", "content": f"q{i}"} for i in range(4)]
        client.messages.create(model="m", messages=long)
        # The app throws its history away and starts over.
        client.messages.create(model="m", messages=[{"role": "user", "content": "new"}])

    contents = [
        e.message.content
        for e in events("j")
        if e.kind == "message" and e.message and e.message.role == "user"
    ]
    assert contents == ["q0", "q1", "q2", "q3"]
    assert client_obj.stats.capture_errors == 1
    assert "shrank" in client_obj.stats.recent_errors[0]


# --------------------------------------------------------------------------
# Content blocks
# --------------------------------------------------------------------------


def test_a_tool_use_block_becomes_a_tool_call(tmp_path):
    start(tmp_path)
    from odyssey.integrations.anthropic import Anthropic

    client = Anthropic(
        scripted=[
            FakeResponse(
                [
                    {
                        "type": "tool_use",
                        "id": "tc_1",
                        "name": "book",
                        "input": {"day": "tue"},
                    }
                ],
                stop="tool_use",
            )
        ]
    )
    with odyssey.journey(id="j"):
        client.messages.create(model="m", messages=[])

    call = [
        e.message.tool_calls[0]
        for e in events("j")
        if e.kind == "message" and e.message and e.message.tool_calls
    ][0]
    assert (call.id, call.name, call.arguments) == ("tc_1", "book", {"day": "tue"})


def test_a_thinking_block_lands_on_reasoning_not_content(tmp_path):
    """Reasoning is kept for provenance but is not the turn to train on."""
    start(tmp_path)
    from odyssey.integrations.anthropic import Anthropic

    client = Anthropic(
        scripted=[
            FakeResponse(
                [
                    {"type": "thinking", "thinking": "internal chain"},
                    {"type": "text", "text": "Booked."},
                ]
            )
        ]
    )
    with odyssey.journey(id="j"):
        client.messages.create(model="m", messages=[])

    msg = [e.message for e in events("j") if e.kind == "message" and e.message][0]
    assert msg.content == "Booked."
    assert msg.reasoning == "internal chain"


def test_an_unknown_block_type_is_reported_not_fatal(tmp_path):
    """A new provider block must not delete the turn it arrived in.

    The batch parser refuses unknown blocks on purpose; on an auto-capture path
    that would lose real data, so unknown types are named in metadata instead.
    """
    client_obj = start(tmp_path)
    from odyssey.integrations.anthropic import Anthropic

    client = Anthropic(
        scripted=[
            FakeResponse(
                [
                    {"type": "text", "text": "Done."},
                    {"type": "server_tool_use_v9", "payload": {"x": 1}},
                ]
            )
        ]
    )
    with odyssey.journey(id="j"):
        client.messages.create(model="m", messages=[])

    recorded = [e for e in events("j") if e.kind == "message"]
    assert len(recorded) == 1
    assert recorded[0].message is not None
    assert recorded[0].message.content == "Done."
    assert (recorded[0].metadata or {})["unknown_blocks"] == ["server_tool_use_v9"]
    assert client_obj.stats.capture_errors == 0


def test_usage_and_stop_reason_survive(tmp_path):
    start(tmp_path)
    from odyssey.integrations.anthropic import Anthropic

    client = Anthropic(
        scripted=[FakeResponse([{"type": "text", "text": "hi"}], stop="max_tokens")]
    )
    with odyssey.journey(id="j"):
        client.messages.create(model="m", messages=[])

    msg = [e.message for e in events("j") if e.kind == "message" and e.message][0]
    assert msg.usage == {"input_tokens": 11, "output_tokens": 7}
    assert msg.finish_reason == "max_tokens"


def test_sampling_parameters_ride_along_in_metadata(tmp_path):
    """The schema has no field for them, and a corpus entry should be reproducible."""
    start(tmp_path)
    from odyssey.integrations.anthropic import Anthropic

    client = Anthropic()
    with odyssey.journey(id="j"):
        client.messages.create(
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
    from odyssey.integrations.anthropic import Anthropic

    client = Anthropic(
        scripted=[FakeResponse([{"type": "text", "text": "hi"}], model="claude-opus-5")]
    )
    with odyssey.journey(id="j"):
        client.messages.create(
            model="claude-opus-5", messages=[{"role": "user", "content": "q"}]
        )

    result = odyssey.fold(events("j"), data_source="anthropic")
    assert result.model_ids == ["claude-opus-5"]


# --------------------------------------------------------------------------
# Journey scoping
# --------------------------------------------------------------------------


def test_a_call_outside_a_journey_still_gets_recorded(tmp_path):
    """The wrapper opens its own journey, so a single call is not lost."""
    start(tmp_path)
    from odyssey.integrations.anthropic import Anthropic

    client = Anthropic()
    client.messages.create(model="m", messages=[{"role": "user", "content": "q"}])

    inner = odyssey.get_client()
    assert inner is not None
    ids = inner.spool.journey_ids()
    assert len(ids) == 1
    assert odyssey.fold(events(ids[0]), data_source="anthropic").trainable


def test_calls_inside_a_journey_all_join_it(tmp_path):
    start(tmp_path)
    from odyssey.integrations.anthropic import Anthropic

    client = Anthropic()
    with odyssey.journey(id="one-call"):
        msgs = []
        for i in range(2):
            msgs.append({"role": "user", "content": f"q{i}"})
            r = client.messages.create(model="m", messages=msgs)
            msgs.append({"role": "assistant", "content": r.content})

    inner = odyssey.get_client()
    assert inner is not None
    assert inner.spool.journey_ids() == ["one-call"]


def test_the_async_client_records_too(tmp_path):
    start(tmp_path)
    from odyssey.integrations.anthropic import AsyncAnthropic

    client = AsyncAnthropic()

    async def main():
        with odyssey.journey(id="j"):
            return await client.messages.create(
                model="m", messages=[{"role": "user", "content": "q"}]
            )

    asyncio.run(main())
    assert roles("j") == ["user", "assistant"]


# --------------------------------------------------------------------------
# Streaming: the final message, not chunks
# --------------------------------------------------------------------------


def test_the_sync_stream_captures_the_final_message_not_chunks(tmp_path):
    start(tmp_path)
    from odyssey.integrations.anthropic import Anthropic

    expected = FakeResponse([{"type": "text", "text": "hi"}])
    client = Anthropic(scripted=[expected])
    with client.messages.stream(
        model="m", messages=[{"role": "user", "content": "q"}]
    ) as stream:
        message = stream.get_final_message()

    assert message is expected
    assert roles(_only_journey_id()) == ["user", "assistant"]


def test_the_async_stream_captures_the_final_message_not_chunks(tmp_path):
    start(tmp_path)
    from odyssey.integrations.anthropic import AsyncAnthropic

    expected = FakeResponse([{"type": "text", "text": "hi"}])
    client = AsyncAnthropic(scripted=[expected])

    async def main():
        async with client.messages.stream(
            model="m", messages=[{"role": "user", "content": "q"}]
        ) as stream:
            return await stream.get_final_message()

    message = asyncio.run(main())
    assert message is expected
    assert roles(_only_journey_id()) == ["user", "assistant"]


def _only_journey_id() -> str:
    """The stream proxy generates its own journey id — read it back off disk."""
    client = odyssey.get_client()
    assert client is not None
    ids = client.spool.journey_ids()
    assert len(ids) == 1
    return ids[0]


# --------------------------------------------------------------------------
# Opt-in patching
# --------------------------------------------------------------------------


def make_patch_target():
    """A stand-in for anthropic.resources.messages, with a Messages.create."""
    module = types.ModuleType("fake_target")

    class Messages:
        def __init__(self):
            self.seen = []

        def create(self, **kwargs):
            self.seen.append(kwargs)
            return FakeResponse([{"type": "text", "text": "patched"}])

    module.Messages = Messages  # type: ignore[attr-defined]
    return module


def test_patching_records_without_touching_the_call_site(tmp_path):
    start(tmp_path)
    from odyssey.integrations.anthropic import instrument, is_instrumented

    target = make_patch_target()
    instrument(target)
    assert is_instrumented()

    plain = target.Messages()  # an unwrapped, pre-existing client
    with odyssey.journey(id="j"):
        result = plain.create(model="m", messages=[{"role": "user", "content": "q"}])

    assert result.content == [{"type": "text", "text": "patched"}]
    assert roles("j") == ["user", "assistant"]


def test_patching_is_idempotent(tmp_path):
    start(tmp_path)
    from odyssey.integrations.anthropic import instrument

    target = make_patch_target()
    instrument(target)
    first = target.Messages.create
    instrument(target)
    assert target.Messages.create is first


def test_uninstrument_restores_the_original(tmp_path):
    start(tmp_path)
    from odyssey.integrations.anthropic import instrument, is_instrumented, uninstrument

    target = make_patch_target()
    original = target.Messages.create
    instrument(target)
    assert target.Messages.create is not original
    uninstrument()
    assert target.Messages.create is original
    assert not is_instrumented()


def test_patching_an_unsupported_sdk_shape_raises_for_its_caller(tmp_path):
    """instrument() is explicit about a provider refactor it cannot handle.

    Raising is right here: a direct caller asked for patching and deserves to
    know it did not happen. init() is the one that must not die — see the next
    test, where the same failure becomes a counter.
    """
    start(tmp_path)
    from odyssey.integrations.anthropic import instrument

    empty = types.ModuleType("no_messages")
    with pytest.raises(AttributeError, match="not supported"):
        instrument(empty)


def test_init_never_dies_because_instrumentation_failed(tmp_path, monkeypatch):
    monkeypatch.delitem(sys.modules, "anthropic", raising=False)
    monkeypatch.setitem(
        sys.modules, "anthropic.resources.messages", types.ModuleType("broken")
    )
    client = start(tmp_path, instrument=["anthropic"])
    assert client.stats.capture_errors == 1
