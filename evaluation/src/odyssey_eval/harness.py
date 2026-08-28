"""`harness.py` — item 7.1/7.5: run a benchmark, write a report.

## Design decision: offline scoring, not live provider calls

There is no live model-serving path in this repo (`services/api` is Step 8,
not built) and `training/` never runs inference itself (`soup-cli` is a
config writer, item 5.6). This harness therefore takes a benchmark suite
(`runner.load_benchmark`) and a caller-produced completions file
(`runner.load_completions` — a `soup-cli`-trained model run through any
inference tool, a raw API call, whatever) and scores the pairing. No live
API calls from this repo, no new heavy dependency, deterministic and
unit-testable — the same shape `odyssey sft`/`odyssey dpo` already have
(operate on an already-produced shard, not a live process). If a live
provider-calling harness is wanted later, this repo already has drop-in
provider wrappers (item 0.9) to build it from — not attempted here since
nothing today names a concrete need for it.

## judges.py — deliberately not built

`docs/STRUCTURE.md` names `judges.py` (LLM-as-judge scoring) as part of this
member's file list. It gets the same explicit-deferral treatment items 0.11
(OTel bridge) and 3.5 (LLM augmentation) got before they had a named
consumer: it needs a real LLM dependency in the loop, and nothing today
names a concrete use for it. This is a scope decision, not a silent drop —
`run_benchmark`/`write_report` below are deliberately metric-agnostic (any
`metrics/*.py` module with a `score` function works), so a future
`judges.py` metric slots in the same way `exact_match`/`tool_call_accuracy`
do, with no harness changes needed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from odyssey_eval.runner import RunResult, TaskResult, compare_runs, run_benchmark

__all__ = ["run_and_report", "write_report", "write_compare_report"]

_DEFAULT_TEMPLATE = """# {benchmark} — eval report

- metric: {metric}
- mean score: {mean_score:.4f}
- tasks: {num_tasks} ({num_missing} missing completion)

## Per-task scores

{task_lines}
"""


def _render(template: str, run: RunResult) -> str:
    task_lines = "\n".join(
        f"- {t.task_id}: {t.score:.4f}" + (" (missing)" if t.missing else "")
        for t in run.tasks
    )
    return template.format(
        benchmark=run.benchmark_name,
        metric=run.metric_name,
        mean_score=run.mean_score,
        num_tasks=len(run.tasks),
        num_missing=sum(1 for t in run.tasks if t.missing),
        task_lines=task_lines or "(no tasks)",
    )


def write_report(
    run: RunResult,
    reports_dir: Path | str,
    *,
    template_path: Path | str | None = None,
) -> Dict[str, Path]:
    """Write ``{reports_dir}/{benchmark}.json`` (raw data, for `eval
    compare`) and ``{reports_dir}/{benchmark}.md`` (human-facing, from
    ``template_path`` or the built-in default). `evaluation/reports/` is
    gitignored (`.gitkeep` only) — every report here is generated output,
    same treatment `training/checkpoints/` gets per ADR 0002."""
    out_dir = Path(reports_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / f"{run.benchmark_name}.json"
    json_path.write_text(
        json.dumps(run.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    template = (
        Path(template_path).read_text(encoding="utf-8")
        if template_path
        else _DEFAULT_TEMPLATE
    )
    md_path = out_dir / f"{run.benchmark_name}.md"
    md_path.write_text(_render(template, run), encoding="utf-8")

    return {"json": json_path, "markdown": md_path}


def run_and_report(
    benchmark_path: Path | str,
    completions_path: Path | str,
    metrics_root: Path | str,
    reports_dir: Path | str,
    *,
    template_path: Path | str | None = None,
) -> Dict[str, Any]:
    """`eval run` (item 7.1) end to end: score + write both report files."""
    run = run_benchmark(benchmark_path, completions_path, metrics_root)
    paths = write_report(run, reports_dir, template_path=template_path)
    return {"run": run, "paths": paths}


def write_compare_report(
    report_a_path: Path | str, report_b_path: Path | str, reports_dir: Path | str
) -> Path:
    """`eval compare` (item 7.1): diff two previously written `*.json`
    reports, write ``{reports_dir}/{a}-vs-{b}.json``."""
    a_data = json.loads(Path(report_a_path).read_text(encoding="utf-8"))
    b_data = json.loads(Path(report_b_path).read_text(encoding="utf-8"))

    a = RunResult(
        benchmark_name=a_data["benchmark"],
        metric_name=a_data["metric"],
        tasks=[
            TaskResult(task_id=t["id"], score=t["score"], missing=t["missing"])
            for t in a_data["tasks"]
        ],
    )
    b = RunResult(
        benchmark_name=b_data["benchmark"],
        metric_name=b_data["metric"],
        tasks=[
            TaskResult(task_id=t["id"], score=t["score"], missing=t["missing"])
            for t in b_data["tasks"]
        ],
    )
    diff = compare_runs(a, b)

    out_dir = Path(reports_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{a.benchmark_name}-vs-{b.benchmark_name}.json"
    out_path.write_text(
        json.dumps(diff, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return out_path
