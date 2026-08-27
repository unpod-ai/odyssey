"""Recipes — item 3.9: a declarative, hashable pipeline config.

A recipe names which `data_preparation` stages ran and with what parameters
— "processed which way," the half of the corpus-version formula
(`design.md` Decision 9) that `curated_watermark` does not answer:

    corpus version = sha(recipe_hash + curated_watermark)

Deliberately minimal: this module does not execute a recipe (no stage in
`data_preparation` besides `normalization` exists yet to execute against —
see `docs/WORKING.md` Step 3), only defines its shape and hashes it. A
runner belongs with `flows/` (item 3.8) once there is more than one real
stage to sequence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from odyssey.hashing import content_hash

__all__ = ["RecipeStage", "Recipe", "load_recipe", "recipe_to_dict", "recipe_hash"]


@dataclass(frozen=True)
class RecipeStage:
    """One step of the pipeline: a `data_preparation` stage name plus its params.

    ``stage`` is a free-form name, not validated against the stages that
    happen to exist yet — a recipe can be authored before its stage lands,
    the same "declare now, implement later" precedent
    `docs/adr/0004-capture-layer.md` Decision 4 already established for
    fields, not just code.
    """

    stage: str
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Recipe:
    """A named, versioned, ordered list of :class:`RecipeStage`."""

    name: str
    stages: List[RecipeStage]
    version: int = 1


def load_recipe(path: Path | str) -> Recipe:
    """Parse a `*.yaml` recipe file into a :class:`Recipe`.

    Import is local — like every optional dependency in this codebase
    (`typer` in `cli/`, `openai` in `integrations/openai.py`) — so importing
    this module costs nothing for callers who only want :func:`recipe_hash`
    over an already-built :class:`Recipe`.
    """
    import yaml  # pyrefly: ignore[missing-import]

    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a YAML mapping at the top level")

    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(f"{path}: 'name' is required and must be a non-empty string")

    stages_raw = raw.get("stages")
    if not isinstance(stages_raw, list) or not stages_raw:
        raise ValueError(f"{path}: 'stages' is required and must be a non-empty list")

    stages: List[RecipeStage] = []
    for i, entry in enumerate(stages_raw):
        if not isinstance(entry, dict) or not isinstance(entry.get("stage"), str):
            raise ValueError(
                f"{path}: stages[{i}] must be a mapping with a 'stage' name"
            )
        params = entry.get("params", {})
        if not isinstance(params, dict):
            raise ValueError(f"{path}: stages[{i}].params must be a mapping")
        stages.append(RecipeStage(stage=entry["stage"], params=params))

    version = raw.get("version", 1)
    if not isinstance(version, int):
        raise ValueError(f"{path}: 'version' must be an integer")

    return Recipe(name=name, stages=stages, version=version)


def recipe_to_dict(recipe: Recipe) -> Dict[str, Any]:
    """The canonical dict a recipe hashes over — order-sensitive.

    Unlike `curated_watermark`'s hash (Decision 9), stage order is part of
    the recipe's meaning — swapping `cleaning` and `augmentation` is a
    different pipeline — so this is a plain nested structure, not sorted.
    """
    return {
        "name": recipe.name,
        "version": recipe.version,
        "stages": [{"stage": s.stage, "params": s.params} for s in recipe.stages],
    }


def recipe_hash(recipe: Recipe) -> str:
    """Hash a recipe with the existing `odyssey.hashing.content_hash` — reuse,
    not a new primitive, per `design.md` Decision 9's own framing of
    `recipe_hash`."""
    return content_hash(recipe_to_dict(recipe))
