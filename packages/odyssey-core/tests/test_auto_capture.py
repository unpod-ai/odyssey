"""Auto-capture across providers: streams, provider names, voice-call linking.

What ``instrument="auto"`` has to get right in a real voice deployment: every
LLM call is async and streamed, most providers are OpenAI-compatible hosts
behind the ``openai`` SDK, and the calls happen inside a LiveKit or Pipecat
session that already records the conversation. No provider SDK is installed;
each test patches a fake module shaped like the real one.
"""

from __future__ import annotations

import asyncio
import types
import uuid
from typing import Any, Dict, List

import pytest

import odyssey
from odyssey.integrations import providers
from odyssey.primitives import JourneyEvent, JourneyHeader, Message

RAW = "X-Stainless-Raw-Response"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("ODYSSEY_PROVIDER_HOSTS", raising=False)
    odyssey.shutdown()
    yield
    from odyssey.integrations import anthropic, gemini, openai

    for module in (openai, gemini, anthropic):
        module.uninstrument()
    for name in ("acme", "broken"):
        providers.unregister_provider(name)
    odyssey.shutdown()


def start(tmp_path, **kw):
    kw.setdefault("instrument", "none")
    return odyssey.init(
        spool_dir=tmp_path / "spool",
        out_dir=tmp_path / "out",
        drain_interval=None,
        **kw,
    )


def client() -> Any:
    c = odyssey.get_client()
    assert c is not None
    return c


def events(jid: str) -> List[JourneyEvent]:
    return client().spool.read(jid)


def turns(jid: str) -> List[JourneyEvent]:
    return [e for e in events(jid) if e.kind == "message" and e.message is not None]


def msg(event: JourneyEvent) -> Message:
    assert event.message is not None
    return event.message


def meta(event: JourneyEvent) -> Dict[str, Any]:
    return event.metadata or {}


def roles(jid: str) -> List[str]:
    return [msg(e).role for e in turns(jid)]


def reply(jid: str) -> JourneyEvent:
    replies = [e for e in turns(jid) if msg(e).role == "assistant"]
    assert replies, f"no assistant turn in {jid}"
    return replies[-1]


def header(jid: str) -> JourneyHeader:
    head = client().spool.header(jid)
    assert head is not None
    return head


def tags(jid: str) -> Dict[str, Any]:
    return header(jid).journey_metadata or {}


def journey_ids() -> List[str]:
    return sorted(client().spool.journey_ids())


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class _SDKClient:
    def __init__(self, base_url):
        self.base_url = base_url


class AsyncChunks:
    """Shaped like ``openai.AsyncStream``: async iterator and context manager."""

    def __init__(self, items, *, cancel_after=None):
        self._items = list(items)
        self._cancel_after = cancel_after
        self.closed = False

    async def _gen(self):
        for i, item in enumerate(self._items):
            if self._cancel_after is not None and i == self._cancel_after:
                raise asyncio.CancelledError()
            yield item

    def __aiter__(self):
        return self._gen()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True


class Raw:
    """Shaped like a Stainless raw response."""

    def __init__(self, parsed):
        self._parsed = parsed
        self.headers = {"x-request-id": "req_1"}

    def parse(self):
        return self._parsed


