"""Tests for odyssey.build.reward."""

from __future__ import annotations

import pytest

from odyssey.builders.reward import build_reward_from_scalar


def test_scalar_default_range():
    reward = build_reward_from_scalar(0.75)
    assert reward.components[0].name == "score"
    assert reward.components[0].value == 0.75
    assert reward.components[0].scaled_value == 0.75
    assert reward.aggregated_value == 0.75


def test_scalar_with_custom_range_and_name():
    reward = build_reward_from_scalar(4.0, name="quality", score_range=(1.0, 5.0))
    assert reward.components[0].name == "quality"
    assert reward.components[0].scaled_value == 0.75
    assert reward.aggregated_value == 0.75


def test_scalar_rejects_inverted_range():
    with pytest.raises(ValueError):
        build_reward_from_scalar(1.0, score_range=(5.0, 1.0))
