"""The single integration point: init(), journey(), observe(), health().

The property this file cares about most is the one that has no happy path:
**capture never raises.** An observability layer that takes down the application
it observes is worse than no observability layer.
"""

from __future__ import annotations

import asyncio
import warnings

import pytest

import odyssey
import odyssey.client as client_mod
from odyssey.context import current
from odyssey.primitives import Message, ToolCall, ToolResponse


@pytest.fixture(autouse=True)
def clean_singleton():
    """init() owns process-wide state; no test may leak it into the next."""
    odyssey.shutdown()
    yield
    odyssey.shutdown()


def start(tmp_path, **kw):
    return odyssey.init(
        spool_dir=tmp_path / "spool",
        out_dir=tmp_path / "out",
        drain_interval=None,  # no background thread in tests
        **kw,
    )


def events(jid: str = "j"):
    client = odyssey.get_client()
    assert client is not None
    return client.spool.read(jid)


# --------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------


def test_init_returns_a_client_and_get_client_finds_it(tmp_path):
    client = start(tmp_path)
    assert odyssey.get_client() is client
    assert len(client.writer_id) == 12


def test_second_init_warns_and_reuses(tmp_path):
    """Two clients would mean two drainers and two seq allocators."""
    first = start(tmp_path)
    with pytest.warns(RuntimeWarning, match="more than once"):
        second = start(tmp_path)
    assert second is first


def test_force_replaces_the_client(tmp_path):
    first = start(tmp_path)
    second = start(tmp_path, force=True)
    assert second is not first
    assert odyssey.get_client() is second


def test_explicit_arguments_beat_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ODYSSEY_SPOOL", str(tmp_path / "from-env"))
    client = start(tmp_path)
    assert client.config.spool_dir == tmp_path / "spool"


def test_environment_is_used_when_nothing_is_passed(tmp_path, monkeypatch):
    monkeypatch.setenv("ODYSSEY_SPOOL", str(tmp_path / "from-env"))
    monkeypatch.setenv("ODYSSEY_OUT", str(tmp_path / "out-env"))
    client = odyssey.init(drain_interval=None)
    assert client.config.spool_dir == tmp_path / "from-env"
    assert client.config.out_dir == tmp_path / "out-env"


def test_a_broken_interval_in_the_environment_does_not_fail_startup(monkeypatch):
    monkeypatch.setenv("ODYSSEY_DRAIN_INTERVAL", "not-a-number")
    from odyssey.config import DEFAULT_DRAIN_INTERVAL, resolve

    assert resolve(drain_interval_set=False).drain_interval == DEFAULT_DRAIN_INTERVAL


def test_unknown_instrumentation_target_is_counted_not_raised(tmp_path):
    client = start(tmp_path, instrument=["nosuchprovider"])
    assert client.stats.capture_errors == 1
    assert "nosuchprovider" in client.stats.recent_errors[0]


# --------------------------------------------------------------------------
# Not initialised / disabled
# --------------------------------------------------------------------------


def test_recording_without_init_warns_once_and_records_nothing(tmp_path):
    """A forgotten init() must not break the app — only stop the recording."""
    import odyssey.client as client_mod

    client_mod._warned_uninitialised = False
    with pytest.warns(RuntimeWarning, match="init"):
        with odyssey.journey(id="j") as j:
            assert j.message(Message(role="user", content="hi")) is None
    # The second use is silent: one warning per process, not one per call.
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with odyssey.journey(id="j2") as j:
            assert j.message(Message(role="user", content="hi")) is None


def test_disabled_client_records_nothing(tmp_path):
    start(tmp_path, enabled=False)
    with odyssey.journey(id="j") as j:
        assert j.message(Message(role="user", content="hi")) is None
    assert events("j") == []


def test_health_reports_uninitialised():
    assert odyssey.health() == {"initialised": False, "enabled": False}


# --------------------------------------------------------------------------
# journey()
# --------------------------------------------------------------------------


def test_seq_is_allocated_without_the_caller_ever_naming_one(tmp_path):
    """The reason this layer exists."""
    start(tmp_path)
    with odyssey.journey(id="j") as j:
        for i in range(4):
            j.message(Message(role="user", content=f"m{i}"))
    assert [e.seq for e in events("j")] == [0, 1, 2, 3, 4]  # +1 terminal


def test_journey_id_defaults_to_a_generated_one(tmp_path):
    start(tmp_path)
    with odyssey.journey() as j:
        assert len(j.id) == 32
        j.message(Message(role="user", content="hi"))
    assert len(events(j.id)) == 2


