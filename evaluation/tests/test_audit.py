"""manifest sha256 integrity gate (item 7.4 / dataset-audit.yml)."""

from __future__ import annotations

from odyssey_eval.audit import audit_registry, main
from odyssey_eval.eval_datasets import build_manifest, update_registry, write_manifest


def test_audit_registry_missing_file_returns_no_errors(tmp_path):
    assert audit_registry(tmp_path / "nope.yaml", "eval_sets") == []


def test_audit_registry_clean(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    shard = tmp_path / "shard.jsonl"
    shard.write_text('{"id": "t1"}\n')
    manifest = build_manifest("my-eval", "manifests", shard_paths=[shard])
    manifest_path = write_manifest(manifest, "manifests")
    update_registry("registry.yaml", "my-eval", manifest_path)

    assert audit_registry("registry.yaml", "eval_sets") == []


def test_audit_registry_detects_tampered_manifest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    shard = tmp_path / "shard.jsonl"
    shard.write_text('{"id": "t1"}\n')
    manifest = build_manifest("my-eval", "manifests", shard_paths=[shard])
    manifest_path = write_manifest(manifest, "manifests")
    update_registry("registry.yaml", "my-eval", manifest_path)

    manifest_path.write_text("tampered", encoding="utf-8")

    errors = audit_registry("registry.yaml", "eval_sets")
    assert len(errors) == 1
    assert "mismatch" in errors[0]


def test_main_returns_zero_when_no_registries_exist(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main([]) == 0
