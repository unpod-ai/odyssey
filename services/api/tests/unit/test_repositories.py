"""Filesystem reads against real files on disk (item 8.1/8.2 support layer)."""

from __future__ import annotations

import yaml
from odyssey.jsonl import write_events
from odyssey.primitives import JourneyEvent, JourneyHeader, Message, Terminal

from odyssey_api.repositories import filesystem

JID = "j_repo"
HEADER = JourneyHeader(journey_id=JID, data_source="livekit")


def _events() -> list[JourneyEvent]:
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


def test_list_journeys_empty_dir(tmp_path):
    assert filesystem.list_journeys(tmp_path / "nope") == []


def test_list_journeys_and_find(tmp_path):
    date_dir = tmp_path / "2026-08-28"
    date_dir.mkdir()
    write_events(date_dir / f"{JID}.jsonl", _events(), header=HEADER)

    assert filesystem.list_journeys(tmp_path) == [(JID, "2026-08-28")]
    found = filesystem.find_journey_path(tmp_path, JID)
    assert found == date_dir / f"{JID}.jsonl"
    assert filesystem.find_journey_path(tmp_path, "nope") is None


def test_list_journeys_skips_non_date_dirs(tmp_path):
    """The collector also writes a ``metrics/`` subdirectory of ``.jsonl``
    files directly under the same root; it isn't a date partition and
    must not be mistaken for one (finding: metrics/journeys collision)."""
    date_dir = tmp_path / "2026-08-28"
    date_dir.mkdir()
    write_events(date_dir / f"{JID}.jsonl", _events(), header=HEADER)

    metrics_dir = tmp_path / "metrics"
    metrics_dir.mkdir()
    (metrics_dir / "2026-08-28.jsonl").write_text(
        '{"ts": "2026-08-28T00:00:00Z"}\n', encoding="utf-8"
    )

    assert filesystem.list_journeys(tmp_path) == [(JID, "2026-08-28")]
    assert filesystem.find_journey_path(tmp_path, "2026-08-28") is None


def test_list_journeys_product_scoped_layout(tmp_path):
    """`--products-file` collector deployments nest one more level:
    ``<journeys_dir>/<product_slug>/<date>/<journey_id>.jsonl``."""
    date_dir = tmp_path / "unpod" / "2026-08-28"
    date_dir.mkdir(parents=True)
    write_events(date_dir / f"{JID}.jsonl", _events(), header=HEADER)

    assert filesystem.list_journeys(tmp_path) == [(JID, "2026-08-28")]
    found = filesystem.find_journey_path(tmp_path, JID)
    assert found == date_dir / f"{JID}.jsonl"
    assert filesystem.find_journey_path(tmp_path, "nope") is None


def test_list_journeys_product_scoped_skips_metrics_subdir(tmp_path):
    """A product's own ``metrics/`` subdirectory (``<slug>/metrics/*.jsonl``)
    isn't a date partition and must not be misread as one."""
    date_dir = tmp_path / "unpod" / "2026-08-28"
    date_dir.mkdir(parents=True)
    write_events(date_dir / f"{JID}.jsonl", _events(), header=HEADER)

    metrics_dir = tmp_path / "unpod" / "metrics"
    metrics_dir.mkdir()
    (metrics_dir / "2026-08-28.jsonl").write_text(
        '{"ts": "2026-08-28T00:00:00Z"}\n', encoding="utf-8"
    )

    assert filesystem.list_journeys(tmp_path) == [(JID, "2026-08-28")]


def test_list_journeys_mixed_products(tmp_path):
    """Multiple product-slug directories are all walked."""
    for slug, jid in (("unpod", "j_a"), ("otherpod", "j_b")):
        date_dir = tmp_path / slug / "2026-08-28"
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

    assert sorted(filesystem.list_journeys(tmp_path)) == [
        ("j_a", "2026-08-28"),
        ("j_b", "2026-08-28"),
    ]
    assert filesystem.find_journey_path(tmp_path, "j_a") is not None
    assert filesystem.find_journey_path(tmp_path, "j_b") is not None


def test_list_metrics_empty_dir(tmp_path):
    assert filesystem.list_metrics(tmp_path / "nope") == []


def test_list_metrics_flat_layout(tmp_path):
    metrics_dir = tmp_path / "metrics"
    metrics_dir.mkdir()
    (metrics_dir / "2026-08-28.jsonl").write_text(
        '{"ts": "2026-08-28T00:00:00Z", "host": "a"}\n'
        '{"ts": "2026-08-29T00:00:00Z", "host": "b"}\n',
        encoding="utf-8",
    )

    out = filesystem.list_metrics(tmp_path)
    assert [m["host"] for m in out] == ["b", "a"]  # newest ts first


def test_list_metrics_product_scoped_layout(tmp_path):
    """A product-scoped collector (``--products-file``) writes snapshots to
    ``<journeys_dir>/<product_slug>/metrics/*.jsonl`` instead of
    ``<journeys_dir>/metrics/*.jsonl``."""
    metrics_dir = tmp_path / "unpod" / "metrics"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "2026-08-28.jsonl").write_text(
        '{"ts": "2026-08-28T00:00:00Z", "host": "a"}\n', encoding="utf-8"
    )

    out = filesystem.list_metrics(tmp_path)
    assert [m["host"] for m in out] == ["a"]


def test_list_metrics_pools_flat_and_product_scoped(tmp_path):
    (tmp_path / "metrics").mkdir()
    (tmp_path / "metrics" / "2026-08-28.jsonl").write_text(
        '{"ts": "2026-08-28T00:00:00Z", "host": "flat"}\n', encoding="utf-8"
    )
    (tmp_path / "unpod" / "metrics").mkdir(parents=True)
    (tmp_path / "unpod" / "metrics" / "2026-08-28.jsonl").write_text(
        '{"ts": "2026-08-29T00:00:00Z", "host": "unpod"}\n', encoding="utf-8"
    )

    out = filesystem.list_metrics(tmp_path)
    assert {m["host"] for m in out} == {"flat", "unpod"}


def test_read_registry_missing_file(tmp_path):
    assert filesystem.read_registry(tmp_path / "registry.yaml", "corpora") == {}


def test_read_registry_real_file(tmp_path):
    path = tmp_path / "registry.yaml"
    path.write_text(
        yaml.safe_dump(
            {"corpora": {"c1": [{"version": 1, "manifest_sha256": "a", "uri": "u"}]}}
        )
    )
    doc = filesystem.read_registry(path, "corpora")
    assert doc["c1"][0]["version"] == 1


def test_list_eval_reports(tmp_path):
    (tmp_path / "a.json").write_text("{}")
    (tmp_path / "b.json").write_text("{}")
    assert [p.name for p in filesystem.list_eval_reports(tmp_path)] == [
        "a.json",
        "b.json",
    ]


def test_list_exports(tmp_path):
    shard = tmp_path / "sft.jsonl"
    shard.write_text('{"messages": []}\n{"messages": []}\n')
    exports = filesystem.list_exports(tmp_path)
    assert len(exports) == 1
    assert exports[0]["name"] == "sft.jsonl"
    assert exports[0]["rows"] == 2
    assert len(exports[0]["sha256"]) == 64
