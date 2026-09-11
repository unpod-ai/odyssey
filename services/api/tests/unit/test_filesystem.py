from __future__ import annotations

from odyssey_api.repositories.filesystem import is_date_dir


def test_is_date_dir_true_for_iso_date(tmp_path):
    d = tmp_path / "2026-08-28"
    d.mkdir()
    assert is_date_dir(d) is True


def test_is_date_dir_false_for_non_date_name(tmp_path):
    d = tmp_path / "metrics"
    d.mkdir()
    assert is_date_dir(d) is False


def test_is_date_dir_false_for_file(tmp_path):
    f = tmp_path / "2026-08-28"
    f.write_text("not a directory")
    assert is_date_dir(f) is False
