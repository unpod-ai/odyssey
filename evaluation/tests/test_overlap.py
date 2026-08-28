"""no-overlap gate (item 7.4)."""

from __future__ import annotations

from odyssey_eval.overlap import check_no_overlap


def _write_journey(path, journey_id):
    path.write_text(f'{{"task": {{"id": "{journey_id}"}}}}\n', encoding="utf-8")


def test_no_overlap_clean(tmp_path):
    eval_dir = tmp_path / "eval"
    train_dir = tmp_path / "train"
    eval_dir.mkdir()
    train_dir.mkdir()
    _write_journey(eval_dir / "j1.json", "j1")
    _write_journey(train_dir / "j2.json", "j2")

    assert check_no_overlap(eval_dir, train_dir) == []


def test_no_overlap_detects_shared_id(tmp_path):
    eval_dir = tmp_path / "eval"
    train_dir = tmp_path / "train"
    eval_dir.mkdir()
    train_dir.mkdir()
    _write_journey(eval_dir / "j1.json", "j1")
    _write_journey(train_dir / "j1.json", "j1")

    errors = check_no_overlap(eval_dir, train_dir)
    assert len(errors) == 1
    assert "j1" in errors[0]
