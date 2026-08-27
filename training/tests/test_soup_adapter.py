"""soup-cli adapter: odyssey corpus -> soup.yaml (item 5.6)."""

from __future__ import annotations

import json

import pytest
import yaml
from soup_cli.config.schema import SoupConfig

from odyssey_training.soup_adapter import (
    translate_dpo_shard,
    write_dpo_config,
    write_sft_config,
)

BASE = "meta-llama/Llama-3.1-8B-Instruct"


def test_write_sft_config_points_at_the_shard_unchanged(tmp_path):
    shard = tmp_path / "sft.jsonl"
    shard.write_text('{"messages": [{"role": "user", "content": "hi"}]}\n')

    out = write_sft_config(
        base=BASE, train_shard=shard, out_path=tmp_path / "soup.yaml"
    )
    doc = yaml.safe_load(out.read_text())

    assert doc["base"] == BASE
    assert doc["task"] == "sft"
    assert doc["data"] == {"train": str(shard), "format": "chatml"}


def test_write_sft_config_validates_against_the_real_soup_schema(tmp_path):
    shard = tmp_path / "sft.jsonl"
    shard.write_text('{"messages": []}\n')
    out = write_sft_config(
        base=BASE, train_shard=shard, out_path=tmp_path / "soup.yaml"
    )
    # Round-trips through soup-cli's own pydantic model without raising.
    SoupConfig(**yaml.safe_load(out.read_text()))


def test_write_sft_config_merges_training_overrides(tmp_path):
    out = write_sft_config(
        base=BASE,
        train_shard=tmp_path / "s.jsonl",
        out_path=tmp_path / "soup.yaml",
        training={"epochs": 5, "lr": 1e-4},
    )
    doc = yaml.safe_load(out.read_text())
    assert doc["training"] == {"epochs": 5, "lr": 1e-4}


def test_translate_dpo_shard_wraps_chosen_and_rejected_in_lists(tmp_path):
    src = tmp_path / "dpo.jsonl"
    src.write_text(
        json.dumps(
            {
                "conversation_id": "j1",
                "prompt": [{"role": "user", "content": "book me"}],
                "chosen": {"role": "assistant", "content": "booked"},
                "rejected": {"role": "assistant", "content": "no"},
            }
        )
        + "\n"
    )
    out = tmp_path / "dpo.soup.jsonl"

    n = translate_dpo_shard(src, out)
    assert n == 1

    row = json.loads(out.read_text().strip())
    assert row["chosen"] == [{"role": "assistant", "content": "booked"}]
    assert row["rejected"] == [{"role": "assistant", "content": "no"}]
    assert row["prompt"] == [{"role": "user", "content": "book me"}]


def test_translate_dpo_shard_handles_multiple_rows(tmp_path):
    src = tmp_path / "dpo.jsonl"
    rows = [
        {
            "prompt": [],
            "chosen": {"role": "assistant", "content": f"c{i}"},
            "rejected": {"role": "assistant", "content": f"r{i}"},
        }
        for i in range(3)
    ]
    src.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    n = translate_dpo_shard(src, tmp_path / "out.jsonl")
    assert n == 3


def test_write_dpo_config_after_translation_validates(tmp_path):
    src = tmp_path / "dpo.jsonl"
    src.write_text(
        json.dumps(
            {
                "prompt": [{"role": "user", "content": "hi"}],
                "chosen": {"role": "assistant", "content": "good"},
                "rejected": {"role": "assistant", "content": "bad"},
            }
        )
        + "\n"
    )
    translated = tmp_path / "dpo.soup.jsonl"
    translate_dpo_shard(src, translated)

    out = write_dpo_config(
        base=BASE, train_shard=translated, out_path=tmp_path / "soup.yaml"
    )
    doc = yaml.safe_load(out.read_text())
    assert doc["task"] == "dpo"
    assert doc["data"]["format"] == "dpo"
    SoupConfig(**doc)


def test_write_config_rejects_an_unknown_backend(tmp_path):
    with pytest.raises(Exception):
        write_sft_config(
            base=BASE,
            train_shard=tmp_path / "s.jsonl",
            out_path=tmp_path / "soup.yaml",
            backend="not-a-real-backend",
        )