def completion(content: str = "ok", **extra: Any) -> Dict[str, Any]:
    return {
        "id": "cmpl-1",
        "model": "served-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content, **extra},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }


def chunk(
    content: Any = None,
    *,
    finish: Any = None,
    usage: Any = None,
    tool_calls: Any = None,
) -> Dict[str, Any]:
    delta: Dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    only_usage = usage is not None and not delta and finish is None
    choices = (
        [] if only_usage else [{"index": 0, "delta": delta, "finish_reason": finish}]
    )
    return {"id": "cmpl-s", "model": "served-model", "choices": choices, "usage": usage}


def openai_target(script: List[Any]) -> Any:
    """A stand-in for ``openai.resources.chat.completions``."""
    module = types.ModuleType("fake_openai_completions")

    def next_item(kwargs: Dict[str, Any]) -> Any:
        item: Any = script.pop(0) if script else completion()
        if (kwargs.get("extra_headers") or {}).get(RAW) == "true":
            return Raw(item)
        return item

    class Completions:
        def __init__(self, base_url="https://api.openai.com/v1"):
            self._client = _SDKClient(base_url)

        def create(self, **kwargs):
            item = next_item(kwargs)
            return iter(item) if kwargs.get("stream") else item

    class AsyncCompletions(Completions):
        async def create(self, **kwargs):  # type: ignore[override]
            item = next_item(kwargs)
            if kwargs.get("stream") and not isinstance(item, AsyncChunks):
                return AsyncChunks(item)
            return item

    module.Completions = Completions  # type: ignore[attr-defined]
    module.AsyncCompletions = AsyncCompletions  # type: ignore[attr-defined]
    return module


def instrument_openai(target: Any) -> Any:
    from odyssey.integrations.openai import instrument

    instrument(target)
    return target


def patch_openai(script: Any = None) -> Any:
    return instrument_openai(openai_target(list(script or [])))


USER = [{"role": "user", "content": "what are your hours?"}]


# --------------------------------------------------------------------------
# Streams
# --------------------------------------------------------------------------


def test_an_async_stream_is_recorded_as_one_turn_once_drained(tmp_path):
    """LiveKit's and Pipecat's exact call shape."""
    start(tmp_path)
    usage = {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}
    target = patch_openai(
        [[chunk("9 to "), chunk("5"), chunk(finish="stop"), chunk(usage=usage)]]
    )

    async def main():
        with odyssey.journey(id="j"):
            stream = await target.AsyncCompletions().create(
                model="gpt-4.1-mini",
                messages=USER,
                stream=True,
                stream_options={"include_usage": True},
            )
            assert roles("j") == ["user"], "nothing to record until it is drained"
            async with stream:
                return [c async for c in stream]

    assert len(asyncio.run(main())) == 4
    assert roles("j") == ["user", "assistant"]
    got = reply("j")
    assert msg(got).content == "9 to 5"
    assert msg(got).finish_reason == "stop"
    assert msg(got).usage == usage
    assert msg(got).ttft_ms is not None and msg(got).latency_ms is not None
    assert meta(got)["streamed"] is True and "incomplete" not in meta(got)
    assert got.model_id == "served-model"


def test_a_sync_stream_is_recorded_too(tmp_path):
    start(tmp_path)
    target = patch_openai([[chunk("Hel"), chunk("lo"), chunk(finish="stop")]])
    with odyssey.journey(id="j"):
        list(target.Completions().create(model="m", messages=USER, stream=True))
    assert msg(reply("j")).content == "Hello"


def test_tool_call_deltas_are_assembled_into_one_call(tmp_path):
    start(tmp_path)
    opening = {
        "index": 0,
        "id": "call_1",
        "type": "function",
        "function": {"name": "book", "arguments": ""},
    }
    target = patch_openai(
        [
            [
                chunk(tool_calls=[opening]),
                chunk(tool_calls=[{"index": 0, "function": {"arguments": '{"day":'}}]),
                chunk(tool_calls=[{"index": 0, "function": {"arguments": ' "tue"}'}}]),
                chunk(finish="tool_calls"),
            ]
        ]
    )
    with odyssey.journey(id="j"):
        list(target.Completions().create(model="m", messages=USER, stream=True))

    calls = msg(reply("j")).tool_calls or []
    assert [(c.name, c.arguments, c.id) for c in calls] == [
        ("book", {"day": "tue"}, "call_1")
    ]


def test_a_cancelled_stream_keeps_what_arrived(tmp_path):
    """A voice barge-in cancels the reply mid-stream."""
    start(tmp_path)
    items = AsyncChunks(
        [chunk("Sure, "), chunk("I can"), chunk(" book")], cancel_after=2
    )
    target = patch_openai([items])

    async def main():
        with odyssey.journey(id="j"):
            stream = await target.AsyncCompletions().create(
                model="m", messages=USER, stream=True
            )
            with pytest.raises(asyncio.CancelledError):
                async for _ in stream:
                    pass

    asyncio.run(main())
    got = reply("j")
    assert msg(got).content == "Sure, I can"
    assert meta(got)["incomplete"] is True
    assert meta(got)["stream_error"] == "CancelledError"


def test_the_turn_is_what_the_provider_sent_not_what_a_consumer_rewrote(tmp_path):
    """LiveKit strips `<think>` tags out of `delta.content` after reading a chunk."""
    start(tmp_path)

    class Chunk:
        def __init__(self, text, finish=None):
            self.delta = types.SimpleNamespace(content=text)
            self.finish = finish

        def model_dump(self):
            return {
                "id": "c",
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": self.delta.content},
                        "finish_reason": self.finish,
                    }
                ],
            }

    target = patch_openai([[Chunk("<think>check</think>Tuesday"), Chunk("", "stop")]])
    with odyssey.journey(id="j"):
        for c in target.Completions().create(model="m", messages=USER, stream=True):
            c.delta.content = c.delta.content.replace("<think>check</think>", "")

    assert msg(reply("j")).content == "<think>check</think>Tuesday"


