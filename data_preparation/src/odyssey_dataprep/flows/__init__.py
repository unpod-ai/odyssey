"""Flows — item 3.8: sequence a `recipes/*.yaml` (3.9) over the stages that
now actually exist. Deliberately not Prefect: nothing here has scheduling,
retries, or a UI requirement that would justify a new dependency, and this
project's own rule for a dependency nothing needs is "a phantom dep; the
change that needs one adds it" (`packages/odyssey-core/pyproject.toml`'s
own words). A stdlib sequencer over real functions is what this stage
means until that changes.

`collection`, `normalization`, `cleaning`, `validation`, `splitting` are
wired — each reads the previous stage's output directory and writes its
own. `annotation` and `augmentation` are deliberately not: their contracts
do not fit a plain "one directory in, one directory out" chain (annotation
needs a human decisions file; augmentation adds synthetic journeys rather
than replacing the directory), so wiring them into an automatic sequence
would either force a fit or lie about what ran. Call them directly, outside
a recipe, the way `odyssey data card` already calls `datasets.write_card`
outside `build-corpus`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict

from odyssey_dataprep.recipes import Recipe

__all__ = ["FlowResult", "run_recipe"]


def _run_collection(
    src_dir: Path, out_dir: Path, *, source: str = "spool", **_: Any
) -> Any:
    from odyssey_dataprep.collection import collect_from_collector, collect_from_spool

    fn = collect_from_spool if source == "spool" else collect_from_collector
    return fn(src_dir, out_dir)


def _run_normalization(src_dir: Path, out_dir: Path, **_: Any) -> Any:
    from odyssey_dataprep.normalization import normalize_odyssey_dir

    return normalize_odyssey_dir(src_dir, out_dir)


def _run_cleaning(src_dir: Path, out_dir: Path, **_: Any) -> Any:
    from odyssey_dataprep.cleaning import clean_dir

    return clean_dir(src_dir, out_dir)


def _run_validation(src_dir: Path, out_dir: Path, **_: Any) -> Any:
    from odyssey_dataprep.validation import validate_dir

    return validate_dir(src_dir)


def _run_splitting(src_dir: Path, out_dir: Path, **params: Any) -> Any:
    from odyssey_dataprep.splitting import split_dir

    return split_dir(src_dir, out_dir, **params)


STAGE_REGISTRY: Dict[str, Callable[..., Any]] = {
    "collection": _run_collection,
    "normalization": _run_normalization,
    "cleaning": _run_cleaning,
    "validation": _run_validation,
    "splitting": _run_splitting,
}


@dataclass(frozen=True)
class FlowResult:
    stage_results: Dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    failed_stage: str | None = None


def run_recipe(
    recipe: Recipe, raw_dir: Path | str, work_root: Path | str
) -> FlowResult:
    """Run every stage in ``recipe.stages`` in order.

    ``validation`` is a gate, not a transform: it does not advance the
    working directory, and a breach (``ValidationResult.ok is False``)
    stops the run before the next stage — a validation breach is a lineage
    violation (ADR 0003), not a warning to note and continue past.
    ``splitting`` fans out into ``train/``/``val/``/``test/`` rather than
    one directory a later stage could read, so it must be the last stage in
    the recipe.
    """
    current_dir = Path(raw_dir)
    stage_results: Dict[str, Any] = {}

    for i, stage in enumerate(recipe.stages):
        fn = STAGE_REGISTRY.get(stage.stage)
        if fn is None:
            raise ValueError(
                f"unknown stage {stage.stage!r}; expected one of {sorted(STAGE_REGISTRY)}"
            )
        if stage.stage == "splitting" and i != len(recipe.stages) - 1:
            raise ValueError(
                "'splitting' fans out into train/val/test; it must be the last stage"
            )

        out_dir = Path(work_root) / stage.stage
        result = fn(current_dir, out_dir, **stage.params)
        stage_results[stage.stage] = result

        if stage.stage == "validation":
            if not getattr(result, "ok", True):
                return FlowResult(
                    stage_results=stage_results, ok=False, failed_stage=stage.stage
                )
            continue  # a gate does not advance the working directory

        current_dir = out_dir

    return FlowResult(stage_results=stage_results, ok=True)
