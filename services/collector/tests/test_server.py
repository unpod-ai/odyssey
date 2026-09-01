"""odyssey-collector, against a real server on an ephemeral port.

The happy-path tests dogfood ``odyssey.HttpSink`` as the client — the exact
thing this server exists to receive — so a passing suite proves the two
projects' idea of the wire contract still agrees, not just that this file's
own assumptions about it are internally consistent.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest
from odyssey import HttpSink
from odyssey.jsonl import encode_event, header_line, read_events
from odyssey.primitives import JourneyEvent, JourneyHeader, Message, Terminal
from odyssey.sinks import HttpSinkError

from odyssey_collector.server import (
    CollectorConfig,
    Product,
    _init_products_file,
    _safe_stem,
    resolve_config,
    serve,
)

JID = "j_collector"
FIXED_DATE = "2026-08-27"

HEADER = JourneyHeader(
    journey_id=JID,
    data_source="livekit",
    trace_id="t_1",
    started_at="2026-01-01T00:00:00+00:00",
)


def evs() -> list[JourneyEvent]:
    return [
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
    ]


@pytest.fixture
def running(tmp_path):
    config = CollectorConfig(
        host="127.0.0.1",
        port=0,
        data_dir=tmp_path / "data",
        date_fn=lambda: FIXED_DATE,
    )
    server = serve(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join()


def endpoint(server) -> str:
    host, port = server.server_address
    return f"http://{host}:{port}"


def stored_path(server, journey_id: str = JID):
    return server.config.data_dir / FIXED_DATE / f"{_safe_stem(journey_id)}.jsonl"


# --------------------------------------------------------------------------
# The happy path, through the real client
# --------------------------------------------------------------------------


def test_health(running):
    with urllib.request.urlopen(f"{endpoint(running)}/health") as resp:
        assert resp.status == 200
        assert json.loads(resp.read()) == {"status": "ok"}


def test_a_batch_sent_via_httpsink_is_persisted_and_readable(running):
    sent = evs()
    HttpSink(endpoint(running)).send(JID, sent, header=HEADER)

    result = read_events(stored_path(running))
    assert result.clean
    assert result.events == sent
    assert result.header == HEADER


def test_a_second_drain_appends_without_a_second_header(running):
    HttpSink(endpoint(running)).send(JID, evs()[:1], header=HEADER)
    HttpSink(endpoint(running)).send(JID, evs()[1:], header=HEADER)

    raw = stored_path(running).read_text()
    assert raw.count("odyssey_schema_version") == 1
    assert [e.seq for e in read_events(stored_path(running)).events] == [0, 1]


def test_a_retried_batch_is_not_written_twice(running):
    """HttpSink retries on failure; a lost 200 must not double the raw layer."""
    sent = evs()
    HttpSink(endpoint(running)).send(JID, sent, header=HEADER)
    HttpSink(endpoint(running)).send(JID, sent, header=HEADER)  # the "retry"

    result = read_events(stored_path(running))
    assert result.events == sent
    raw = stored_path(running).read_text()
    assert raw.count("odyssey_schema_version") == 1


def test_a_partially_new_retried_batch_only_writes_the_new_events(running):
    HttpSink(endpoint(running)).send(JID, evs()[:1], header=HEADER)
    HttpSink(endpoint(running)).send(JID, evs(), header=HEADER)  # e0 repeats, e1 new

    result = read_events(stored_path(running))
    assert [e.event_id for e in result.events] == ["e0", "e1"]


def test_different_journeys_land_in_different_files(running):
    HttpSink(endpoint(running)).send("j_a", evs())
    HttpSink(endpoint(running)).send("j_b", evs())
    assert stored_path(running, "j_a").exists()
    assert stored_path(running, "j_b").exists()


# --------------------------------------------------------------------------
# Date partitioning — <data_dir>/<date>/<journey_id>.jsonl
# --------------------------------------------------------------------------


def test_a_batch_lands_under_the_date_it_was_received(running):
    HttpSink(endpoint(running)).send(JID, evs())
    assert (running.config.data_dir / FIXED_DATE / f"{JID}.jsonl").exists()
    # And nowhere else — no flat <data_dir>/<journey_id>.jsonl left behind.
    assert not (running.config.data_dir / f"{JID}.jsonl").exists()


def test_a_new_day_starts_a_fresh_date_directory(tmp_path):
    today = [FIXED_DATE]
    config = CollectorConfig(
        host="127.0.0.1", port=0, data_dir=tmp_path / "data", date_fn=lambda: today[0]
    )
    server = serve(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        HttpSink(endpoint(server)).send(JID, evs()[:1])
        today[0] = "2026-08-28"
        HttpSink(endpoint(server)).send(JID, evs()[1:])

        first_day = config.data_dir / FIXED_DATE / f"{JID}.jsonl"
        second_day = config.data_dir / "2026-08-28" / f"{JID}.jsonl"
        assert first_day.exists() and second_day.exists()
        assert [e.seq for e in read_events(first_day).events] == [0]
        assert [e.seq for e in read_events(second_day).events] == [1]
    finally:
        server.shutdown()
        thread.join()


def test_default_timezone_is_utc(tmp_path, monkeypatch):
    monkeypatch.delenv("ODYSSEY_COLLECTOR_TIMEZONE", raising=False)
    from datetime import datetime, timezone

    config = CollectorConfig(host="127.0.0.1", port=0, data_dir=tmp_path / "data")
    server = serve(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        HttpSink(endpoint(server)).send(JID, evs())
        expected = datetime.now(timezone.utc).date().isoformat()
        assert (config.data_dir / expected / f"{JID}.jsonl").exists()
    finally:
        server.shutdown()
        thread.join()


def test_explicit_timezone_is_respected(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    config = CollectorConfig(
        host="127.0.0.1", port=0, data_dir=tmp_path / "data", timezone="Asia/Kolkata"
    )
    server = serve(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        HttpSink(endpoint(server)).send(JID, evs())
        expected = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
        assert (config.data_dir / expected / f"{JID}.jsonl").exists()
    finally:
        server.shutdown()
        thread.join()


def test_the_timezone_env_var_is_respected(tmp_path, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    monkeypatch.setenv("ODYSSEY_COLLECTOR_TIMEZONE", "Asia/Kolkata")
    config = resolve_config(host="127.0.0.1", port=0, data_dir=tmp_path / "data")
    server = serve(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        HttpSink(endpoint(server)).send(JID, evs())
        expected = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
        assert (config.data_dir / expected / f"{JID}.jsonl").exists()
    finally:
        server.shutdown()
        thread.join()


def test_an_unknown_timezone_falls_back_to_utc(tmp_path):
    from datetime import datetime, timezone

    config = CollectorConfig(
        host="127.0.0.1", port=0, data_dir=tmp_path / "data", timezone="Not/A_Real_Zone"
    )
    server = serve(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        HttpSink(endpoint(server)).send(JID, evs())
        expected = datetime.now(timezone.utc).date().isoformat()
        assert (config.data_dir / expected / f"{JID}.jsonl").exists()
    finally:
        server.shutdown()
        thread.join()


def test_a_traversal_journey_id_cannot_escape_data_dir(running):
    """A journey_id is caller-chosen; nothing stops one holding a separator.
    Written naively, "../../etc/passwd" would escape data_dir entirely."""
    HttpSink(endpoint(running)).send("../../../etc/passwd", evs())

    escaped = (
        running.config.data_dir / ".." / ".." / ".." / "etc" / "passwd"
    ).resolve()
    assert not escaped.exists()

    written = list((running.config.data_dir / FIXED_DATE).glob("*.jsonl"))
    assert len(written) == 1
    assert written[0].resolve().is_relative_to(running.config.data_dir.resolve())
    assert "etc_passwd" in written[0].name


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


@pytest.fixture
def guarded(tmp_path):
    config = CollectorConfig(
        host="127.0.0.1",
        port=0,
        data_dir=tmp_path / "data",
        api_key="sk-collector",
        date_fn=lambda: FIXED_DATE,
    )
    server = serve(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join()


def test_a_missing_key_is_rejected_and_nothing_is_written(guarded):
    with pytest.raises(HttpSinkError, match="HTTP 401"):
        HttpSink(endpoint(guarded)).send(JID, evs())
    assert not stored_path(guarded).exists()


def test_the_correct_key_is_accepted(guarded):
    sent = evs()
    HttpSink(endpoint(guarded), api_key="sk-collector").send(JID, sent)
    assert read_events(stored_path(guarded)).events == sent


def test_the_wrong_key_is_rejected(guarded):
    with pytest.raises(HttpSinkError, match="HTTP 401"):
        HttpSink(endpoint(guarded), api_key="sk-wrong").send(JID, evs())


# --------------------------------------------------------------------------
# Product scoping — multiple registered keys, isolated storage
# --------------------------------------------------------------------------


ACME = Product(slug="proj_acme", name="Acme Corp", api_key="sk-acme")
GLOBEX = Product(slug="proj_globex", name="Globex Inc", api_key="sk-globex")


@pytest.fixture
def scoped(tmp_path):
    config = CollectorConfig(
        host="127.0.0.1",
        port=0,
        data_dir=tmp_path / "data",
        products=(ACME, GLOBEX),
        date_fn=lambda: FIXED_DATE,
    )
    server = serve(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join()


def product_path(server, slug, journey_id=JID):
    return (
        server.config.data_dir / slug / FIXED_DATE / f"{_safe_stem(journey_id)}.jsonl"
    )


def test_a_registered_key_lands_under_its_own_product(scoped):
    sent = evs()
    HttpSink(endpoint(scoped), api_key="sk-acme").send(JID, sent)
    assert read_events(product_path(scoped, "proj_acme")).events == sent
    assert not product_path(scoped, "proj_globex").exists()
    # And nowhere unscoped either -- product mode always partitions by product.
    assert not (scoped.config.data_dir / FIXED_DATE).exists()


def test_two_products_writing_the_same_journey_id_never_collide(scoped):
    acme_events = evs()
    globex_events = evs()[:1]
    HttpSink(endpoint(scoped), api_key="sk-acme").send(JID, acme_events)
    HttpSink(endpoint(scoped), api_key="sk-globex").send(JID, globex_events)

    assert read_events(product_path(scoped, "proj_acme")).events == acme_events
    assert read_events(product_path(scoped, "proj_globex")).events == globex_events


def test_an_unregistered_key_is_rejected_and_nothing_is_written(scoped):
    with pytest.raises(HttpSinkError, match="HTTP 401"):
        HttpSink(endpoint(scoped), api_key="sk-not-registered").send(JID, evs())
    assert not product_path(scoped, "proj_acme").exists()
    assert not product_path(scoped, "proj_globex").exists()


def test_a_missing_key_is_rejected_in_product_mode_too(scoped):
    with pytest.raises(HttpSinkError, match="HTTP 401"):
        HttpSink(endpoint(scoped)).send(JID, evs())


def test_api_key_and_products_are_mutually_exclusive():
    with pytest.raises(ValueError, match="not both"):
        CollectorConfig(api_key="sk-shared", products=(ACME,))


def test_a_malformed_products_file_fails_fast_at_startup(tmp_path):
    bad = tmp_path / "keys.json"
    bad.write_text("not json")
    with pytest.raises(json.JSONDecodeError):
        resolve_config(data_dir=tmp_path / "data", products_file=bad)


def test_a_products_file_missing_the_products_key_is_rejected(tmp_path):
    bad = tmp_path / "keys.json"
    bad.write_text(json.dumps({"sk-a": "proj_a"}))  # the old flat-map shape
    with pytest.raises(ValueError, match="products file must be"):
        resolve_config(data_dir=tmp_path / "data", products_file=bad)


def test_a_product_entry_missing_a_field_is_rejected(tmp_path):
    bad = tmp_path / "keys.json"
    bad.write_text(
        json.dumps({"products": [{"slug": "proj_a", "api_key": "sk-a"}]})  # no name
    )
    with pytest.raises(ValueError, match="slug.*name.*api_key"):
        resolve_config(data_dir=tmp_path / "data", products_file=bad)


def test_a_duplicate_slug_is_rejected(tmp_path):
    bad = tmp_path / "keys.json"
    bad.write_text(
        json.dumps(
            {
                "products": [
                    {"slug": "proj_a", "name": "A", "api_key": "sk-1"},
                    {"slug": "proj_a", "name": "A Again", "api_key": "sk-2"},
                ]
            }
        )
    )
    with pytest.raises(ValueError, match="duplicate product slug"):
        resolve_config(data_dir=tmp_path / "data", products_file=bad)


def test_a_duplicate_api_key_is_rejected(tmp_path):
    bad = tmp_path / "keys.json"
    bad.write_text(
        json.dumps(
            {
                "products": [
                    {"slug": "proj_a", "name": "A", "api_key": "sk-shared"},
                    {"slug": "proj_b", "name": "B", "api_key": "sk-shared"},
                ]
            }
        )
    )
    with pytest.raises(ValueError, match="same api_key"):
        resolve_config(data_dir=tmp_path / "data", products_file=bad)


def test_a_valid_products_file_round_trips_through_resolve_config(tmp_path):
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(
        json.dumps(
            {"products": [{"slug": "proj_a", "name": "A Corp", "api_key": "sk-a"}]}
        )
    )
    config = resolve_config(data_dir=tmp_path / "data", products_file=keys_file)
    assert config.products == (Product(slug="proj_a", name="A Corp", api_key="sk-a"),)


def test_init_products_file_writes_a_loadable_roster(tmp_path):
    """The bootstrap path (`odyssey-collector --init-products-file`) writes
    exactly the shape `resolve_config`/`_load_products_file` already accept --
    no second parser, no drift between "what writes it" and "what reads it".
    """
    path = tmp_path / "keys.json"
    written = _init_products_file(path, slug="acme", name="Acme Corp")

    assert written.slug == "acme"
    assert written.name == "Acme Corp"
    assert len(written.api_key) >= 32  # a real secret, not a short placeholder

    config = resolve_config(data_dir=tmp_path / "data", products_file=path)
    assert config.products == (written,)


def test_init_products_file_refuses_to_overwrite_an_existing_file(tmp_path):
    path = tmp_path / "keys.json"
    first = _init_products_file(path, slug="acme", name="Acme Corp")

    with pytest.raises(FileExistsError):
        _init_products_file(path, slug="acme", name="Acme Corp")

    # the real roster on disk must be untouched by the failed second call
    config = resolve_config(data_dir=tmp_path / "data", products_file=path)
    assert config.products == (first,)


def test_init_products_file_generates_a_different_key_each_time(tmp_path):
    a = _init_products_file(tmp_path / "a.json", slug="acme", name="Acme")
    b = _init_products_file(tmp_path / "b.json", slug="acme", name="Acme")
    assert a.api_key != b.api_key


def test_a_slug_cannot_traverse_out_of_data_dir(tmp_path):
    """A keys file is operator-authored, but defence in depth is cheap --
    the same journey_id traversal guard applies to a product's slug."""
    evil = Product(slug="../../etc", name="Evil", api_key="sk-evil")
    config = CollectorConfig(
        host="127.0.0.1",
        port=0,
        data_dir=tmp_path / "data",
        products=(evil,),
        date_fn=lambda: FIXED_DATE,
    )
    server = serve(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        HttpSink(endpoint(server), api_key="sk-evil").send(JID, evs())
        escaped = (config.data_dir / ".." / "etc").resolve()
        assert not escaped.exists()
        written = list(config.data_dir.glob("**/*.jsonl"))
        assert len(written) == 1
        assert written[0].resolve().is_relative_to(config.data_dir.resolve())
    finally:
        server.shutdown()
        thread.join()


# --------------------------------------------------------------------------
# GET /products — the roster, names + slugs, never keys
# --------------------------------------------------------------------------


def _get_json(url, *, api_key=None):
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request) as resp:
        return resp.status, json.loads(resp.read())