def test_a_stream_outside_any_journey_opens_and_closes_its_own(tmp_path):
    start(tmp_path)
    target = patch_openai([[chunk("hi"), chunk(finish="stop")]])
    list(target.Completions().create(model="m", messages=USER, stream=True))

    (jid,) = journey_ids()
    assert [e.kind for e in events(jid)][-1] == "terminal"
    assert odyssey.current() is None


# --------------------------------------------------------------------------
# Who served it
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "base_url, provider",
    [
        ("https://api.openai.com/v1", "openai"),
        ("https://api.groq.com/openai/v1", "groq"),
        ("https://api.x.ai/v1", "xai"),
        ("https://api.cerebras.ai/v1", "cerebras"),
        ("https://openrouter.ai/api/v1", "openrouter"),
        ("https://api.sarvam.ai/v1", "sarvam"),
        ("https://api.deepinfra.com/v1/openai", "deepinfra"),
        ("https://acme-eastus.openai.azure.com/openai", "azure"),
        ("http://localhost:11434/v1", "ollama"),
        ("https://unpod-llm--serve.modal.run/v1", "modal"),
        ("https://llm.internal.example.com/v1", "llm.internal.example.com"),
    ],
)
def test_the_provider_is_named_from_the_clients_base_url(tmp_path, base_url, provider):
    start(tmp_path)
    target = patch_openai()
    with odyssey.journey(id="j"):
        target.Completions(base_url).create(model="m", messages=USER)
    assert msg(reply("j")).provider == provider


def test_a_host_can_be_named_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ODYSSEY_PROVIDER_HOSTS", "llm.internal.example.com=inhouse")
    start(tmp_path)
    target = patch_openai()
    with odyssey.journey(id="j"):
        target.Completions("https://llm.internal.example.com/v1").create(
            model="m", messages=USER
        )
    assert msg(reply("j")).provider == "inhouse"


def _split_think(entry: Dict[str, Any], raw: Any) -> Dict[str, Any]:
    text = entry.get("content") or ""
    if "</think>" in text:
        thought, _, answer = text.partition("</think>")
        entry["reasoning"] = thought.replace("<think>", "").strip()
        entry["content"] = answer.strip()
    return entry


def test_a_registered_provider_can_adapt_what_is_recorded(tmp_path):
    start(tmp_path)
    providers.register_provider("acme", hosts=["api.acme.ai"], adapt=_split_think)
    target = patch_openai([completion("<think>check calendar</think> Tuesday works")])
    with odyssey.journey(id="j"):
        target.Completions("https://api.acme.ai/v1").create(model="m", messages=USER)

    got = msg(reply("j"))
    assert (got.provider, got.content, got.reasoning) == (
        "acme",
        "Tuesday works",
        "check calendar",
    )


def test_an_adapter_that_raises_costs_the_hook_not_the_turn(tmp_path):
    start(tmp_path)

    def broken(entry, raw):
        raise RuntimeError("bad hook")

    providers.register_provider("broken", hosts=["api.acme.ai"], adapt=broken)
    target = patch_openai([completion("still here")])
    with odyssey.journey(id="j"):
        target.Completions("https://api.acme.ai/v1").create(model="m", messages=USER)

    assert msg(reply("j")).content == "still here"
    assert client().stats.capture_errors >= 1


def test_reasoning_content_is_recorded_as_reasoning(tmp_path):
    """DeepSeek, Groq, Qwen and Sarvam spell it `reasoning_content`."""
    start(tmp_path)
    target = patch_openai([completion("Tuesday", reasoning_content="calendar is free")])
    with odyssey.journey(id="j"):
        target.Completions("https://api.deepseek.com/v1").create(
            model="m", messages=USER
        )
    assert msg(reply("j")).reasoning == "calendar is free"


