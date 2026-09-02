"""Against a real `services/api` instance (started via uvicorn in a
background thread), not a mocked transport — this repo's own established
convention for cross-member integration tests."""

from __future__ import annotations

import socket
import threading
import time

import pytest
import uvicorn
from odyssey.jsonl import write_events
from odyssey.primitives import JourneyEvent, JourneyHeader, Message, Terminal
from odyssey_api import deps
from odyssey_api.main import create_app
from odyssey_api.settings import Settings

from odyssey_sdk import OdysseyAPIError, OdysseyAPINotFoundError, OdysseySDK

JID = "j_sdk"
HEADER = JourneyHeader(journey_id=JID, data_source="livekit")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(tmp_path):
    journeys_dir = tmp_path / "journeys"
    date_dir = journeys_dir / "2026-08-28"
    date_dir.mkdir(parents=True)
    write_events(
        date_dir / f"{JID}.jsonl",
        [
            JourneyEvent(
                journey_id=JID,
                seq=0,
                kind="message",
                event_id="e0",
                message=Message(role="user", content="hi"),
            ),
            JourneyEvent(
                journey_id=JID,
                seq=1,
                kind="terminal",
                event_id="e1",
                terminal=Terminal(termination_reason="ENV_DONE"),
            ),
        ],
        header=HEADER,
    )

    app = create_app()
    app.dependency_overrides[deps.get_settings_dep] = lambda: Settings(
        journeys_dir=journeys_dir
    )
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.01)
    else:
        raise RuntimeError("server did not start in time")

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_health(live_server):
    client = OdysseySDK(live_server)
    assert client.health().status == "ok"


def test_journeys_list_and_get(live_server):
    client = OdysseySDK(live_server)
    listed = client.journeys.list()
    assert [j.journey_id for j in listed] == [JID]

    detail = client.journeys.get(JID)
    assert detail.complete is True
    assert detail.steps


def test_journeys_get_missing_raises_not_found(live_server):
    client = OdysseySDK(live_server)
    with pytest.raises(OdysseyAPINotFoundError):
        client.journeys.get("does-not-exist")


def test_empty_registries_return_empty_lists(live_server):
    client = OdysseySDK(live_server)
    assert client.datasets.list() == []
    assert client.models.list() == []
    assert client.runs.list() == []
    assert client.exports.list() == []


@pytest.fixture
def live_server_with_api_key(tmp_path):
    journeys_dir = tmp_path / "journeys"
    date_dir = journeys_dir / "2026-08-28"
    date_dir.mkdir(parents=True)
    write_events(
        date_dir / f"{JID}.jsonl",
        [
            JourneyEvent(
                journey_id=JID,
                seq=0,
                kind="message",
                event_id="e0",
                message=Message(role="user", content="hi"),
            ),
            JourneyEvent(
                journey_id=JID,
                seq=1,
                kind="terminal",
                event_id="e1",
                terminal=Terminal(termination_reason="ENV_DONE"),
            ),
        ],
        header=HEADER,
    )

    app = create_app()
    app.dependency_overrides[deps.get_settings_dep] = lambda: Settings(
        journeys_dir=journeys_dir, api_key="sk-test"
    )
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.01)
    else:
        raise RuntimeError("server did not start in time")

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_missing_api_key_raises_401(live_server_with_api_key, monkeypatch):
    monkeypatch.delenv("ODYSSEY_API_AUTH_KEY", raising=False)
    client = OdysseySDK(live_server_with_api_key)
    with pytest.raises(OdysseyAPIError) as exc_info:
        client.journeys.list()
    assert exc_info.value.status_code == 401


def test_wrong_api_key_raises_401(live_server_with_api_key):
    client = OdysseySDK(live_server_with_api_key, api_key="sk-wrong")
    with pytest.raises(OdysseyAPIError) as exc_info:
        client.journeys.list()
    assert exc_info.value.status_code == 401


def test_correct_api_key_succeeds(live_server_with_api_key):
    client = OdysseySDK(live_server_with_api_key, api_key="sk-test")
    listed = client.journeys.list()
    assert [j.journey_id for j in listed] == [JID]