def test_every_event_carries_the_writer_id(tmp_path):
    """The one tag that must NOT be hoisted into the header.

    A header is written once per shard by whoever opened it, so a second process
    appending to that shard would inherit the first one's identity and the fold
    would see one writer where there are two. Per-event is the only place the
    collision is provable.
    """
    client = start(tmp_path)
    with odyssey.journey(id="j") as j:
        j.message(Message(role="user", content="hi"))
    assert all(
        (e.metadata or {}).get(odyssey.WRITER_META_KEY) == client.writer_id
        for e in events("j")
    )


def test_caller_metadata_is_stated_once_in_the_header(tmp_path):
    """Journey-level tags belong on line 1, not on all N lines.

    They are constant for the whole journey by definition, so repeating them per
    event bought a reader nothing and was the majority of every line.
    """
    client = start(tmp_path)
    with odyssey.journey(id="j", user_id="u_42", data_source="unit") as j:
        j.message(Message(role="user", content="hi"))

    header = odyssey.read_events(client.spool.shards("j")[0]).header
    assert header.journey_id == "j"
    assert header.data_source == "unit"
    assert header.journey_metadata == {"user_id": "u_42"}
    assert all("user_id" not in (e.metadata or {}) for e in events("j"))


def test_an_unserializable_tag_is_sanitized_before_it_reaches_the_header(tmp_path):
    """`header_line` json-dumps the snapshot, so a raw enum there is fatal.

    Not a hypothetical: it raised inside `_open_shard`, left a zero-byte shard,
    and every event of the journey was dropped with only a counter to show it.
    """
    import enum

    class Tier(enum.Enum):
        GOLD = "gold"

    client = start(tmp_path)
    with odyssey.journey(id="j", tier=Tier.GOLD) as j:
        j.message(Message(role="user", content="hi"))

    header = odyssey.read_events(client.spool.shards("j")[0]).header
    assert header.journey_metadata == {"tier": "gold"}
    assert client.stats.events_dropped == 0


def test_a_tag_added_mid_journey_rides_on_the_event(tmp_path):
    """The header was already written when the tag appeared, so it goes on the
    event — where a reader applying header-then-event gets the value that was
    true at that seq."""
    start(tmp_path)
    with odyssey.journey(id="j", tenant="acme") as outer:
        outer.message(Message(role="user", content="before"))
        with odyssey.journey(id="j", escalated=True):
            outer.message(Message(role="user", content="after"))

    by_content = {
        e.message.content: (e.metadata or {}) for e in events("j") if e.message
    }
    assert "escalated" not in by_content["before"]
    assert by_content["after"]["escalated"] is True
    # The original tag is in the header, so it never repeats on either event.
    assert all("tenant" not in m for m in by_content.values())


def test_terminal_is_emitted_on_exit(tmp_path):
    start(tmp_path)
    with odyssey.journey(id="j") as j:
        j.message(Message(role="assistant", content="done"))
    tail = events("j")[-1]
    assert tail.kind == "terminal"
    assert tail.terminal is not None
    assert tail.terminal.termination_reason == "ENV_DONE"


def test_an_exception_closes_the_journey_as_an_error_and_propagates(tmp_path):
    start(tmp_path)
    with pytest.raises(ValueError, match="app bug"):
        with odyssey.journey(id="j") as j:
            j.message(Message(role="user", content="hi"))
            raise ValueError("app bug")
    tail = events("j")[-1]
    assert tail.kind == "terminal"
    assert tail.terminal is not None
    assert tail.terminal.termination_reason == "ERROR"
    assert tail.terminal.error is not None
    assert "app bug" in tail.terminal.error


def test_terminal_makes_the_journey_foldable_and_trainable(tmp_path):
    start(tmp_path)
    with odyssey.journey(id="j") as j:
        j.message(Message(role="user", content="hi"))
        j.message(Message(role="assistant", content="hello"))
    result = odyssey.fold(events("j"), data_source="test")
    assert result.trainable
    assert result.incomplete_reason is None


def test_without_a_terminal_the_journey_is_not_trainable(tmp_path):
    start(tmp_path)
    with odyssey.journey(id="j", terminal=False) as j:
        j.message(Message(role="assistant", content="hi"))
    result = odyssey.fold(events("j"), data_source="test")
    assert not result.trainable
    assert result.incomplete_reason is not None
    assert "may still be running" in result.incomplete_reason