# --------------------------------------------------------------------------
# Raw responses, frameworks, several clients
# --------------------------------------------------------------------------


def test_a_raw_response_is_recorded_once_parsed(tmp_path):
    """LangChain's `ChatOpenAI` calls `with_raw_response.create`."""
    start(tmp_path)
    target = patch_openai([completion("raw ok")])
    with odyssey.journey(id="j"):
        raw = target.Completions().create(
            model="m", messages=USER, extra_headers={RAW: "true"}
        )
        assert raw.headers == {"x-request-id": "req_1"}
        assert roles("j") == ["user"]
        first = raw.parse()
        assert raw.parse() is first

    assert roles("j") == ["user", "assistant"]
    assert msg(reply("j")).content == "raw ok"


class _Owner:
    """What a framework handler exposes: which of its runs are still going,
    and a hook for what the patch underneath measured."""

    def __init__(self) -> None:
        self.running = {"run_1"}
        self.measured: List[Dict[str, Any]] = []

    def is_running(self, run_id: str) -> bool:
        return run_id in self.running

    def observed(self, run_id: str, **fields: Any) -> None:
        self.measured.append({"run_id": run_id, **fields})


class _MuteOwner(_Owner):
    """A handler from an older release: marks its runs, takes no measurements."""

    observed = None  # type: ignore[assignment]


def test_a_call_a_framework_is_recording_is_left_to_it(tmp_path):
    """LangChain's handler records `ChatOpenAI`; the patch underneath must not."""
    from odyssey.integrations._reentry import enter_framework_call, exit_framework_call

    start(tmp_path)
    target = patch_openai()
    # Held for the run, as a handler is; a mark's owner is referenced weakly.
    owner = _Owner()
    token = enter_framework_call(owner, "run_1")
    try:
        target.Completions().create(model="m", messages=USER)
    finally:
        exit_framework_call(token)
    assert journey_ids() == []

    target.Completions().create(model="m", messages=USER)
    assert len(journey_ids()) == 1, "the mark does not outlive the model run"


def test_a_mark_whose_run_already_ended_does_not_silence_capture(tmp_path):
    """A mark can be left in a context that never saw its run end. That must not
    switch provider capture off for the rest of that task."""
    from odyssey.integrations._reentry import enter_framework_call, exit_framework_call

    start(tmp_path)
    target = patch_openai()
    owner = _Owner()
    token = enter_framework_call(owner, "run_1")
    owner.running.clear()
    try:
        target.Completions().create(model="m", messages=USER)
    finally:
        exit_framework_call(token)
    assert len(journey_ids()) == 1


def test_a_framework_recorded_call_still_reports_provider_and_latency(tmp_path):
    """The handler writes the turn, but only the patch below sees who served
    it. Without this the LangChain turns carry no provider and no timing."""
    from odyssey.integrations._reentry import enter_framework_call, exit_framework_call

    start(tmp_path)
    target = patch_openai()
    owner = _Owner()
    token = enter_framework_call(owner, "run_1")
    try:
        target.Completions("https://api.groq.com/openai/v1").create(
            model="m", messages=USER
        )
    finally:
        exit_framework_call(token)

    assert journey_ids() == [], "still no turn of our own"
    assert len(owner.measured) == 1
    got = owner.measured[0]
    assert got["run_id"] == "run_1"
    assert got["provider"] == "groq"
    assert got["latency_ms"] is not None
    assert got["ttft_ms"] is None, "nothing was streamed"


def test_a_framework_recorded_stream_reports_its_time_to_first_token(tmp_path):
    from odyssey.integrations._reentry import enter_framework_call, exit_framework_call

    start(tmp_path)
    target = patch_openai([[chunk("Hel"), chunk("lo"), chunk(finish="stop")]])
    owner = _Owner()
    token = enter_framework_call(owner, "run_1")
    try:
        stream = target.Completions().create(model="m", messages=USER, stream=True)
        assert owner.measured == [], "nothing to report until it is drained"
        assert list(stream) != []
    finally:
        exit_framework_call(token)

    assert journey_ids() == []
    got = owner.measured[0]
    assert got["provider"] == "openai"
    assert got["ttft_ms"] is not None and got["latency_ms"] is not None


