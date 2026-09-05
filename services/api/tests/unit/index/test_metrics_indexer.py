from __future__ import annotations

import json

from odyssey_api.index.metrics_indexer import index_metrics

from odyssey_store.db import connect


def _snapshot(ts, hostname, project=None):
    return {"ts": ts, "hostname": hostname, "os": "Linux", "project": project}


def test_index_metrics_inserts_rows(tmp_path):
    metrics_dir = tmp_path / "journeys" / "metrics"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "2026-09-05.jsonl").write_text(
        json.dumps(_snapshot("t1", "h1"))
        + "\n"
        + json.dumps(_snapshot("t2", "h2"))
        + "\n"
    )
    conn = connect(f"sqlite:///{tmp_path}/db.sqlite3")

    count = index_metrics(conn, tmp_path / "journeys")

    assert count == 2
    assert conn.execute("SELECT COUNT(*) FROM metrics_snapshots").fetchone()[0] == 2


def test_index_metrics_tails_appended_lines_only(tmp_path):
    metrics_dir = tmp_path / "journeys" / "metrics"
    metrics_dir.mkdir(parents=True)
    shard = metrics_dir / "2026-09-05.jsonl"
    shard.write_text(json.dumps(_snapshot("t1", "h1")) + "\n")
    conn = connect(f"sqlite:///{tmp_path}/db.sqlite3")
    index_metrics(conn, tmp_path / "journeys")

    with open(shard, "a") as f:
        f.write(json.dumps(_snapshot("t2", "h2")) + "\n")
    second_count = index_metrics(conn, tmp_path / "journeys")

    assert second_count == 1
    assert conn.execute("SELECT COUNT(*) FROM metrics_snapshots").fetchone()[0] == 2


def test_index_metrics_skips_malformed_line(tmp_path):
    metrics_dir = tmp_path / "journeys" / "metrics"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "2026-09-05.jsonl").write_text(
        "not json\n" + json.dumps(_snapshot("t1", "h1")) + "\n"
    )
    conn = connect(f"sqlite:///{tmp_path}/db.sqlite3")

    count = index_metrics(conn, tmp_path / "journeys")

    assert count == 1


def test_index_metrics_skips_non_dict_json_line(tmp_path):
    """A line that is valid JSON but not an object (e.g. a bare list or
    number) must be skipped like any other malformed line, not raise
    AttributeError out of index_metrics and abort the whole pass."""
    metrics_dir = tmp_path / "journeys" / "metrics"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "2026-09-05.jsonl").write_text(
        "[1, 2, 3]\n" + "42\n" + json.dumps(_snapshot("t1", "h1")) + "\n"
    )
    conn = connect(f"sqlite:///{tmp_path}/db.sqlite3")

    count = index_metrics(conn, tmp_path / "journeys")

    assert count == 1
    assert conn.execute("SELECT COUNT(*) FROM metrics_snapshots").fetchone()[0] == 1


def test_index_metrics_tags_product_slug(tmp_path):
    metrics_dir = tmp_path / "journeys" / "unpod" / "metrics"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "2026-09-05.jsonl").write_text(
        json.dumps(_snapshot("t1", "h1")) + "\n"
    )
    conn = connect(f"sqlite:///{tmp_path}/db.sqlite3")

    index_metrics(conn, tmp_path / "journeys")

    row = conn.execute("SELECT product_slug FROM metrics_snapshots").fetchone()
    assert row["product_slug"] == "unpod"
