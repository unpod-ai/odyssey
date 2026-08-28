"""Eval-runs use-case — reads the ``*.json`` reports `odyssey eval run`
already writes (`odyssey_eval.runner.RunResult.to_dict()`'s own shape:
``{"benchmark", "metric", "mean_score", "tasks"}``), not a second copy of
run results."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from odyssey_api.repositories import filesystem

__all__ = ["list_eval_runs"]


def list_eval_runs(reports_dir: Path) -> List[Dict[str, Any]]:
    out = []
    for path in filesystem.list_eval_reports(reports_dir):
        doc = json.loads(path.read_text(encoding="utf-8"))
        out.append(
            {
                "benchmark_name": doc["benchmark"],
                "metric_name": doc["metric"],
                "mean_score": doc["mean_score"],
                "report_path": str(path),
            }
        )
    return out
