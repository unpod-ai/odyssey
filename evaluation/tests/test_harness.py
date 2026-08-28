"""report writing (items 7.1/7.5)."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from odyssey_eval.harness import run_and_report, write_compare_report


def _write_benchmark(tmp_path) -> Path:
    path = tmp_path / "bench.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "example",
                "metric": "exact_match",
                "tasks": [{"id": "t1", "prompt": "2+2?", "reference": "4"}],
            }
        )
    )
    return path


def _metrics_root() -> Path:
    return Path(__file__).parent.parent / "metrics"


def test_run_and_report_writes_json_and_markdown(tmp_path):
    benchmark = _write_benchmark(tmp_path)
    completions = tmp_path / "completions.jsonl"
    completions.write_text('{"id": "t1", "response": "4"}\n')
    reports_dir = tmp_path / "reports"

    result = run_and_report(benchmark, completions, _metrics_root(), reports_dir)

    json_path = result["paths"]["json"]
    md_path = result["paths"]["markdown"]
    assert json_path.exists() and md_path.exists()
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["mean_score"] == 1.0
    assert "example" in md_path.read_text(encoding="utf-8")


def test_write_compare_report(tmp_path):
    benchmark = _write_benchmark(tmp_path)
    metrics_root = _metrics_root()
    reports_dir = tmp_path / "reports"

    completions_a = tmp_path / "a.jsonl"
    completions_a.write_text('{"id": "t1", "response": "4"}\n')
    result_a = run_and_report(benchmark, completions_a, metrics_root, reports_dir)

    completions_b = tmp_path / "b.jsonl"
    completions_b.write_text('{"id": "t1", "response": "5"}\n')
    result_b = run_and_report(benchmark, completions_b, metrics_root, reports_dir / "b")

    diff_path = write_compare_report(
        result_a["paths"]["json"], result_b["paths"]["json"], reports_dir
    )
    diff = json.loads(diff_path.read_text(encoding="utf-8"))
    assert diff["mean_score_delta"] == -1.0