def test_a_framework_recorded_call_that_fails_reports_nothing(tmp_path):
    """The handler's own `on_llm_error` ends that run; there is no turn to stamp."""
    from odyssey.integrations._reentry import enter_framework_call, exit_framework_call

    start(tmp_path)

    class Boom(Exception):
        pass

    def explode(self, **kwargs):
        raise Boom("provider down")

    # Patched over the failing call, not the other way round.
    target = openai_target([])
    target.Completions.create = explode
    instrument_openai(target)
    owner = _Owner()
    token = enter_framework_call(owner, "run_1")
    try:
        with pytest.raises(Boom):
            target.Completions().create(model="m", messages=USER)
    finally:
        exit_framework_call(token)

    assert owner.measured == [] and journey_ids() == []


def test_a_handler_with_nothing_to_report_to_still_works(tmp_path):
    from odyssey.integrations._reentry import enter_framework_call, exit_framework_call

    start(tmp_path)
    target = patch_openai([completion("fine")])
    owner = _MuteOwner()
    token = enter_framework_call(owner, "run_1")
    try:
        result = target.Completions().create(model="m", messages=USER)
    finally:
        exit_framework_call(token)

    assert result["choices"][0]["message"]["content"] == "fine"
    assert journey_ids() == []


def test_two_clients_in_one_journey_each_keep_their_own_history(tmp_path):
    """A voice call's main LLM and a filler model share the `.llm` journey."""
    start(tmp_path)
    target = patch_openai([completion("a1"), completion("filler"), completion("a2")])
    main = target.Completions("https://api.groq.com/openai/v1")
    filler = target.Completions("https://api.openai.com/v1")
    system = {"role": "system", "content": "you book slots"}
    with odyssey.journey(id="j"):
        main.create(model="m", messages=[system, USER[0]])
        filler.create(model="f", messages=[{"role": "user", "content": "say hmm"}])
        main.create(
            model="m",
            messages=[
                system,
                USER[0],
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "tuesday?"},
            ],
        )

    requests = [
        (msg(e).role, msg(e).content)
        for e in turns("j")
        if meta(e).get("direction") == "request"
    ]
    assert requests == [
        ("system", "you book slots"),
        ("user", "what are your hours?"),
        ("user", "say hmm"),
        ("user", "tuesday?"),
    ]
    assert client().stats.capture_errors == 0


# --------------------------------------------------------------------------
# Voice calls: the linked `.llm` journey
# --------------------------------------------------------------------------


class Session:
    def __init__(self):
        self.handlers: Dict[str, list] = {}
        self.current_agent = None

    def on(self, name, fn):
        self.handlers.setdefault(name, []).append(fn)

    def off(self, name, fn):
        self.handlers.get(name, []).remove(fn)


def _llm_turn(target: Any, base_url: str = "https://api.groq.com/openai/v1") -> Any:
    async def run():
        stream = await target.AsyncCompletions(base_url).create(
            model="llama", messages=USER, stream=True
        )
        async with stream:
            async for _ in stream:
                pass

    return run


def test_provider_calls_in_a_voice_call_land_in_its_llm_journey(tmp_path):
    from odyssey.integrations.livekit import attach

    start(tmp_path)
    target = patch_openai([[chunk("9 to 5"), chunk(finish="stop")]])
    rec = attach(Session(), journey_id="call_9", handler="LiteV2Handler", agent_id="a7")
    assert odyssey.current() is None, "the app's own ambient journey is untouched"

    # The session's own task: created after attach, so it inherits the link.
    asyncio.run(_llm_turn(target)())
    rec.close()

    assert journey_ids() == ["call_9", "call_9.llm"]
    assert tags("call_9.llm")["parent_journey_id"] == "call_9"
    assert (tags("call_9.llm")["handler"], tags("call_9.llm")["agent_id"]) == (
        "LiteV2Handler",
        "a7",
    )
    assert roles("call_9.llm") == ["user", "assistant"]
    assert msg(reply("call_9.llm")).provider == "groq"
    assert [e.kind for e in events("call_9.llm")][-1] == "terminal"
    assert turns("call_9") == [], "the spoken conversation is not duplicated"