def test_close_is_idempotent(tmp_path):
    start(tmp_path)
    with odyssey.journey(id="j") as j:
        j.message(Message(role="user", content="hi"))
        assert j.close() is not None
        assert j.close() is None
    assert [e.kind for e in events("j")].count("terminal") == 1


def test_nesting_joins_the_parent_instead_of_splitting(tmp_path):
    """A decorated helper called mid-journey must not start a second one."""
    start(tmp_path)
    with odyssey.journey(id="outer") as outer:
        outer.message(Message(role="user", content="a"))
        with odyssey.journey() as inner:
            assert inner.id == "outer"
            inner.message(Message(role="assistant", content="b"))
    client = odyssey.get_client()
    assert client is not None
    assert client.spool.journey_ids() == ["outer"]
    assert [e.kind for e in events("outer")].count("terminal") == 1


def test_an_explicit_different_id_starts_a_separate_journey(tmp_path):
    start(tmp_path)
    with odyssey.journey(id="outer"):
        with odyssey.journey(id="inner") as inner:
            inner.message(Message(role="user", content="x"))
        assert current() is not None
        assert current().journey_id == "outer"  # type: ignore[union-attr]
    client = odyssey.get_client()
    assert client is not None
    assert sorted(client.spool.journey_ids()) == ["inner", "outer"]


def test_a_resumed_journey_continues_the_sequence(tmp_path):
    """Same journey id after a restart must not reissue seq 0."""
    start(tmp_path)
    with odyssey.journey(id="j", terminal=False) as j:
        j.message(Message(role="user", content="first"))
    odyssey.shutdown()

    start(tmp_path, force=True)
    with odyssey.journey(id="j") as j:
        j.message(Message(role="assistant", content="second"))
    assert [e.seq for e in events("j")] == [0, 1, 2]


# --------------------------------------------------------------------------
# signals and rewards — the preference-training inputs
# --------------------------------------------------------------------------


def test_signal_defaults_to_the_last_message(tmp_path):
    start(tmp_path)
    with odyssey.journey(id="j") as j:
        j.message(Message(role="user", content="q"))
        j.message(Message(role="assistant", content="a"))
        j.signal("thumbs_up")
    signal = [e for e in events("j") if e.kind == "signal"][0]
    assert signal.signal is not None
    assert signal.signal.target_seq == 1


def test_a_signal_with_no_message_to_target_is_dropped_not_raised(tmp_path):
    client = start(tmp_path)
    with odyssey.journey(id="j") as j:
        assert j.signal("thumbs_up") is None
    assert client.stats.capture_errors == 1
    assert [e.kind for e in events("j")] == ["terminal"]


def test_thumbs_down_marks_the_turn_untrainable(tmp_path):
    start(tmp_path)
    with odyssey.journey(id="j") as j:
        j.message(Message(role="user", content="q"))
        j.message(Message(role="assistant", content="bad"))
        j.signal("thumbs_down")
    result = odyssey.fold(events("j"), data_source="test")
    assert result.journey.steps[-1].trainable_status == "not_trainable"


def test_a_regeneration_supersedes_the_earlier_answer(tmp_path):
    """The minimum a DPO exporter needs: a rejected and a chosen answer."""
    start(tmp_path)
    with odyssey.journey(id="j") as j:
        j.message(Message(role="user", content="q"))
        first = j.message(Message(role="assistant", content="weak"))
        j.signal("regenerated", target_seq=first)
        j.message(Message(role="assistant", content="strong"))
        j.signal("thumbs_up")
    result = odyssey.fold(events("j"), data_source="test")
    statuses = [s.trainable_status for s in result.journey.steps]
    assert "superseded" in statuses
    assert "trainable" in statuses


def test_a_scalar_reward_is_wrapped_and_folded(tmp_path):
    start(tmp_path)
    with odyssey.journey(id="j") as j:
        j.message(Message(role="assistant", content="a"))
        j.reward(0.75)
    result = odyssey.fold(events("j"), data_source="test")
    assert result.journey.metrics is not None
    assert result.journey.metrics.aggregated_reward == pytest.approx(0.75)


# --------------------------------------------------------------------------
# Redaction on the capture path
# --------------------------------------------------------------------------


def test_a_secret_never_reaches_disk(tmp_path):
    start(tmp_path)
    with odyssey.journey(id="j") as j:
        j.message(
            Message(
                role="assistant",
                tool_calls=[
                    ToolCall(
                        id="tc",
                        name="call",
                        arguments={"api_key": "sk-live-secret", "day": "tue"},
                    )
                ],
            )
        )
    client = odyssey.get_client()
    assert client is not None
    raw = client.spool.shards("j")[0].read_text()
    assert "sk-live-secret" not in raw
    assert "[REDACTED]" in raw
    assert "tue" in raw


