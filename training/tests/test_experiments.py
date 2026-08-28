"""experiments/<exp_id>.yaml manifest writer (item 5.8)."""

from __future__ import annotations

import hashlib

import pytest
import yaml

from odyssey_training.experiments import write_experiment_manifest


def test_write_experiment_manifest_records_config_sha_and_corpus_version(tmp_path):
    config = tmp_path / "soup.yaml"
    config.write_text("base: x\ntask: sft\n")

    out = write_experiment_manifest(
        "exp_0001",
        config_path=config,
        corpus_version="corpus-sha-abc",
        experiments_root=tmp_path / "experiments",
    )
    doc = yaml.safe_load(out.read_text())

    assert out == tmp_path / "experiments" / "exp_0001.yaml"
    assert doc["exp_id"] == "exp_0001"
    assert doc["config_path"] == str(config)
    assert doc["config_sha256"] == hashlib.sha256(config.read_bytes()).hexdigest()
    assert doc["corpus_version"] == "corpus-sha-abc"
    assert doc["metrics_ref"] is None


def test_write_experiment_manifest_records_a_metrics_ref_when_given(tmp_path):
    config = tmp_path / "soup.yaml"
    config.write_text("base: x\n")

    out = write_experiment_manifest(
        "exp_0002",
        config_path=config,
        corpus_version="corpus-sha-def",
        experiments_root=tmp_path / "experiments",
        metrics_ref="https://wandb.ai/acme/run/exp_0002",
    )
    doc = yaml.safe_load(out.read_text())
    assert doc["metrics_ref"] == "https://wandb.ai/acme/run/exp_0002"


def test_write_experiment_manifest_records_a_checkpoint_pointer_when_given(tmp_path):
    config = tmp_path / "soup.yaml"
    config.write_text("base: x\n")

    out = write_experiment_manifest(
        "exp_ckpt",
        config_path=config,
        corpus_version="corpus-sha-ghi",
        experiments_root=tmp_path / "experiments",
        checkpoint_uri="s3://bucket/checkpoints/exp_ckpt/",
        checkpoint_sha256="deadbeef",
    )
    doc = yaml.safe_load(out.read_text())
    assert doc["checkpoint_uri"] == "s3://bucket/checkpoints/exp_ckpt/"
    assert doc["checkpoint_sha256"] == "deadbeef"


def test_write_experiment_manifest_checkpoint_pointer_defaults_to_none(tmp_path):
    config = tmp_path / "soup.yaml"
    config.write_text("base: x\n")

    out = write_experiment_manifest(
        "exp_no_ckpt",
        config_path=config,
        corpus_version="v1",
        experiments_root=tmp_path / "experiments",
    )
    doc = yaml.safe_load(out.read_text())
    assert doc["checkpoint_uri"] is None
    assert doc["checkpoint_sha256"] is None


def test_write_experiment_manifest_refuses_to_clobber_an_existing_exp_id(tmp_path):
    config = tmp_path / "soup.yaml"
    config.write_text("base: x\n")
    root = tmp_path / "experiments"

    write_experiment_manifest(
        "exp_0003", config_path=config, corpus_version="v1", experiments_root=root
    )
    with pytest.raises(FileExistsError):
        write_experiment_manifest(
            "exp_0003", config_path=config, corpus_version="v2", experiments_root=root
        )


def test_write_experiment_manifest_overwrite_replaces_it(tmp_path):
    config = tmp_path / "soup.yaml"
    config.write_text("base: x\n")
    root = tmp_path / "experiments"

    write_experiment_manifest(
        "exp_0004", config_path=config, corpus_version="v1", experiments_root=root
    )
    out = write_experiment_manifest(
        "exp_0004",
        config_path=config,
        corpus_version="v2",
        experiments_root=root,
        overwrite=True,
    )
    doc = yaml.safe_load(out.read_text())
    assert doc["corpus_version"] == "v2"


def test_write_experiment_manifest_hashes_the_config_as_found_on_disk(tmp_path):
    config = tmp_path / "soup.yaml"
    config.write_text("base: x\n")
    out = write_experiment_manifest(
        "exp_0005",
        config_path=config,
        corpus_version="v1",
        experiments_root=tmp_path / "experiments",
    )
    original_sha = yaml.safe_load(out.read_text())["config_sha256"]

    config.write_text("base: y\n")
    assert hashlib.sha256(config.read_bytes()).hexdigest() != original_sha