def test_a_call_with_no_provider_calls_leaves_no_llm_journey(tmp_path):
    from odyssey.integrations.livekit import attach

    start(tmp_path)
    attach(Session(), journey_id="call_quiet").close()
    assert "call_quiet.llm" not in journey_ids()


def test_a_provider_call_after_the_call_ended_opens_its_own_journey(tmp_path):
    from odyssey.integrations.livekit import attach

    start(tmp_path)
    target = patch_openai()
    attach(Session(), journey_id="call_done").close()
    target.Completions().create(model="m", messages=USER)
    assert "call_done.llm" not in journey_ids()
    assert [j for j in journey_ids() if not j.startswith("call_done")]


def test_linking_can_be_turned_off(tmp_path):
    from odyssey.integrations.livekit import attach

    start(tmp_path)
    target = patch_openai()
    rec = attach(Session(), journey_id="call_off", record_provider_calls=False)
    target.Completions().create(model="m", messages=USER)
    rec.close()
    assert "call_off.llm" not in journey_ids()


def test_an_explicit_journey_still_wins_over_the_link(tmp_path):
    from odyssey.integrations.livekit import attach

    start(tmp_path)
    target = patch_openai()
    rec = attach(Session(), journey_id="call_x")
    with odyssey.journey(id="app_scope"):
        target.Completions().create(model="m", messages=USER)
    rec.close()
    assert "app_scope" in journey_ids() and "call_x.llm" not in journey_ids()


def test_a_langchain_run_inside_a_voice_call_joins_its_llm_journey(tmp_path):
    from odyssey.integrations.langchain import _Recorder
    from odyssey.integrations.livekit import attach

    class Gen:
        def __init__(self, text):
            self.text, self.message = text, None

    class Result:
        def __init__(self, text):
            self.generations = [[Gen(text)]]

    start(tmp_path)
    rec = attach(Session(), journey_id="call_lc")
    lc = _Recorder(data_source="langchain", metadata=None)
    run_id = uuid.uuid4()
    lc.on_llm_start({}, ["classify intent"], run_id=run_id)
    lc.on_llm_end(Result("booking"), run_id=run_id)
    rec.close()

    assert str(run_id) not in journey_ids()
    assert [msg(e).content for e in turns("call_lc.llm")] == [
        "classify intent",
        "booking",
    ]


def test_pipecat_links_provider_calls_too(tmp_path):
    from odyssey.integrations.pipecat import attach

    class Task:
        def __init__(self):
            self.observers: list = []

        def add_observer(self, obs):
            self.observers.append(obs)

    start(tmp_path)
    target = patch_openai()
    rec = attach(Task(), journey_id="pc_1")
    target.Completions("https://api.cerebras.ai/v1").create(model="m", messages=USER)
    rec.close()

    assert msg(reply("pc_1.llm")).provider == "cerebras"
    assert tags("pc_1.llm")["parent_journey_id"] == "pc_1"


# --------------------------------------------------------------------------
# Gemini and Anthropic streams
# --------------------------------------------------------------------------


def gchunk(
    text: Any = None, *, finish: Any = None, usage: Any = None
) -> Dict[str, Any]:
    parts = [{"text": text}] if text else []
    candidate: Dict[str, Any] = {"content": {"role": "model", "parts": parts}}
    if finish:
        candidate["finish_reason"] = finish
    d: Dict[str, Any] = {
        "candidates": [candidate],
        "model_version": "gemini-2.5-flash",
        "response_id": "r1",
    }
    if usage:
        d["usage_metadata"] = usage
    return d


def gemini_target(chunks: List[Any]) -> Any:
    module = types.ModuleType("fake_genai_models")

    class Models:
        def __init__(self, vertex=False):
            self._api_client = types.SimpleNamespace(vertexai=vertex)

        def generate_content(self, **kwargs):
            return gchunk("hi", finish="STOP")

        def generate_content_stream(self, **kwargs):
            yield from chunks

    class AsyncModels(Models):
        async def generate_content(self, **kwargs):  # type: ignore[override]
            return gchunk("hi", finish="STOP")

        async def generate_content_stream(self, **kwargs):  # type: ignore[override]
            async def gen():
                for c in chunks:
                    yield c

            return gen()

    module.Models = Models  # type: ignore[attr-defined]
    module.AsyncModels = AsyncModels  # type: ignore[attr-defined]
    return module


