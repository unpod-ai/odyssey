"""frozen eval set manifests/registry/cards (item 7.2)."""

from __future__ import annotations

import yaml

from odyssey_eval.eval_datasets import (
    build_manifest,
    next_version,
    update_registry,
    write_card,
    write_manifest,
)


def test_next_version_starts_at_one(tmp_path):
    assert next_version("my-eval", tmp_path) == 1


def test_build_write_manifest_no_recipe_hash_or_watermark(tmp_path):
    shard = tmp_path / "shard.jsonl"
    shard.write_text('{"id": "t1"}\n{"id": "t2"}\n')

    manifest = build_manifest("my-eval", tmp_path / "manifests", shard_paths=[shard])

    assert manifest["name"] == "my-eval"
    assert manifest["version"] == 1
    assert "recipe_hash" not in manifest
    assert "curated_watermark" not in manifest
    assert manifest["shards"][0]["rows"] == 2

    path = write_manifest(manifest, tmp_path / "manifests")
    assert path.exists()

    # a second build against the same name mints v2, not a v1 collision
    manifest2 = build_manifest("my-eval", tmp_path / "manifests", shard_paths=[shard])
    assert manifest2["version"] == 2


def test_update_registry_is_idempotent(tmp_path):
    shard = tmp_path / "shard.jsonl"
    shard.write_text('{"id": "t1"}\n')
    manifest = build_manifest("my-eval", tmp_path / "manifests", shard_paths=[shard])
    manifest_path = write_manifest(manifest, tmp_path / "manifests")

    registry_path = tmp_path / "registry.yaml"
    update_registry(registry_path, "my-eval", manifest_path)
    update_registry(registry_path, "my-eval", manifest_path)

    doc = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    assert len(doc["eval_sets"]["my-eval"]) == 1


def test_write_card_documents_frozen_property(tmp_path):
    shard = tmp_path / "shard.jsonl"
    shard.write_text('{"id": "t1"}\n')
    manifest = build_manifest("my-eval", tmp_path / "manifests", shard_paths=[shard])
    write_manifest(manifest, tmp_path / "manifests")

    path = write_card(
        manifest,
        tmp_path / "cards",
        license="MIT",
        intended_use="regression testing",
        provenance="hand-built",
    )
    text = path.read_text(encoding="utf-8")
    assert "never trained on" in text
    assert "hand-built" in text