def test_get_products_lists_the_roster_by_slug_and_name(scoped):
    status, body = _get_json(f"{endpoint(scoped)}/products", api_key="sk-acme")
    assert status == 200
    assert body == {
        "products": [
            {"slug": "proj_acme", "name": "Acme Corp"},
            {"slug": "proj_globex", "name": "Globex Inc"},
        ]
    }


def test_get_products_never_includes_api_keys(scoped):
    _, body = _get_json(f"{endpoint(scoped)}/products", api_key="sk-acme")
    assert "sk-acme" not in json.dumps(body)
    assert "sk-globex" not in json.dumps(body)


def test_get_products_requires_a_registered_key(scoped):
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _get_json(f"{endpoint(scoped)}/products")
    assert exc_info.value.code == 401


def test_get_products_is_404_outside_product_scoped_mode(running):
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _get_json(f"{endpoint(running)}/products")
    assert exc_info.value.code == 404


# --------------------------------------------------------------------------
# Cross-journey batching (item 1.7) — POST /batch/events
# --------------------------------------------------------------------------


def test_a_batch_of_two_journeys_lands_in_two_files(running):
    sent_a, sent_b = evs(), evs()
    result = HttpSink(endpoint(running)).send_batch(
        [("j_a", sent_a, HEADER), ("j_b", sent_b, None)]
    )
    assert result == {"j_a": None, "j_b": None}
    assert read_events(stored_path(running, "j_a")).events == sent_a
    assert read_events(stored_path(running, "j_b")).events == sent_b