CONTENTS = [{"role": "user", "parts": [{"text": "hours?"}]}]


def test_a_gemini_async_stream_is_recorded_as_one_turn(tmp_path):
    """LiveKit's Google plugin: `await aio.models.generate_content_stream(...)`."""
    from odyssey.integrations.gemini import instrument

    start(tmp_path)
    usage = {"prompt_token_count": 5, "candidates_token_count": 3}
    target = gemini_target(
        [gchunk("Hello "), gchunk("there"), gchunk(finish="STOP", usage=usage)]
    )
    instrument(target)

    async def main():
        with odyssey.journey(id="j"):
            stream = await target.AsyncModels(vertex=True).generate_content_stream(
                model="gemini-2.5-flash", contents=CONTENTS
            )
            async for _ in stream:
                pass

    asyncio.run(main())
    got = reply("j")
    assert msg(got).content == "Hello there"
    assert msg(got).provider == "vertex"
    assert msg(got).finish_reason == "STOP"
    assert meta(got)["streamed"] is True


def test_a_gemini_sync_stream_is_recorded_too(tmp_path):
    from odyssey.integrations.gemini import instrument

    start(tmp_path)
    target = gemini_target([gchunk("a"), gchunk("b", finish="STOP")])
    instrument(target)
    with odyssey.journey(id="j"):
        list(target.Models().generate_content_stream(model="g", contents=CONTENTS))
    assert msg(reply("j")).content == "ab"
    assert msg(reply("j")).provider == "gemini"


ANTHROPIC_EVENTS: List[Dict[str, Any]] = [
    {
        "type": "message_start",
        "message": {
            "id": "msg_s",
            "model": "claude-sonnet",
            "role": "assistant",
            "usage": {"input_tokens": 9},
        },
    },
    {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "text", "text": ""},
    },
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": "Sure, "},
    },
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": "booking."},
    },
    {"type": "content_block_stop", "index": 0},
    {
        "type": "content_block_start",
        "index": 1,
        "content_block": {
            "type": "tool_use",
            "id": "tu_1",
            "name": "book",
            "input": {},
        },
    },
    {
        "type": "content_block_delta",
        "index": 1,
        "delta": {"type": "input_json_delta", "partial_json": '{"day": '},
    },
    {
        "type": "content_block_delta",
        "index": 1,
        "delta": {"type": "input_json_delta", "partial_json": '"tue"}'},
    },
    {
        "type": "message_delta",
        "delta": {"stop_reason": "tool_use"},
        "usage": {"output_tokens": 12},
    },
    {"type": "message_stop"},
]


def test_an_anthropic_beta_async_stream_is_recorded_as_one_turn(tmp_path):
    """Pipecat's Anthropic service: `await beta.messages.create(stream=True)`."""
    from odyssey.integrations.anthropic import instrument

    start(tmp_path)

    messages_module = types.ModuleType("fake_anthropic_messages")

    class Messages:
        def create(self, **kwargs):
            raise AssertionError("not used")

    messages_module.Messages = Messages  # type: ignore[attr-defined]

    beta = types.ModuleType("fake_anthropic_beta_messages")

    class AsyncMessages:
        def __init__(self):
            self._client = _SDKClient("https://api.anthropic.com")

        async def create(self, **kwargs):
            return AsyncChunks(ANTHROPIC_EVENTS)

    beta.AsyncMessages = AsyncMessages  # type: ignore[attr-defined]
    instrument(messages_module, beta_target=beta)

    async def main():
        with odyssey.journey(id="j"):
            stream = await AsyncMessages().create(
                model="claude-sonnet",
                system="you book slots",
                messages=[{"role": "user", "content": "book tuesday"}],
                stream=True,
            )
            async for _ in stream:
                pass

    asyncio.run(main())
    got = reply("j")
    assert msg(got).content == "Sure, booking."
    calls = msg(got).tool_calls or []
    assert [(c.name, c.arguments) for c in calls] == [("book", {"day": "tue"})]
    assert msg(got).provider == "anthropic"
    assert got.model_id == "claude-sonnet"
    assert meta(got)["streamed"] is True
