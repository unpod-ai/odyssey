"""prune_dir: retention for the date-partitioned data_dir (items 1.12/2.14)."""

from __future__ import annotations

from datetime import date, timedelta

from odyssey_collector.prune import main, prune_dir


def _make_date_dir(root, days_ago: int, name: str = "j.jsonl") -> None:
    d = root / (date.today() - timedelta(days=days_ago)).isoformat()
    d.mkdir(parents=True)
    (d / name).write_text("x")


def test_prune_deletes_directories_older_than_the_cutoff(tmp_path):
    _make_date_dir(tmp_path, days_ago=40)
    _make_date_dir(tmp_path, days_ago=1)

    deleted = prune_dir(tmp_path, older_than_days=30)
    assert len(deleted) == 1
    assert not deleted[0].exists()
    remaining = [p.name for p in tmp_path.iterdir()]
    assert len(remaining) == 1


def test_prune_dry_run_deletes_nothing(tmp_path):
    _make_date_dir(tmp_path, days_ago=40)
    deleted = prune_dir(tmp_path, older_than_days=30, dry_run=True)
    assert len(deleted) == 1
    assert deleted[0].exists()


def test_prune_leaves_non_date_directories_alone(tmp_path):
    (tmp_path / "not-a-date").mkdir()
    (tmp_path / "not-a-date" / "f").write_text("x")
    deleted = prune_dir(tmp_path, older_than_days=0)
    assert deleted == []
    assert (tmp_path / "not-a-date").exists()


def test_prune_on_a_missing_data_dir_returns_nothing(tmp_path):
    assert prune_dir(tmp_path / "does-not-exist", older_than_days=1) == []


def test_main_prints_deleted_nothing_when_nothing_qualifies(tmp_path, capsys):
    rc = main(["--data-dir", str(tmp_path), "--older-than-days", "30"])
    assert rc == 0
    assert "deleted nothing" in capsys.readouterr().out


def test_main_prints_would_delete_under_dry_run(tmp_path, capsys):
    _make_date_dir(tmp_path, days_ago=40)
    rc = main(["--data-dir", str(tmp_path), "--older-than-days", "30", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "would delete" in out
    assert list(tmp_path.iterdir())
