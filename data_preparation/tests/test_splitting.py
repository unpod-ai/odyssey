"""Splitting: by group key, never by row (item 3.7)."""

from __future__ import annotations

import json

from odyssey_dataprep.splitting import assign_split, group_key, split_dir


def write_journey(dir_, name, *, trace_id=None):
    doc = {"task": {"conversation_id": name}, "steps": []}
    if trace_id:
        doc["trace_id"] = trace_id
    (dir_ / f"{name}.json").write_text(json.dumps(doc), encoding="utf-8")


def test_group_key_prefers_trace_id():
    assert group_key({"trace_id": "t1", "task": {"conversation_id": "j1"}}) == "t1"


def test_group_key_falls_back_to_conversation_id():
    assert group_key({"task": {"conversation_id": "j1"}}) == "j1"


def test_assign_split_is_deterministic():
    assert assign_split("same-key") == assign_split("same-key")


def test_assign_split_only_returns_configured_names():
    ratios = {"train": 0.5, "test": 0.5}
    for i in range(50):
        assert assign_split(f"k{i}", ratios) in ratios


def test_split_dir_never_splits_a_group_across_two_splits(tmp_path):
    """The property 3.7 explicitly demands a test for."""
    src = tmp_path / "cleaned"
    src.mkdir()
    # Five journeys sharing one trace_id -- a session that must stay together.
    for i in range(5):
        write_journey(src, f"j{i}", trace_id="session-1")

    written = split_dir(src, tmp_path / "splits")
    homes = {
        split_name
        for split_name, paths in written.items()
        for path in paths
        if path.exists()
    }
    assert len(homes) == 1


def test_split_dir_distributes_many_groups_across_all_splits(tmp_path):
    src = tmp_path / "cleaned"
    src.mkdir()
    for i in range(200):
        write_journey(src, f"j{i}", trace_id=f"session-{i}")

    written = split_dir(src, tmp_path / "splits")
    assert all(len(paths) > 0 for paths in written.values())
    assert sum(len(paths) for paths in written.values()) == 200


def test_split_dir_writes_readable_copies(tmp_path):
    src = tmp_path / "cleaned"
    src.mkdir()
    write_journey(src, "j0", trace_id="s0")

    written = split_dir(src, tmp_path / "splits")
    all_paths = [p for paths in written.values() for p in paths]
    assert len(all_paths) == 1
    assert json.loads(all_paths[0].read_text())["task"]["conversation_id"] == "j0"
