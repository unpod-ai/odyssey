"""End-to-end against a real FastAPI app + real files on disk, not mocks —
this repo's own established convention (see 5.9/6.1/7's own test suites)."""

from __future__ import annotations

import json

import yaml
from fastapi.testclient import TestClient
from odyssey.jsonl import write_events
from odyssey.primitives import JourneyEvent, JourneyHeader, Message, Terminal

from odyssey_api import deps
from odyssey_api.main import create_app
from odyssey_api.settings import Settings

JID = "j_api"
HEADER = JourneyHeader(journey_id=JID, data_source="livekit")


def _client(settings: Settings) -> TestClient:
    app = create_app()
    app.dependency_overrides[deps.get_settings_dep] = lambda: settings
    return TestClient(app)


def test_health(tmp_path):
    client = _client(Settings())
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_journeys_list_and_detail(tmp_path):
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
    settings = Settings(journeys_dir=journeys_dir)
    client = _client(settings)

    listed = client.get("/journeys")
    assert listed.status_code == 200
    assert listed.json() == [
        {"journey_id": JID, "date": "2026-08-28", "complete": True}
    ]

    detail = client.get(f"/journeys/{JID}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["complete"] is True
    assert body["steps"]

    missing = client.get("/journeys/does-not-exist")
    assert missing.status_code == 404


def test_journeys_list_excludes_metrics_directory(tmp_path):
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

    # Mimic what services/collector's POST /metrics writes: a `metrics/`
    # directory (not a valid ISO date) directly under journeys_dir, with a
    # .jsonl file inside it.
    metrics_dir = journeys_dir / "metrics"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "2026-08-28.jsonl").write_text('{"metric": "x"}\n')

    settings = Settings(journeys_dir=journeys_dir)
    client = _client(settings)

    listed = client.get("/journeys")
    assert listed.status_code == 200
    body = listed.json()
    assert body == [{"journey_id": JID, "date": "2026-08-28", "complete": True}]
    assert all(entry["date"] != "metrics" for entry in body)
    assert all(entry["journey_id"] != "2026-08-28" for entry in body)


def test_datasets_and_models(tmp_path):
    datasets_registry = tmp_path / "datasets" / "registry.yaml"
    datasets_registry.parent.mkdir(parents=True)
    datasets_registry.write_text(
        yaml.safe_dump(
            {"corpora": {"c1": [{"version": 1, "manifest_sha256": "a", "uri": "u"}]}}
        )
    )
    models_registry = tmp_path / "models" / "registry.yaml"
    models_registry.parent.mkdir(parents=True)
    models_registry.write_text(
        yaml.safe_dump({"models": {"m1": [{"version": 1, "sha256": "a", "uri": "u"}]}})
    )
    settings = Settings(
        datasets_registry=datasets_registry, models_registry=models_registry
    )
    client = _client(settings)

    ds = client.get("/datasets")
    assert ds.status_code == 200
    assert ds.json()[0]["name"] == "c1"

    ds_one = client.get("/datasets/c1")
    assert ds_one.status_code == 200
    assert client.get("/datasets/nope").status_code == 404

    models = client.get("/models")
    assert models.status_code == 200
    assert models.json()[0]["name"] == "m1"
    assert client.get("/models/nope").status_code == 404


def test_products_list_drops_api_key(tmp_path):
    products_file = tmp_path / "products.json"
    products_file.write_text(
        json.dumps(
            {
                "products": [
                    {"slug": "unpod", "name": "Unpod", "api_key": "secret-key"},
                ]
            }
        )
    )
    settings = Settings(products_file=products_file)
    client = _client(settings)

    resp = client.get("/products")
    assert resp.status_code == 200
    assert resp.json() == [{"slug": "unpod", "name": "Unpod"}]
    assert "secret-key" not in resp.text


def test_products_list_empty_when_unset():
    client = _client(Settings())
    resp = client.get("/products")
    assert resp.status_code == 200
    assert resp.json() == []


def test_journeys_and_metrics_product_filter(tmp_path):
    journeys_dir = tmp_path / "journeys"
    for slug, jid in (("unpod", "j_a"), ("otherpod", "j_b")):
        date_dir = journeys_dir / slug / "2026-08-28"
        date_dir.mkdir(parents=True)
        header = JourneyHeader(journey_id=jid, data_source="livekit")
        write_events(
            date_dir / f"{jid}.jsonl",
            [
                JourneyEvent(
                    journey_id=jid,
                    seq=0,
                    kind="message",
                    event_id="e0",
                    message=Message(role="user", content="hi"),
                ),
                JourneyEvent(
                    journey_id=jid,
                    seq=1,
                    kind="terminal",
                    event_id="e1",
                    terminal=Terminal(termination_reason="ENV_DONE"),
                ),
            ],
            header=header,
        )
        metrics_dir = journeys_dir / slug / "metrics"
        metrics_dir.mkdir()
        (metrics_dir / "2026-08-28.jsonl").write_text(
            json.dumps({"ts": "2026-08-28T00:00:00Z", "hostname": slug, "os": "linux"})
            + "\n",
            encoding="utf-8",
        )

    settings = Settings(journeys_dir=journeys_dir)
    client = _client(settings)

    filtered = client.get("/journeys", params={"product": "unpod"})
    assert filtered.status_code == 200
    assert [j["journey_id"] for j in filtered.json()] == ["j_a"]

    filtered_metrics = client.get("/metrics", params={"product": "unpod"})
    assert filtered_metrics.status_code == 200
    assert [m["hostname"] for m in filtered_metrics.json()] == ["unpod"]

    unfiltered = client.get("/journeys")
    assert {j["journey_id"] for j in unfiltered.json()} == {"j_a", "j_b"}


