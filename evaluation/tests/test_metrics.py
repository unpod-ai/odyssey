"""The built-in metric modules themselves (item 7.3), loaded the same way
`runner.load_metric` does."""

from __future__ import annotations

from pathlib import Path

from odyssey_eval.runner import load_metric

METRICS_ROOT = Path(__file__).parent.parent / "metrics"


def test_exact_match():
    score = load_metric("exact_match", METRICS_ROOT)
    assert score("4", "4") == 1.0
    assert score(" 4 \n", "4") == 1.0
    assert score("5", "4") == 0.0


def test_tool_call_accuracy_no_calls_scores_one():
    score = load_metric("tool_call_accuracy", METRICS_ROOT)
    assert score({"tool_error_rate": None}) == 1.0


def test_tool_call_accuracy_uses_error_rate():
    score = load_metric("tool_call_accuracy", METRICS_ROOT)
    assert score({"tool_error_rate": 0.25}) == 0.75
    assert score({"tool_error_rate": 1.0}, {"anything": True}) == 0.0
