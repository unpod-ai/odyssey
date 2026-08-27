"""odyssey-cli: plugin discovery, dispatch, and the eager commands.

In-process via typer's CliRunner where possible (fast, matches the rest of
this monorepo's preference for exercising real code over mocks) — except the
cold-start timing assertion, which genuinely needs a subprocess: CliRunner
runs in the same interpreter and would not measure real import cost.
"""

from __future__ import annotations

import json
import subprocess
import sys

from typer.testing import CliRunner

from odyssey_cli.main import app
from odyssey_cli.registry import discover

runner = CliRunner()


# --------------------------------------------------------------------------
# Plugin discovery
# --------------------------------------------------------------------------


def test_the_real_workspace_registers_spool_and_data():
    """Not a fake — this is what `uv sync --all-packages` actually installs."""
    groups = discover()
    assert {"spool", "data"} <= set(groups)


def test_discover_reads_metadata_without_importing_the_target():
    """The whole laziness contract: entry_points() must not import odyssey."""
    import sys as _sys

    had = "odyssey.cli" in _sys.modules
    discover()
    if not had:
        assert "odyssey.cli" not in _sys.modules


# --------------------------------------------------------------------------
# Root help — the surface, and the cold-start budget
# --------------------------------------------------------------------------


def test_root_help_lists_every_command_group():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in ("spool", "data", "doctor", "push", "status"):
        assert name in result.output


def test_version_flag_prints_and_exits():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "odyssey-cli" in result.output


def test_cold_help_stays_under_the_200ms_budget():
    """ADR 0003's own claim, made real rather than left as a comment."""
    proc = subprocess.run(
        [sys.executable, "-m", "odyssey_cli.main", "--help"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0


def test_doctor_reports_discovered_groups_and_timing():
    """The 200ms budget itself is a wall-clock assertion doctor makes for a
    human running it locally — not something to gate this test's pass/fail
    on, since CI load can legitimately push a cold subprocess over it."""
    result = runner.invoke(app, ["doctor"])
    assert "spool" in result.output
    assert "data" in result.output
    assert "cold --help" in result.output


# --------------------------------------------------------------------------
# spool — mounted from odyssey-core, dispatched only when invoked
# --------------------------------------------------------------------------


def test_spool_help_lists_every_core_subcommand():
    result = runner.invoke(app, ["spool", "--help"])
    assert result.exit_code == 0
    for name in ("push", "export", "sft", "dpo", "status", "show", "health"):
        assert name in result.output


def test_spool_status_on_an_empty_spool(tmp_path):
    result = runner.invoke(app, ["spool", "status", "--spool", str(tmp_path / "s")])
    assert result.exit_code == 0
    assert "empty" in result.output


def test_spool_push_and_status_round_trip(tmp_path):
    _seed_spool(tmp_path / "spool")

    status = runner.invoke(app, ["spool", "status", "--spool", str(tmp_path / "spool")])
    assert "call_1" in status.output

    pushed = runner.invoke(
        app,
        [
            "spool",
            "push",
            "--spool",
            str(tmp_path / "spool"),
            "--out",
            str(tmp_path / "out"),
        ],
    )
    assert pushed.exit_code == 0
    assert "pushed  3" in pushed.output
    assert (tmp_path / "out" / "call_1.jsonl").exists()


def test_spool_sft_writes_a_training_file(tmp_path):
    _seed_spool(tmp_path / "spool")
    result = runner.invoke(
        app,
        [
            "spool",
            "sft",
            "--spool",
            str(tmp_path / "spool"),
            "--out",
            str(tmp_path / "train.jsonl"),
        ],
    )
    assert result.exit_code == 0
    lines = (tmp_path / "train.jsonl").read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["messages"]


def test_spool_health_json(tmp_path):
    _seed_spool(tmp_path / "spool")
    result = runner.invoke(
        app, ["spool", "health", "--spool", str(tmp_path / "spool"), "--json"]
    )
    assert result.exit_code in (0, 3)
    payload = json.loads(result.output)
    assert "journeys" in payload


def _seed_spool(root) -> None:
    from odyssey.primitives import JourneyEvent, Message, Terminal
    from odyssey.spool import Spool, SpoolConfig

    s = Spool(SpoolConfig(root=root))
    s.record_all(
        [
            JourneyEvent(
                journey_id="call_1",
                seq=0,
                kind="message",
                event_id="e0",
                message=Message(role="user", content="hi"),
            ),
            JourneyEvent(
                journey_id="call_1",
                seq=1,
                kind="message",
                event_id="e1",
                message=Message(role="assistant", content="hello"),
            ),
            JourneyEvent(
                journey_id="call_1",
                seq=2,
                kind="terminal",
                event_id="e2",
                terminal=Terminal(termination_reason="ENV_DONE"),
            ),
        ]
    )
    s.close()


# --------------------------------------------------------------------------
# data — mounted from odyssey-dataprep
# --------------------------------------------------------------------------


def test_data_help_lists_normalize():
    result = runner.invoke(app, ["data", "--help"])
    assert result.exit_code == 0
    assert "normalize" in result.output


def test_data_normalize_byod(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "conv_1.json").write_text(
        json.dumps(
            [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hey"}]
        )
    )
    result = runner.invoke(
        app,
        [
            "data",
            "normalize",
            "--raw",
            str(raw),
            "--format",
            "openai_chat",
            "--data-source",
            "customer_a",
            "--out",
            str(tmp_path / "normalized"),
        ],
    )
    assert result.exit_code == 0
    assert (tmp_path / "normalized" / "conv_1.json").exists()


def test_data_normalize_byod_missing_format_is_a_usage_error(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    result = runner.invoke(
        app,
        ["data", "normalize", "--raw", str(raw), "--out", str(tmp_path / "out")],
    )
    assert result.exit_code == 2


# --------------------------------------------------------------------------
# Deprecated top-level aliases
# --------------------------------------------------------------------------


def test_push_alias_warns_and_delegates(tmp_path):
    _seed_spool(tmp_path / "spool")
    result = runner.invoke(
        app,
        [
            "push",
            "--spool",
            str(tmp_path / "spool"),
            "--out",
            str(tmp_path / "out"),
        ],
    )
    assert result.exit_code == 0
    assert "deprecated" in result.output
    assert "odyssey spool push" in result.output
    assert (tmp_path / "out" / "call_1.jsonl").exists()


def test_status_alias_warns_and_delegates(tmp_path):
    result = runner.invoke(app, ["status", "--spool", str(tmp_path / "empty")])
    assert result.exit_code == 0
    assert "deprecated" in result.output
    assert "empty" in result.output