def test_runs_and_exports(tmp_path):
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    (reports_dir / "b1.json").write_text(
        json.dumps(
            {"benchmark": "b1", "metric": "exact_match", "mean_score": 1.0, "tasks": []}
        )
    )
    exports_dir = tmp_path / "exports"
    exports_dir.mkdir()
    (exports_dir / "sft.jsonl").write_text('{"messages": []}\n')

    settings = Settings(eval_reports_dir=reports_dir, exports_dir=exports_dir)
    client = _client(settings)

    runs = client.get("/runs")
    assert runs.status_code == 200
    assert runs.json()[0]["benchmark_name"] == "b1"

    exp = client.get("/exports")
    assert exp.status_code == 200
    assert exp.json()[0]["rows"] == 1


def test_metrics_list(tmp_path):
    journeys_dir = tmp_path / "journeys"
    metrics_dir = journeys_dir / "metrics"
    metrics_dir.mkdir(parents=True)
    snapshot = {
        "ts": "2026-09-02T10:00:00+00:00",
        "hostname": "host1",
        "os": "Linux-x86_64",
        "cpu_count": 8,
        "memory_total_bytes": 1000,
        "memory_available_bytes": 500,
        "disk_total_bytes": 2000,
        "disk_free_bytes": 1000,
        "project": "demo",
        "public_ip": "1.2.3.4",
    }
    (metrics_dir / "2026-09-02.jsonl").write_text(json.dumps(snapshot) + "\n")

    settings = Settings(journeys_dir=journeys_dir)
    client = _client(settings)

    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.json() == [snapshot]


def test_metrics_list_skips_malformed_lines(tmp_path):
    journeys_dir = tmp_path / "journeys"
    metrics_dir = journeys_dir / "metrics"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "2026-09-02.jsonl").write_text(
        "not json\n" + json.dumps({"ts": "t", "hostname": "h", "os": "o"}) + "\n"
    )

    settings = Settings(journeys_dir=journeys_dir)
    client = _client(settings)

    resp = client.get("/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["ts"] == "t"
    assert body[0]["hostname"] == "h"
    assert body[0]["os"] == "o"


def test_metrics_list_skips_snapshots_missing_required_fields(tmp_path):
    """A snapshot missing ``ts``/``os`` (older probe payloads) must not
    500 the whole listing — it's dropped, valid snapshots still come back."""
    journeys_dir = tmp_path / "journeys"
    metrics_dir = journeys_dir / "metrics"
    metrics_dir.mkdir(parents=True)
    valid = {"ts": "t", "hostname": "h", "os": "o"}
    (metrics_dir / "2026-09-02.jsonl").write_text(
        json.dumps({"hostname": "probe", "os": "linux"}) + "\n"  # missing ts
        + json.dumps({"ts": "t2", "hostname": "probe2"}) + "\n"  # missing os
        + json.dumps(valid) + "\n"
    )

    settings = Settings(journeys_dir=journeys_dir)
    client = _client(settings)

    resp = client.get("/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["ts"] == "t"
    assert body[0]["hostname"] == "h"
    assert body[0]["os"] == "o"


def test_openapi_schema_is_generated():
    client = _client(Settings())
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    assert "/journeys" in resp.json()["paths"]


def test_protected_route_requires_api_key_when_configured():
    client = _client(Settings(api_key="sk-test"))
    resp = client.get("/journeys")
    assert resp.status_code == 401


def test_protected_route_accepts_correct_bearer_token():
    client = _client(Settings(api_key="sk-test"))
    resp = client.get("/journeys", headers={"Authorization": "Bearer sk-test"})
    assert resp.status_code == 200


def test_protected_route_rejects_wrong_bearer_token():
    client = _client(Settings(api_key="sk-test"))
    resp = client.get("/journeys", headers={"Authorization": "Bearer sk-wrong"})
    assert resp.status_code == 401


def test_health_stays_open_even_when_api_key_configured():
    client = _client(Settings(api_key="sk-test"))
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_protected_route_open_by_default_when_no_api_key_configured(monkeypatch):
    monkeypatch.delenv("ODYSSEY_API_AUTH_KEY", raising=False)
    client = _client(Settings())
    resp = client.get("/journeys")
    assert resp.status_code == 200