# --------------------------------------------------------------------------
# Recording outside a journey
# --------------------------------------------------------------------------


def test_events_outside_a_journey_are_dropped_and_counted(tmp_path):
    """Auto-creating a journey here would mint untrainable one-event noise."""
    client = start(tmp_path)
    assert odyssey.message(Message(role="user", content="orphan")) is None
    assert client.stats.events_recorded == 0
    assert client.spool.journey_ids() == []


# --------------------------------------------------------------------------
# @observe
# --------------------------------------------------------------------------


def test_observe_records_no_event_by_default(tmp_path):
    """A corpus is not a span log: an internal call is noise, not training data."""
    start(tmp_path)

    @odyssey.observe()
    def handle(text: str) -> str:
        return text.upper()

    with odyssey.journey(id="j"):
        assert handle("hi") == "HI"

    assert [e.kind for e in events("j")] == ["terminal"]


def test_observe_joins_the_ambient_journey(tmp_path):
    start(tmp_path)

    @odyssey.observe()
    def handle() -> str:
        ctx = current()
        assert ctx is not None
        return ctx.journey_id

    with odyssey.journey(id="outer"):
        assert handle() == "outer"


def test_observe_as_tool_records_arguments_and_result(tmp_path):
    start(tmp_path)

    @odyssey.observe(as_tool=True)
    def book(day: str, time: str) -> dict:
        return {"ok": True, "day": day}

    with odyssey.journey(id="j"):
        book("tue", time="15:00")

    tool = [e for e in events("j") if e.kind == "message"][0]
    assert tool.message is not None
    assert tool.message.role == "tool"
    response = tool.message.tool_response
    assert response is not None
    assert response.name == "book"
    assert response.arguments == {"day": "tue", "time": "15:00"}
    assert response.response == {"ok": True, "day": "tue"}
    assert response.metadata is not None
    assert "duration_ms" in response.metadata


def test_observe_as_tool_records_the_error_and_reraises(tmp_path):
    start(tmp_path)

    @odyssey.observe(as_tool=True)
    def boom() -> None:
        raise KeyError("missing slot")

    with odyssey.journey(id="j"):
        with pytest.raises(KeyError):
            boom()

    response = [e for e in events("j") if e.kind == "message"][0].message
    assert response is not None
    assert response.tool_response is not None
    assert response.tool_response.error is not None
    assert "KeyError" in response.tool_response.error
    assert response.tool_response.response is None


def test_observe_handles_an_unserialisable_return_value(tmp_path):
    """Auto-capture sees whatever the app returns; json.dumps must never break."""
    start(tmp_path)

    class Opaque:
        def __repr__(self) -> str:
            return "<Opaque>"

    @odyssey.observe(as_tool=True)
    def make() -> Opaque:
        return Opaque()

    with odyssey.journey(id="j"):
        make()

    client = odyssey.get_client()
    assert client is not None
    assert client.stats.capture_errors == 0
    response = [e for e in events("j") if e.kind == "message"][0].message
    assert response is not None
    assert response.tool_response is not None
    assert response.tool_response.response == "<Opaque>"


def test_observe_works_on_async_functions(tmp_path):
    start(tmp_path)

    @odyssey.observe(as_tool=True)
    async def fetch(x: int) -> int:
        await asyncio.sleep(0)
        return x * 2

    async def main():
        with odyssey.journey(id="j"):
            return await fetch(21)

    assert asyncio.run(main()) == 42
    response = [e for e in events("j") if e.kind == "message"][0].message
    assert response is not None
    assert response.tool_response is not None
    assert response.tool_response.response == 42


def test_observe_outside_a_journey_opens_one_when_given_an_id(tmp_path):
    start(tmp_path)

    @odyssey.observe(as_tool=True, journey_id="standalone")
    def work() -> str:
        return "done"

    work()
    kinds = [e.kind for e in events("standalone")]
    assert kinds == ["message", "terminal"]
    assert odyssey.fold(events("standalone"), data_source="test").trainable


# --------------------------------------------------------------------------
# Never raise — the property with no happy path
# --------------------------------------------------------------------------