def test_a_batch_of_several_journeys_all_land_correctly(running):
    items = [(f"j_{i}", evs(), None) for i in range(5)]
    HttpSink(endpoint(running)).send_batch(items)
    for jid, sent, _header in items:
        assert read_events(stored_path(running, jid)).events == sent


def test_one_malformed_journey_in_a_batch_does_not_block_the_others(running):
    """Hand-built envelope, not HttpSink -- the client can only ever encode
    valid events, so a genuinely malformed per-journey blob has to be
    crafted directly, the same way test_a_malformed_body_is_rejected_with_400
    below builds a raw request rather than going through the client."""
    good = evs()[0]
    good_blob = header_line() + "\n" + encode_event(good) + "\n"
    envelope = json.dumps(
        {"journeys": {"j_good": good_blob, "j_bad": "not an odyssey header at all\n"}}
    ).encode()
    request = urllib.request.Request(
        f"{endpoint(running)}/batch/events", data=envelope, method="POST"
    )
    with urllib.request.urlopen(request) as resp:
        body = json.loads(resp.read())

    assert body["results"]["j_good"]["ok"] is True
    assert body["results"]["j_bad"]["ok"] is False
    assert read_events(stored_path(running, "j_good")).events == [good]
    assert not stored_path(running, "j_bad").exists()


