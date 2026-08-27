"""Flows: sequence recipe stages over real directories (item 3.8)."""

from __future__ import annotations

from odyssey.primitives import JourneyEvent, Message, Terminal
from odyssey.spool import Spool, SpoolConfig

from odyssey_dataprep.flows import run_recipe
from odyssey_dataprep.recipes import Recipe, RecipeStage


def ev(jid, seq, **kw):
    return JourneyEvent(journey_id=jid, seq=seq, event_id=f"{jid}-{seq}", **kw)


def seed_spool(root, jid):
    spool = Spool(SpoolConfig(root=root))
    spool.record(ev(jid, 0, kind="message", message=Message(role="user", content="hi")))
    spool.record(
        ev(jid, 1, kind="message", message=Message(role="assistant", content="hello"))
    )
    spool.record(
        ev(jid, 2, kind="terminal", terminal=Terminal(termination_reason="ENV_DONE"))
    )
    spool.close()


def test_run_recipe_chains_collection_through_cleaning(tmp_path):
    seed_spool(tmp_path / ".odyssey", "j1")

    recipe = Recipe(
        name="basic",
        stages=[
            RecipeStage(stage="collection", params={"source": "spool"}),
            RecipeStage(stage="normalization"),
            RecipeStage(stage="cleaning"),
        ],
    )
    result = run_recipe(recipe, tmp_path / ".odyssey", tmp_path / "work")
    assert result.ok
    assert result.stage_results["collection"].count == 1
    assert result.stage_results["normalization"].count == 1
    assert result.stage_results["cleaning"].count == 1
    assert list((tmp_path / "work" / "cleaning").glob("*.json"))


def test_run_recipe_stops_before_next_stage_on_validation_breach(tmp_path):
    seed_spool(tmp_path / ".odyssey", "j1")

    # A journey missing task.conversation_id fails schema validation.
    raw_dir = tmp_path / "bad_normalized"
    raw_dir.mkdir()
    (raw_dir / "bad.json").write_text('{"steps": "not-a-list"}', encoding="utf-8")

    recipe = Recipe(
        name="gated",
        stages=[
            RecipeStage(stage="validation"),
            RecipeStage(stage="splitting"),
        ],
    )
    result = run_recipe(recipe, raw_dir, tmp_path / "work")
    assert not result.ok
    assert result.failed_stage == "validation"
    assert "splitting" not in result.stage_results


def test_run_recipe_validation_gate_does_not_advance_working_dir(tmp_path):
    import json

    good_dir = tmp_path / "normalized"
    good_dir.mkdir()
    (good_dir / "a.json").write_text(
        json.dumps({"task": {"conversation_id": "a"}, "steps": []}), encoding="utf-8"
    )

    recipe = Recipe(
        name="pass_through",
        stages=[RecipeStage(stage="validation"), RecipeStage(stage="splitting")],
    )
    result = run_recipe(recipe, good_dir, tmp_path / "work")
    assert result.ok
    assert (
        list((tmp_path / "work" / "splitting" / "train").glob("*.json"))
        or list((tmp_path / "work" / "splitting" / "val").glob("*.json"))
        or list((tmp_path / "work" / "splitting" / "test").glob("*.json"))
    )


def test_run_recipe_rejects_splitting_before_the_last_stage(tmp_path):
    recipe = Recipe(
        name="bad_order",
        stages=[RecipeStage(stage="splitting"), RecipeStage(stage="validation")],
    )
    try:
        run_recipe(recipe, tmp_path, tmp_path / "work")
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "splitting" in str(exc)


def test_run_recipe_rejects_unknown_stage(tmp_path):
    recipe = Recipe(name="x", stages=[RecipeStage(stage="not_a_real_stage")])
    try:
        run_recipe(recipe, tmp_path, tmp_path / "work")
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "unknown stage" in str(exc)
