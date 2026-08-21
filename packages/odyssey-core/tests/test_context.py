"""Ambient context and seq allocation. No filesystem, no client — pure unit."""

from __future__ import annotations

import asyncio
import threading

import pytest

from odyssey.context import JourneyContext, SeqAllocator, bind, current

# --------------------------------------------------------------------------
# SeqAllocator
# --------------------------------------------------------------------------


def alloc(seed=None) -> SeqAllocator:
    return SeqAllocator(lambda _jid: seed)


def test_fresh_journey_starts_at_zero():
    a = alloc()
    assert [a.next("j") for _ in range(4)] == [0, 1, 2, 3]


def test_journeys_are_numbered_independently():
    a = alloc()
    assert (a.next("a"), a.next("b"), a.next("a")) == (0, 0, 1)


def test_resumed_journey_continues_past_what_is_on_disk():
    """The whole point of seeding: a restart must not reissue used numbers."""
    a = SeqAllocator(lambda _jid: 41)
    assert a.next("j") == 42


def test_seed_is_consulted_once_per_journey():
    calls: list[str] = []

    def seed(jid: str):
        calls.append(jid)
        return 5

    a = SeqAllocator(seed)
    [a.next("j") for _ in range(3)]
    assert calls == ["j"]


def test_a_failing_seed_does_not_break_recording():
    """A broken spool read must degrade to 0, not take the app down."""

    def seed(_jid: str):
        raise OSError("disk gone")

    assert SeqAllocator(seed).next("j") == 0


def test_forget_makes_the_journey_reseed():
    a = SeqAllocator(lambda _jid: 9)
    assert a.next("j") == 10
    a.forget("j")
    assert a.next("j") == 10


def test_peek_does_not_consume():
    a = alloc()
    a.next("j")
    assert a.peek("j") == 1
    assert a.peek("j") == 1
    assert a.next("j") == 1


def test_peek_on_an_untouched_journey_is_none():
    assert alloc().peek("nope") is None


def test_tracked_lists_touched_journeys():
    a = alloc()
    a.next("b")
    a.next("a")
    assert a.tracked() == ["a", "b"]


def test_no_number_is_ever_issued_twice_under_threads():
    """The invariant that makes a single-writer journey sound."""
    a = alloc()
    seen: list[int] = []
    lock = threading.Lock()

    def worker():
        local = [a.next("j") for _ in range(200)]
        with lock:
            seen.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(seen) == 1600
    assert len(set(seen)) == 1600, "a seq was handed out twice"
    assert sorted(seen) == list(range(1600)), "the sequence has holes"


# --------------------------------------------------------------------------
# The ContextVar
# --------------------------------------------------------------------------


def ctx(jid: str = "j") -> JourneyContext:
    return JourneyContext(journey_id=jid, allocator=alloc())


def test_no_journey_by_default():
    assert current() is None


def test_bind_sets_and_restores():
    c = ctx()
    with bind(c):
        assert current() is c
    assert current() is None


def test_bind_restores_even_on_exception():
    with pytest.raises(RuntimeError):
        with bind(ctx()):
            raise RuntimeError("boom")
    assert current() is None


def test_nested_bind_restores_the_outer_context():
    outer, inner = ctx("outer"), ctx("inner")
    with bind(outer):
        with bind(inner):
            assert current() is inner
        assert current() is outer


def test_context_propagates_into_asyncio_tasks():
    """asyncio copies the context into a task, so a journey survives an await."""
    c = ctx("async")

    async def child() -> str | None:
        got = current()
        return None if got is None else got.journey_id

    async def main() -> str | None:
        with bind(c):
            return await asyncio.create_task(child())

    assert asyncio.run(main()) == "async"


def test_context_does_not_leak_into_a_new_thread():
    """Documented caveat: threading.Thread starts from defaults, not a copy.

    This is why bind() is public — a handoff has to be explicit.
    """
    seen: list[object] = []
    c = ctx()

    def worker():
        seen.append(current())

    with bind(c):
        t = threading.Thread(target=worker)
        t.start()
        t.join()

    assert seen == [None]


def test_bind_carries_the_context_across_a_thread():
    seen: list[str] = []
    c = ctx("carried")

    def worker(handoff):
        with bind(handoff):
            got = current()
            assert got is not None
            seen.append(got.journey_id)

    with bind(c):
        t = threading.Thread(target=worker, args=(current(),))
        t.start()
        t.join()

    assert seen == ["carried"]


# --------------------------------------------------------------------------
# Metadata vs state — the separation that keeps bookkeeping out of the corpus
# --------------------------------------------------------------------------


def test_metadata_and_state_are_distinct():
    c = ctx()
    c.metadata["user_id"] = "u_1"
    c.state["_internal"] = 3
    assert "user_id" in c.metadata and "_internal" not in c.metadata
    assert "_internal" in c.state and "user_id" not in c.state
