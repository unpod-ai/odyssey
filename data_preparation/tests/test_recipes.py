"""Recipes: declarative pipeline config, hashed (items 3.9/4.4)."""

from __future__ import annotations

import pytest

from odyssey_dataprep.recipes import Recipe, RecipeStage, load_recipe, recipe_hash


def write_recipe(tmp_path, text):
    path = tmp_path / "recipe.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_recipe_parses_stages_in_order(tmp_path):
    path = write_recipe(
        tmp_path,
        """
        name: default
        version: 1
        stages:
          - stage: normalization
            params:
              format: openai_chat
          - stage: cleaning
        """,
    )
    recipe = load_recipe(path)
    assert recipe == Recipe(
        name="default",
        version=1,
        stages=[
            RecipeStage(stage="normalization", params={"format": "openai_chat"}),
            RecipeStage(stage="cleaning", params={}),
        ],
    )


def test_load_recipe_requires_name(tmp_path):
    path = write_recipe(tmp_path, "stages:\n  - stage: normalization\n")
    with pytest.raises(ValueError, match="name"):
        load_recipe(path)


def test_load_recipe_requires_nonempty_stages(tmp_path):
    path = write_recipe(tmp_path, "name: default\nstages: []\n")
    with pytest.raises(ValueError, match="stages"):
        load_recipe(path)


def test_recipe_hash_is_stable_and_deterministic(tmp_path):
    a = Recipe(name="x", stages=[RecipeStage(stage="cleaning", params={"drop": True})])
    b = Recipe(name="x", stages=[RecipeStage(stage="cleaning", params={"drop": True})])
    assert recipe_hash(a) == recipe_hash(b)


def test_recipe_hash_changes_with_stage_order():
    a = Recipe(
        name="x",
        stages=[RecipeStage(stage="cleaning"), RecipeStage(stage="augmentation")],
    )
    b = Recipe(
        name="x",
        stages=[RecipeStage(stage="augmentation"), RecipeStage(stage="cleaning")],
    )
    assert recipe_hash(a) != recipe_hash(b)


def test_recipe_hash_changes_with_params():
    a = Recipe(name="x", stages=[RecipeStage(stage="cleaning", params={"drop": True})])
    b = Recipe(name="x", stages=[RecipeStage(stage="cleaning", params={"drop": False})])
    assert recipe_hash(a) != recipe_hash(b)
