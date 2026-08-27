"""datasets/: registry + manifests + cards (items 4.6/4.7/4.8)."""

from __future__ import annotations

import json

import yaml

from odyssey_dataprep.datasets import (
    build_manifest,
    next_version,
    update_registry,
    write_card,
    write_manifest,
)

WATERMARK = {"seq": 1, "hash": "wm-hash"}


def make_shard(tmp_path, name, lines):
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_next_version_starts_at_one(tmp_path):
    assert next_version("default", tmp_path / "manifests") == 1


def test_next_version_increments_past_existing(tmp_path):
    root = tmp_path / "manifests"
    (root / "default").mkdir(parents=True)
    (root / "default" / "v1.json").write_text("{}", encoding="utf-8")
    (root / "default" / "v2.json").write_text("{}", encoding="utf-8")
    assert next_version("default", root) == 3


def test_build_manifest_records_shard_hash_and_row_count(tmp_path):
    shard = make_shard(tmp_path, "train.jsonl", ["a", "b", "c"])
    manifest = build_manifest(
        "default",
        tmp_path / "manifests",
        corpus_version="cv1",
        recipe_hash="rh1",
        curated_watermark=WATERMARK,
        shard_paths=[shard],
    )
    assert manifest["version"] == 1
    assert manifest["corpus_version"] == "cv1"
    assert manifest["recipe_hash"] == "rh1"
    assert manifest["curated_watermark"] == WATERMARK
    assert manifest["shards"][0]["rows"] == 3
    assert len(manifest["shards"][0]["sha256"]) == 64


def test_write_manifest_round_trips(tmp_path):
    shard = make_shard(tmp_path, "train.jsonl", ["a"])
    manifest = build_manifest(
        "default",
        tmp_path / "manifests",
        corpus_version="cv1",
        recipe_hash="rh1",
        curated_watermark=WATERMARK,
        shard_paths=[shard],
    )
    path = write_manifest(manifest, tmp_path / "manifests")
    assert path == tmp_path / "manifests" / "default" / "v1.json"
    assert json.loads(path.read_text()) == manifest


def test_update_registry_records_version_sha_and_uri(tmp_path):
    shard = make_shard(tmp_path, "train.jsonl", ["a"])
    manifest = build_manifest(
        "default",
        tmp_path / "manifests",
        corpus_version="cv1",
        recipe_hash="rh1",
        curated_watermark=WATERMARK,
        shard_paths=[shard],
    )
    manifest_path = write_manifest(manifest, tmp_path / "manifests")
    registry_path = update_registry(
        tmp_path / "registry.yaml", "default", manifest_path
    )

    doc = yaml.safe_load(registry_path.read_text())
    entries = doc["corpora"]["default"]
    assert len(entries) == 1
    assert entries[0]["version"] == 1
    assert entries[0]["uri"] == str(manifest_path)
    assert len(entries[0]["manifest_sha256"]) == 64


def test_update_registry_replaces_same_version_instead_of_duplicating(tmp_path):
    shard = make_shard(tmp_path, "train.jsonl", ["a"])
    manifest = build_manifest(
        "default",
        tmp_path / "manifests",
        corpus_version="cv1",
        recipe_hash="rh1",
        curated_watermark=WATERMARK,
        shard_paths=[shard],
    )
    manifest_path = write_manifest(manifest, tmp_path / "manifests")
    registry_path = tmp_path / "registry.yaml"
    update_registry(registry_path, "default", manifest_path)
    update_registry(registry_path, "default", manifest_path)

    doc = yaml.safe_load(registry_path.read_text())
    assert len(doc["corpora"]["default"]) == 1


def test_write_card_includes_provenance_and_policy_fields(tmp_path):
    shard = make_shard(tmp_path, "train.jsonl", ["a", "b"])
    manifest = build_manifest(
        "default",
        tmp_path / "manifests",
        corpus_version="cv1",
        recipe_hash="rh1",
        curated_watermark=WATERMARK,
        shard_paths=[shard],
    )
    path = write_card(
        manifest,
        tmp_path / "cards",
        license="Apache-2.0",
        pii_posture="key-based masking only",
        intended_use="SFT for a booking agent",
    )
    text = path.read_text()
    assert "# default v1" in text
    assert "cv1" in text
    assert "Apache-2.0" in text
    assert "key-based masking only" in text
    assert "SFT for a booking agent" in text
    assert "Not yet split" in text