def test_a_batch_journey_id_cannot_traverse_out_of_data_dir(running):
    result = HttpSink(endpoint(running)).send_batch(
        [("../../../etc/passwd", evs(), None)]
    )
    assert result["../../../etc/passwd"] is None
    escaped = (running.config.data_dir / ".." / ".." / ".." / "etc").resolve()
    assert not escaped.exists()
    written = list((running.config.data_dir / FIXED_DATE).glob("*.jsonl"))
    assert any("etc_passwd" in p.name for p in written)


def test_a_retried_batch_does_not_double_write_any_journey(running):
    sink = HttpSink(endpoint(running))
    sent_a, sent_b = evs(), evs()
    items = [("j_a", sent_a, HEADER), ("j_b", sent_b, HEADER)]
    sink.send_batch(items)
    sink.send_batch(items)  # the "retry"

    for jid, sent in (("j_a", sent_a), ("j_b", sent_b)):
        assert read_events(stored_path(running, jid)).events == sent
        raw = stored_path(running, jid).read_text()
        assert raw.count("odyssey_schema_version") == 1


def test_a_batch_requires_authorization_in_guarded_mode(guarded):
    with pytest.raises(HttpSinkError):
        HttpSink(endpoint(guarded)).send_batch([("j_a", evs(), None)])
    assert not stored_path(guarded, "j_a").exists()