def test_a_broken_spool_does_not_break_the_caller(tmp_path):
    client = start(tmp_path)

    def boom(_event, **_kw):
        raise OSError("disk full")

    client.spool.record = boom  # type: ignore[method-assign]
    with odyssey.journey(id="j", terminal=False) as j:
        assert j.message(Message(role="user", content="hi")) is None
    assert client.stats.capture_errors >= 1
    assert client.stats.events_dropped >= 1


def test_debug_mode_reraises_instead_of_swallowing(tmp_path):
    """Local development wants the traceback, production wants the counter."""
    client = start(tmp_path, debug=True)

    def boom(_event, **_kw):
        raise OSError("disk full")

    client.spool.record = boom  # type: ignore[method-assign]
    with pytest.raises(OSError, match="disk full"):
        with odyssey.journey(id="j", terminal=False) as j:
            j.message(Message(role="user", content="hi"))


def test_a_failing_flush_is_reported_not_raised(tmp_path):
    client = start(tmp_path)

    class Broken:
        def send(self, journey_id, events, header=None):
            raise RuntimeError("sink down")

    client.sink = Broken()
    with odyssey.journey(id="j") as j:
        j.message(Message(role="user", content="hi"))
    result = client.flush()
    assert not result.ok
    assert result.failed > 0


# --------------------------------------------------------------------------
# flush / shutdown
# --------------------------------------------------------------------------


def test_flush_writes_the_journey_out(tmp_path):
    start(tmp_path)
    with odyssey.journey(id="j") as j:
        j.message(Message(role="user", content="hi"))
    result = odyssey.flush()
    assert result is not None and result.pushed == 2

    out = tmp_path / "out" / "j.jsonl"
    assert out.exists()
    assert odyssey.read_events(out).clean


def test_a_second_flush_sends_nothing(tmp_path):
    start(tmp_path)
    with odyssey.journey(id="j") as j:
        j.message(Message(role="user", content="hi"))
    odyssey.flush()
    second = odyssey.flush()
    assert second is not None and second.pushed == 0


def test_closing_a_journey_releases_its_shard_handle(tmp_path):
    client = start(tmp_path)
    with odyssey.journey(id="j") as j:
        j.message(Message(role="user", content="hi"))
    assert client.spool.open_shard_count() == 0


def test_shutdown_clears_the_singleton(tmp_path):
    start(tmp_path)
    odyssey.shutdown()
    assert odyssey.get_client() is None


# --------------------------------------------------------------------------
# health
# --------------------------------------------------------------------------


def test_health_surfaces_counters_and_swallowed_errors(tmp_path):
    client = start(tmp_path)
    with odyssey.journey(id="j") as j:
        j.message(Message(role="user", content="hi"))
        j.signal("thumbs_up")

    def boom(_event, **_kw):
        raise OSError("nope")

    client.spool.record = boom  # type: ignore[method-assign]
    with odyssey.journey(id="j2", terminal=False) as j2:
        j2.message(Message(role="user", content="x"))

    report = odyssey.health()
    assert report["initialised"] is True
    assert report["writer_id"] == client.writer_id
    assert report["stats"]["events_recorded"] == 3
    assert report["stats"]["capture_errors"] == 1
    assert report["stats"]["recent_errors"]
    assert report["journeys_in_process"]


def test_health_never_grows_the_error_ring_without_bound(tmp_path):
    client = start(tmp_path)
    for i in range(20):
        client.note_error(f"e{i}", RuntimeError(str(i)))
    assert client.stats.capture_errors == 20
    assert len(client.stats.recent_errors) == 5
    assert "e19" in client.stats.recent_errors[-1]


# --------------------------------------------------------------------------
# The whole point, end to end
# --------------------------------------------------------------------------


def test_one_init_and_a_journey_yields_a_trainable_corpus_file(tmp_path):
    start(tmp_path)

    with odyssey.journey(id="call_8891", user_id="u_42") as j:
        j.message(Message(role="system", content="You book appointments."))
        j.message(Message(role="user", content="Book me for Tuesday at 3."))
        j.message(
            Message(
                role="assistant",
                tool_calls=[ToolCall(id="tc_1", name="book", arguments={"day": "tue"})],
            )
        )
        j.message(
            Message(
                role="tool",
                tool_response=ToolResponse(
                    id="tc_1", name="book", arguments={}, response={"ok": True}
                ),
            )
        )
        j.message(
            Message(role="assistant", content="Booked for 3pm."),
            model_id="claude-opus-5",
        )
        j.signal("thumbs_up")
        j.reward(0.9)

    odyssey.flush()

    reread = odyssey.read_events(tmp_path / "out" / "call_8891.jsonl")
    assert reread.clean
    result = odyssey.fold(reread.events, data_source="voice")
    assert result.trainable
    assert result.model_ids == ["claude-opus-5"]
    assert [s.trainable_status for s in result.journey.steps][-1] == "trainable"
    assert result.journey.metrics is not None
    assert result.journey.metrics.num_tool_calls == 1
    assert result.journey.metrics.aggregated_reward == pytest.approx(0.9)


