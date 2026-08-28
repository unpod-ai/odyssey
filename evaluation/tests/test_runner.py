"""load + score (item 7.3)."""

from __future__ import annotations

import pytest
import yaml

from odyssey_eval.runner import (
    compare_runs,
    load_benchmark,
    load_completions,
    load_metric,
    run_benchmark,
)


@pytest.fixture
def benchmark_path(tmp_path):
    path = tmp_path / "bench.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "example",
                "metric": "exact_match",
                "tasks": [
                    {"id": "t1", "prompt": "2+2?", "reference": "4"},
                    {"id": "t2", "prompt": "3+3?", "reference": "6"},
                ],
            }
        )
    )
    return path


@pytest.fixture
def metrics_root():
    from pathlib import Path

    return Path(__file__).parent.parent / "metrics"


def test_load_benchmark_requires_tasks(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump({"name": "x"}))
    with pytest.raises(ValueError):
        load_benchmark(path)


def test_load_completions(tmp_path):
    path = tmp_path / "completions.jsonl"
    path.write_text('{"id": "t1", "response": "4"}\n{"id": "t2", "response": "7"}\n')
    completions = load_completions(path)
    assert completions == {"t1": "4", "t2": "7"}


def test_load_metric_exact_match(metrics_root):
    score = load_metric("exact_match", metrics_root)
    assert score("4", "4") == 1.0
    assert score("5", "4") == 0.0


def test_load_metric_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_metric("nope", tmp_path)


def test_run_benchmark_scores_and_flags_missing(benchmark_path, metrics_root, tmp_path):
    completions_path = tmp_path / "completions.jsonl"
    completions_path.write_text('{"id": "t1", "response": "4"}\n')

    result = run_benchmark(benchmark_path, completions_path, metrics_root)

    assert result.benchmark_name == "example"
    assert len(result.tasks) == 2
    t1 = next(t for t in result.tasks if t.task_id == "t1")
    t2 = next(t for t in result.tasks if t.task_id == "t2")
    assert t1.score == 1.0 and not t1.missing
    assert t2.score == 0.0 and t2.missing
    assert result.mean_score == 0.5


def test_compare_runs(benchmark_path, metrics_root, tmp_path):
    completions_a = tmp_path / "a.jsonl"
    completions_a.write_text(
        '{"id": "t1", "response": "4"}\n{"id": "t2", "response": "6"}\n'
    )
    completions_b = tmp_path / "b.jsonl"
    completions_b.write_text(
        '{"id": "t1", "response": "5"}\n{"id": "t2", "response": "6"}\n'
    )

    run_a = run_benchmark(benchmark_path, completions_a, metrics_root)
    run_b = run_benchmark(benchmark_path, completions_b, metrics_root)

    diff = compare_runs(run_a, run_b)
    assert diff["mean_score_delta"] == pytest.approx(-0.5)
    assert diff["task_deltas"]["t1"] == pytest.approx(-1.0)
    assert diff["task_deltas"]["t2"] == pytest.approx(0.0)