def test_a_batch_is_product_scoped_like_single_sends(scoped):
    sent_a, sent_b = evs(), evs()
    HttpSink(endpoint(scoped), api_key="sk-acme").send_batch(
        [("j_a", sent_a, None), ("j_b", sent_b, None)]
    )
    assert read_events(product_path(scoped, "proj_acme", "j_a")).events == sent_a
    assert read_events(product_path(scoped, "proj_acme", "j_b")).events == sent_b


def test_a_malformed_batch_envelope_is_rejected_with_400(running):
    request = urllib.request.Request(
        f"{endpoint(running)}/batch/events",
        data=b"not a json object at all",
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(request)
    assert exc_info.value.code == 400


def test_batch_events_path_rejects_get(running):
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(f"{endpoint(running)}/batch/events")
    assert exc_info.value.code == 404


# --------------------------------------------------------------------------
# Malformed input — a validating ingest point, not a dumb pipe
# --------------------------------------------------------------------------


def test_a_bad_gzip_body_is_rejected_with_400(running):
    request = urllib.request.Request(
        f"{endpoint(running)}/journeys/{JID}/events",
        data=b"not actually gzip",
        method="POST",
        headers={"Content-Encoding": "gzip"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(request)
    assert exc_info.value.code == 400
    assert not stored_path(running).exists()


def test_an_uncompressed_body_is_still_accepted(running):
    """compress=False callers (or anything predating item 1.7) still work —
    Content-Encoding is optional, not required."""
    sent = evs()
    HttpSink(endpoint(running), compress=False).send(JID, sent, header=HEADER)
    result = read_events(stored_path(running))
    assert result.clean
    assert result.events == sent


def test_a_malformed_body_is_rejected_with_400_and_nothing_is_written(running):
    request = urllib.request.Request(
        f"{endpoint(running)}/journeys/{JID}/events",
        data=b"not an odyssey header at all\n",
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(request)
    assert exc_info.value.code == 400
    assert not stored_path(running).exists()


def test_an_empty_journey_id_is_rejected(running):
    request = urllib.request.Request(
        f"{endpoint(running)}/journeys//events", data=b"x", method="POST"
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(request)
    assert exc_info.value.code == 400


def test_an_unrecognised_path_is_404(running):
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(f"{endpoint(running)}/nonsense")
    assert exc_info.value.code == 404