# --------------------------------------------------------------------------
# Misusing journey() — visible, not silently wrong
#
# `journey()` is a context manager. Keeping only the handle and dropping the
# manager lets CPython garbage-collect the suspended generator, which throws
# GeneratorExit into it and ends the scope. That is API misuse, but it must
# degrade honestly: an abandoned scope is STALE, not a fake application ERROR,
# and the events that follow must be counted rather than vanish.
# --------------------------------------------------------------------------


def test_an_abandoned_scope_closes_as_stale_not_as_an_app_error(tmp_path):
    import gc

    start(tmp_path)
    handle = odyssey.journey(id="j").__enter__()  # manager not held — misuse
    gc.collect()

    tail = events("j")[-1]
    assert tail.kind == "terminal"
    assert tail.terminal is not None
    assert tail.terminal.termination_reason == "STALE"
    assert tail.terminal.error is not None
    assert "with odyssey.journey" in tail.terminal.error
    assert handle is not None


def test_recording_after_an_abandoned_scope_is_counted_not_silent(tmp_path):
    """The events do not land — but health() says so, which is the contract."""
    import gc

    client = start(tmp_path)
    handle = odyssey.journey(id="j").__enter__()
    gc.collect()

    for _ in range(3):
        assert handle.message(Message(role="user", content="lost")) is None
    assert client.stats.events_dropped == 3
    assert odyssey.health()["stats"]["events_dropped"] == 3


def test_a_real_exception_still_closes_as_error(tmp_path):
    """GeneratorExit is special-cased; nothing else is."""
    start(tmp_path)
    with pytest.raises(ValueError, match="real bug"):
        with odyssey.journey(id="j"):
            raise ValueError("real bug")
    tail = events("j")[-1]
    assert tail.terminal is not None
    assert tail.terminal.termination_reason == "ERROR"
    assert tail.terminal.error is not None
    assert "real bug" in tail.terminal.error


# --------------------------------------------------------------------------
# The closer registry: integrations hand shutdown a way to end open journeys
# --------------------------------------------------------------------------


class _FakeRecorder:
    """The shape `Client.register_journey` expects, and nothing more."""

    def __init__(self):
        self.calls = []

    def close(self, *, reason="ENV_DONE", error=None):
        self.calls.append((reason, error))


def test_shutdown_closes_registered_journeys(tmp_path):
    client = start(tmp_path)
    rec = _FakeRecorder()
    client.register_journey(rec)
    assert client.health()["open_journeys"] == 1

    odyssey.shutdown()
    assert rec.calls == [("STALE", client_mod._ABANDONED)]


def test_an_unregistered_journey_is_left_alone(tmp_path):
    client = start(tmp_path)
    rec = _FakeRecorder()
    client.register_journey(rec)
    client.unregister_journey(rec)

    odyssey.shutdown()
    assert rec.calls == []


def test_one_closer_raising_does_not_strand_the_others(tmp_path):
    """Shutdown is the last chance every other journey will get."""

    class Exploding(_FakeRecorder):
        def close(self, *, reason="ENV_DONE", error=None):
            raise RuntimeError("boom")

    client = start(tmp_path)
    bad, good = Exploding(), _FakeRecorder()
    client.register_journey(bad)
    client.register_journey(good)

    client.close_open_journeys()
    assert good.calls  # not stranded behind the failure
    assert client.stats.capture_errors >= 1


def test_the_registry_holds_recorders_weakly(tmp_path):
    """A long-lived worker runs thousands of calls; a strong ref would keep every
    finished recorder — and its journey context — alive for the process."""
    import gc

    client = start(tmp_path)
    client.register_journey(_FakeRecorder())  # no local ref survives
    gc.collect()
    assert client.health()["open_journeys"] == 0


def test_closing_open_journeys_is_idempotent(tmp_path):
    client = start(tmp_path)
    rec = _FakeRecorder()
    client.register_journey(rec)
    assert client.close_open_journeys() == 1
    assert client.close_open_journeys() == 0
    assert len(rec.calls) == 1
