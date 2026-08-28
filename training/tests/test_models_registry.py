"""models/registry.yaml writer (item 6.1)."""

from __future__ import annotations

import yaml

from odyssey_training.models_registry import next_version, register_model


def _register(tmp_path, **overrides):
    kwargs = dict(
        sha256="a" * 64,
        uri="s3://bucket/checkpoints/exp_0001/",
        base_model="meta-llama/Llama-3.1-8B-Instruct",
        corpus_version="corpus-sha-abc",
    )
    kwargs.update(overrides)
    return register_model(tmp_path / "registry.yaml", "acme-agent", **kwargs)


def test_next_version_starts_at_one_when_the_registry_does_not_exist(tmp_path):
    assert next_version("acme-agent", tmp_path / "registry.yaml") == 1


def test_register_model_writes_name_version_sha_uri_base_corpus(tmp_path):
    entry = _register(tmp_path)
    doc = yaml.safe_load((tmp_path / "registry.yaml").read_text())

    assert entry["version"] == 1
    models = doc["models"]["acme-agent"]
    assert len(models) == 1
    assert models[0] == {
        "version": 1,
        "sha256": "a" * 64,
        "uri": "s3://bucket/checkpoints/exp_0001/",
        "base_model": "meta-llama/Llama-3.1-8B-Instruct",
        "corpus_version": "corpus-sha-abc",
    }


def test_register_model_auto_increments_the_version(tmp_path):
    first = _register(tmp_path)
    second = _register(tmp_path, corpus_version="corpus-sha-def")

    assert first["version"] == 1
    assert second["version"] == 2
    doc = yaml.safe_load((tmp_path / "registry.yaml").read_text())
    assert [m["version"] for m in doc["models"]["acme-agent"]] == [1, 2]


def test_register_model_replaces_same_version_instead_of_duplicating(tmp_path):
    _register(tmp_path, version=1)
    _register(tmp_path, version=1, corpus_version="corpus-sha-updated")

    doc = yaml.safe_load((tmp_path / "registry.yaml").read_text())
    entries = doc["models"]["acme-agent"]
    assert len(entries) == 1
    assert entries[0]["corpus_version"] == "corpus-sha-updated"


def test_register_model_keeps_different_names_independent(tmp_path):
    registry_path = tmp_path / "registry.yaml"
    register_model(
        registry_path,
        "agent-a",
        sha256="a" * 64,
        uri="s3://bucket/a/",
        base_model="base-a",
        corpus_version="cv-a",
    )
    register_model(
        registry_path,
        "agent-b",
        sha256="b" * 64,
        uri="s3://bucket/b/",
        base_model="base-b",
        corpus_version="cv-b",
    )

    doc = yaml.safe_load(registry_path.read_text())
    assert set(doc["models"]) == {"agent-a", "agent-b"}
    assert doc["models"]["agent-a"][0]["version"] == 1
    assert doc["models"]["agent-b"][0]["version"] == 1


def test_next_version_ignores_other_models(tmp_path):
    registry_path = tmp_path / "registry.yaml"
    register_model(
        registry_path,
        "agent-a",
        sha256="a" * 64,
        uri="s3://bucket/a/",
        base_model="base-a",
        corpus_version="cv-a",
    )
    assert next_version("agent-b", registry_path) == 1
