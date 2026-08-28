"""`runner.py` — item 7.3's orchestration half: load a benchmark suite +
a caller-produced completions file + a metric, score, aggregate.

Benchmark suites (`evaluation/benchmarks/*.yaml`) are TRACKED (per
`docs/STRUCTURE.md`) suite definitions — task prompts plus a reference
answer, and which metric to score them with. Completions are *not*
produced here (see `harness.py`'s module docstring for why this member
never calls a model) — the caller runs whatever inference path they like
and hands this a JSONL of ``{"id": ..., "response": ...}`` rows.
"""

from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List

import yaml  # pyrefly: ignore[missing-import]

__all__ = [
    "TaskResult",
    "RunResult",
    "load_benchmark",
    "load_completions",
    "load_metric",
    "run_benchmark",
    "compare_runs",
]


@dataclass(frozen=True)
class TaskResult:
    task_id: str
    score: float
    missing: bool = False


@dataclass(frozen=True)
class RunResult:
    benchmark_name: str
    metric_name: str
    tasks: List[TaskResult] = field(default_factory=list)

    @property
    def mean_score(self) -> float:
        if not self.tasks:
            return 0.0
        return sum(t.score for t in self.tasks) / len(self.tasks)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "benchmark": self.benchmark_name,
            "metric": self.metric_name,
            "mean_score": self.mean_score,
            "tasks": [
                {"id": t.task_id, "score": t.score, "missing": t.missing}
                for t in self.tasks
            ],
        }


def load_benchmark(path: Path | str) -> Dict[str, Any]:
    """Parse a `evaluation/benchmarks/*.yaml` suite def. Required keys:
    ``name``, ``metric``, ``tasks`` (each a ``{id, prompt, reference}``)."""
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or "tasks" not in doc:
        raise ValueError(f"{path}: not a valid benchmark suite (missing 'tasks')")
    return doc


def load_completions(path: Path | str) -> Dict[str, str]:
    """A `{"id": ..., "response": ...}` JSONL, keyed by id."""
    completions: Dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            completions[row["id"]] = row["response"]
    return completions


def load_metric(name: str, metrics_root: Path | str) -> Callable[..., float]:
    """Load ``{metrics_root}/{name}.py``'s ``score`` function.

    `evaluation/metrics/` is tracked implementation code, deliberately kept
    outside the installable `odyssey_eval` package (see
    `docs/STRUCTURE.md`'s "code, not numbers" note) so a new metric can be
    added without a package release — loaded here via `importlib`, not a
    plugin-discovery mechanism, since there's no named need for one yet.
    """
    path = Path(metrics_root) / f"{name}.py"
    if not path.is_file():
        raise FileNotFoundError(f"no metric {name!r} at {path}")
    spec = importlib.util.spec_from_file_location(f"odyssey_eval_metric_{name}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load metric module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    score_fn = getattr(module, "score", None)
    if score_fn is None:
        raise AttributeError(f"{path} has no 'score' function")
    return score_fn


def run_benchmark(
    benchmark_path: Path | str,
    completions_path: Path | str,
    metrics_root: Path | str,
) -> RunResult:
    """Score every task in a benchmark against a completions file.

    A task with no matching completion id scores 0.0 and is flagged
    ``missing`` — silently skipping it would let coverage gaps hide as a
    higher score."""
    benchmark = load_benchmark(benchmark_path)
    completions = load_completions(completions_path)
    metric = load_metric(benchmark["metric"], metrics_root)

    # `run_benchmark` is the text-completion path (`response: str`, e.g.
    # `exact_match`). `tool_call_accuracy` scores a captured journey's own
    # metrics dict directly (see its module docstring) — call
    # `load_metric("tool_call_accuracy", ...)` yourself for that shape
    # rather than routing it through this JSONL-of-strings loader.
    results: List[TaskResult] = []
    for task in benchmark["tasks"]:
        task_id = task["id"]
        response = completions.get(task_id)
        if response is None:
            results.append(TaskResult(task_id=task_id, score=0.0, missing=True))
            continue
        results.append(
            TaskResult(
                task_id=task_id,
                score=metric(response, task.get("reference")),
            )
        )
    return RunResult(
        benchmark_name=benchmark["name"], metric_name=benchmark["metric"], tasks=results
    )


def compare_runs(a: RunResult, b: RunResult) -> Dict[str, Any]:
    """Diff two `RunResult`s (item 7's `eval compare`) — per-task and
    overall score deltas, ``b`` relative to ``a``."""
    a_scores = {t.task_id: t.score for t in a.tasks}
    b_scores = {t.task_id: t.score for t in b.tasks}
    task_ids = sorted(set(a_scores) | set(b_scores))
    deltas = {tid: b_scores.get(tid, 0.0) - a_scores.get(tid, 0.0) for tid in task_ids}
    return {
        "a": a.to_dict(),
        "b": b.to_dict(),
        "mean_score_delta": b.mean_score - a.mean_score,
        "task_deltas": deltas,
    }
