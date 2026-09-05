from __future__ import annotations

from odyssey_api.cli import register
from typer.testing import CliRunner


def _make_app():
    import typer

    app = typer.Typer()
    register(app)
    return app


def test_reindex_command_runs_and_prints_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("ODYSSEY_API_JOURNEYS_DIR", str(tmp_path / "journeys"))
    monkeypatch.setenv("ODYSSEY_DB_URI", f"sqlite:///{tmp_path}/db.sqlite3")
    runner = CliRunner()

    result = runner.invoke(_make_app(), ["reindex"])

    assert result.exit_code == 0
    assert "journeys" in result.stdout.lower()
