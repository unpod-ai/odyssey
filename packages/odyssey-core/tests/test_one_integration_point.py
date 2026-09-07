"""``odyssey.init()`` as the *only* line an application has to add.

Everything here is about what attaches itself without a second edit: which
targets `instrument="auto"` picks up, which it deliberately refuses to, and
where drained journeys go when the caller named no sink.
"""

from __future__ import annotations

import sys
import types

import pytest

import odyssey
from odyssey.client import _installed, _resolve_instrument
from odyssey.config import UNSET


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("ODYSSEY_INSTRUMENT", raising=False)
    monkeypatch.delenv("ODYSSEY_ENDPOINT", raising=False)
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


@pytest.fixture
def fake_openai(monkeypatch):
    """An importable `openai`, so `auto` has something real to find."""
    module = types.ModuleType("openai")

    class Completions:
        def create(self, **kwargs):
            return {"choices": []}

    resources = types.ModuleType("openai.resources")
    chat = types.ModuleType("openai.resources.chat")
    completions = types.ModuleType("openai.resources.chat.completions")
    completions.Completions = Completions  # type: ignore[attr-defined]
    chat.completions = completions  # type: ignore[attr-defined]
    resources.chat = chat  # type: ignore[attr-defined]
    module.resources = resources  # type: ignore[attr-defined]
    for name, mod in (
        ("openai", module),
        ("openai.resources", resources),
        ("openai.resources.chat", chat),
        ("openai.resources.chat.completions", completions),
    ):
        monkeypatch.setitem(sys.modules, name, mod)
    yield module
    from odyssey.integrations.openai import uninstrument

    uninstrument()


# --------------------------------------------------------------------------
# What "auto" means
# --------------------------------------------------------------------------


def test_auto_is_the_default(monkeypatch):
    """The default is what makes init the single integration point. An app
    that adds one line and nothing else has to be recording."""
    assert _resolve_instrument(UNSET) == _resolve_instrument("auto")


def test_auto_never_includes_otel():
    """A process with both a patched provider client and an OTel processor
    records every call twice, under two journeys — and `opentelemetry-sdk` is
    a transitive dependency nobody chose."""
    assert "otel" not in _resolve_instrument("auto")


def test_all_is_the_opt_in_that_includes_otel(monkeypatch):
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk", types.ModuleType("x"))
    assert "otel" in _resolve_instrument("all")
    assert "otel" not in _resolve_instrument("auto")


def test_auto_can_be_mixed_with_an_explicit_target():
    assert _resolve_instrument(["auto", "otel"])[-1] == "otel"


def test_a_target_is_never_attached_twice():
    assert _resolve_instrument(["openai", "openai", "all"]).count("openai") == 1


def test_nothing_attaches_when_asked_for_none():
    for value in ("none", "off", "", (), [], None):
        assert _resolve_instrument(value) == []


def test_the_environment_names_targets_without_touching_the_app(monkeypatch):
    """A deployment turns capture on and off without a code change."""
    monkeypatch.setenv("ODYSSEY_INSTRUMENT", "otel, openai")
    assert _resolve_instrument(UNSET) == ["otel", "openai"]


def test_an_explicit_argument_beats_the_environment(monkeypatch):
    monkeypatch.setenv("ODYSSEY_INSTRUMENT", "otel")
    assert _resolve_instrument("none") == []


def test_auto_skips_what_is_not_installed():
    """Expanded groups are filtered by what is importable, so `auto` in a
    process with no provider SDK attaches nothing rather than reporting four
    missing packages."""
    assert _resolve_instrument("auto") == [
        t for t in _resolve_instrument("auto") if _installed(t)
    ]


def test_auto_attaches_a_provider_that_is_installed(tmp_path, fake_openai):
    from odyssey.integrations.openai import is_instrumented

    start(tmp_path)
    assert is_instrumented()


def test_auto_leaves_a_provider_that_is_absent_alone(tmp_path):
    """Not an error, and not a warning: most processes have three of the four
    packages missing, and saying so on every start is noise."""
    start(tmp_path)
    assert odyssey.health()["stats"]["capture_errors"] == 0


def test_asking_for_nothing_patches_nothing(tmp_path, fake_openai):
    from odyssey.integrations.openai import is_instrumented

    start(tmp_path, instrument="none")
    assert not is_instrumented()


# --------------------------------------------------------------------------
# Targets that cannot attach from init
# --------------------------------------------------------------------------


def test_naming_livekit_explains_itself(tmp_path):
    """It attaches to an `AgentSession` the application owns. "Unknown target"
    would send someone looking for a typo that is not there."""
    start(tmp_path, instrument=["livekit"])

    errors = odyssey.health()["stats"]["recent_errors"]
    assert any("attach(session" in e for e in errors)


def test_naming_pipecat_explains_itself(tmp_path):
    start(tmp_path, instrument=["pipecat"])

    errors = odyssey.health()["stats"]["recent_errors"]
    assert any("attach(task" in e for e in errors)


def test_an_unknown_target_is_reported_not_raised(tmp_path):
    """A typo in a deployment's config must not take the application down."""
    start(tmp_path, instrument=["nonesuch_provider"])

    errors = odyssey.health()["stats"]["recent_errors"]
    assert any("nonesuch_provider" in e for e in errors)


def test_an_explicitly_named_missing_package_is_reported(tmp_path):
    """Unlike an expanded group, an explicit name is always attempted — asking
    for it and getting silence is worse than asking for it and being told."""
    start(tmp_path, instrument=["langchain"])

    assert odyssey.health()["stats"]["capture_errors"] >= 1


# --------------------------------------------------------------------------
# Where journeys go
# --------------------------------------------------------------------------


def test_an_endpoint_in_the_environment_is_enough_to_ship(tmp_path, monkeypatch):
    """The endpoint is already configured out-of-process; the code should not
    have to name it a second time."""
    from odyssey.sinks import HttpSink

    monkeypatch.setenv("ODYSSEY_ENDPOINT", "http://127.0.0.1:8787")
    client = start(tmp_path)
    assert isinstance(client.sink, HttpSink)


def test_no_endpoint_still_means_the_file_sink(tmp_path):
    from odyssey.sinks import FileSink

    client = start(tmp_path)
    assert isinstance(client.sink, FileSink)


def test_an_explicit_sink_always_wins(tmp_path, monkeypatch):
    from odyssey.sinks import FileSink

    monkeypatch.setenv("ODYSSEY_ENDPOINT", "http://127.0.0.1:8787")
    client = start(tmp_path, sink=FileSink(tmp_path / "explicit"))
    assert isinstance(client.sink, FileSink)


def test_a_malformed_endpoint_falls_back_instead_of_failing_init(tmp_path, monkeypatch):
    """`init()` raising would take the application down over a typo in an
    environment variable — the one thing this layer must never do."""
    from odyssey.sinks import FileSink

    monkeypatch.setenv("ODYSSEY_ENDPOINT", "not-a-url")
    client = start(tmp_path)
    assert isinstance(client.sink, FileSink)
    assert odyssey.health()["stats"]["capture_errors"] >= 1
