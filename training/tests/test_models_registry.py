"""models/registry.yaml writer (item 6.1), model cards (6.2), promote/export
(6.4)."""

from __future__ import annotations

import pytest
import yaml

from odyssey_training.checkpoints import upload_checkpoint
from odyssey_training.models_registry import (
    export_model,
    next_version,
    promote_model,
    register_model,
    resolve_model,
    write_model_card,
)


class FakeS3Client:
    """A minimal boto3 S3 client double, same shape as
    `test_checkpoints.py`'s own — duplicated locally rather than imported
    across test modules so each test file stands alone."""

    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body):
        self.objects[(Bucket, Key)] = Body.read()

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        keys = sorted(
            k for (b, k) in self.objects if b == Bucket and k.startswith(Prefix)
        )
        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}

    def get_object(self, Bucket, Key):
        return {"Body": _FakeBody(self.objects[(Bucket, Key)])}


class _FakeBody:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


def _make_checkpoint(tmp_path):
    ckpt = tmp_path / "checkpoint"
    ckpt.mkdir()
    (ckpt / "config.json").write_text('{"base": "x"}')
    return ckpt


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


def test_resolve_model_by_version(tmp_path):
    registry_path = tmp_path / "registry.yaml"
    entry = _register(tmp_path)
    assert resolve_model(registry_path, "acme-agent", version=1) == entry


def test_resolve_model_requires_exactly_one_of_version_or_alias(tmp_path):
    registry_path = tmp_path / "registry.yaml"
    _register(tmp_path)
    with pytest.raises(ValueError):
        resolve_model(registry_path, "acme-agent")
    with pytest.raises(ValueError):
        resolve_model(registry_path, "acme-agent", version=1, alias="production")


def test_resolve_model_raises_for_an_unknown_version(tmp_path):
    registry_path = tmp_path / "registry.yaml"
    _register(tmp_path)
    with pytest.raises(KeyError):
        resolve_model(registry_path, "acme-agent", version=99)


def test_promote_model_records_an_alias(tmp_path):
    registry_path = tmp_path / "registry.yaml"
    _register(tmp_path)
    result = promote_model(registry_path, "acme-agent", 1)
    assert result == {"name": "acme-agent", "alias": "production", "version": 1}

    doc = yaml.safe_load(registry_path.read_text())
    assert doc["aliases"]["acme-agent"]["production"] == 1


def test_promote_model_supports_a_custom_alias(tmp_path):
    registry_path = tmp_path / "registry.yaml"
    _register(tmp_path)
    promote_model(registry_path, "acme-agent", 1, alias="staging")

    doc = yaml.safe_load(registry_path.read_text())
    assert doc["aliases"]["acme-agent"]["staging"] == 1
    assert "production" not in doc["aliases"]["acme-agent"]


def test_promote_model_rejects_an_unregistered_version(tmp_path):
    registry_path = tmp_path / "registry.yaml"
    _register(tmp_path)
    with pytest.raises(KeyError):
        promote_model(registry_path, "acme-agent", 99)


def test_resolve_model_by_alias_after_promotion(tmp_path):
    registry_path = tmp_path / "registry.yaml"
    _register(tmp_path)
    promote_model(registry_path, "acme-agent", 1, alias="production")

    entry = resolve_model(registry_path, "acme-agent", alias="production")
    assert entry["version"] == 1


def test_resolve_model_raises_for_an_unknown_alias(tmp_path):
    registry_path = tmp_path / "registry.yaml"
    _register(tmp_path)
    with pytest.raises(KeyError):
        resolve_model(registry_path, "acme-agent", alias="production")


def test_write_model_card_includes_provenance_and_policy_fields(tmp_path):
    entry = _register(tmp_path)
    path = write_model_card(
        entry,
        "acme-agent",
        tmp_path / "cards",
        license="Apache-2.0",
        intended_use="internal support triage",
        limitations="English only",
    )
    text = path.read_text()
    assert path == tmp_path / "cards" / "acme-agent-v1.md"
    assert "Apache-2.0" in text
    assert "internal support triage" in text
    assert "English only" in text
    assert entry["base_model"] in text
    assert "Not yet evaluated" in text


def test_write_model_card_includes_a_real_eval_summary_when_given(tmp_path):
    entry = _register(tmp_path)
    path = write_model_card(
        entry,
        "acme-agent",
        tmp_path / "cards",
        license="Apache-2.0",
        intended_use="internal support triage",
        limitations="English only",
        eval_summary="0.82 accuracy on the frozen eval set",
    )
    text = path.read_text()
    assert "0.82 accuracy" in text
    assert "Not yet evaluated" not in text


def test_export_model_downloads_and_verifies_against_the_registered_sha256(tmp_path):
    ckpt = _make_checkpoint(tmp_path)
    client = FakeS3Client()
    uploaded = upload_checkpoint(ckpt, "bucket", "checkpoints/exp1", client=client)

    registry_path = tmp_path / "registry.yaml"
    register_model(
        registry_path,
        "acme-agent",
        sha256=uploaded.manifest_sha256,
        uri=uploaded.uri,
        base_model="base",
        corpus_version="cv1",
    )

    out = tmp_path / "exported"
    result = export_model(registry_path, "acme-agent", out, version=1, client=client)

    assert (out / "config.json").read_text() == '{"base": "x"}'
    assert result.manifest_sha256 == uploaded.manifest_sha256


def test_export_model_raises_when_the_registered_sha256_is_wrong(tmp_path):
    ckpt = _make_checkpoint(tmp_path)
    client = FakeS3Client()
    uploaded = upload_checkpoint(ckpt, "bucket", "checkpoints/exp1", client=client)

    registry_path = tmp_path / "registry.yaml"
    register_model(
        registry_path,
        "acme-agent",
        sha256="wrong" + "a" * 59,
        uri=uploaded.uri,
        base_model="base",
        corpus_version="cv1",
    )

    with pytest.raises(ValueError):
        export_model(
            registry_path, "acme-agent", tmp_path / "exported", version=1, client=client
        )


def test_export_model_resolves_by_alias(tmp_path):
    ckpt = _make_checkpoint(tmp_path)
    client = FakeS3Client()
    uploaded = upload_checkpoint(ckpt, "bucket", "checkpoints/exp1", client=client)

    registry_path = tmp_path / "registry.yaml"
    register_model(
        registry_path,
        "acme-agent",
        sha256=uploaded.manifest_sha256,
        uri=uploaded.uri,
        base_model="base",
        corpus_version="cv1",
    )
    promote_model(registry_path, "acme-agent", 1, alias="production")

    result = export_model(
        registry_path,
        "acme-agent",
        tmp_path / "exported",
        alias="production",
        client=client,
    )
    assert result.manifest_sha256 == uploaded.manifest_sha256
