"""checkpoint -> object store upload (item 5.9)."""

from __future__ import annotations

import hashlib

import pytest

from odyssey_training.checkpoints import upload_checkpoint


class FakeS3Client:
    """A minimal boto3 S3 client double — just the one method
    ``upload_checkpoint`` calls, mirroring `test_collection.py`'s own
    `FakeS3Client` for `collect_from_object_store` (item 1.10)."""

    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body):
        self.objects[(Bucket, Key)] = Body.read()


def _make_checkpoint(tmp_path):
    ckpt = tmp_path / "checkpoint"
    (ckpt / "adapter").mkdir(parents=True)
    (ckpt / "config.json").write_text('{"base": "x"}')
    (ckpt / "adapter" / "weights.bin").write_bytes(b"\x00\x01\x02fake-weights")
    return ckpt


def test_upload_checkpoint_uploads_every_file(tmp_path):
    ckpt = _make_checkpoint(tmp_path)
    client = FakeS3Client()

    result = upload_checkpoint(ckpt, "bucket", "checkpoints/exp_0001", client=client)

    assert client.objects[("bucket", "checkpoints/exp_0001/config.json")] == (
        b'{"base": "x"}'
    )
    assert (
        client.objects[("bucket", "checkpoints/exp_0001/adapter/weights.bin")]
        == b"\x00\x01\x02fake-weights"
    )
    assert result.uri == "s3://bucket/checkpoints/exp_0001/"


def test_upload_checkpoint_computes_real_sha256_per_file(tmp_path):
    ckpt = _make_checkpoint(tmp_path)
    client = FakeS3Client()

    result = upload_checkpoint(ckpt, "bucket", "prefix", client=client)

    by_key = {f["key"]: f for f in result.files}
    expected = hashlib.sha256(b'{"base": "x"}').hexdigest()
    assert by_key["prefix/config.json"]["sha256"] == expected


def test_upload_checkpoint_manifest_sha256_is_deterministic(tmp_path):
    ckpt = _make_checkpoint(tmp_path)

    result_a = upload_checkpoint(ckpt, "bucket", "prefix", client=FakeS3Client())
    result_b = upload_checkpoint(ckpt, "bucket", "prefix", client=FakeS3Client())

    assert result_a.manifest_sha256 == result_b.manifest_sha256


def test_upload_checkpoint_manifest_sha256_changes_when_a_file_changes(tmp_path):
    ckpt = _make_checkpoint(tmp_path)
    before = upload_checkpoint(ckpt, "bucket", "prefix", client=FakeS3Client())

    (ckpt / "config.json").write_text('{"base": "y"}')
    after = upload_checkpoint(ckpt, "bucket", "prefix", client=FakeS3Client())

    assert before.manifest_sha256 != after.manifest_sha256


def test_upload_checkpoint_rejects_a_missing_directory(tmp_path):
    with pytest.raises(NotADirectoryError):
        upload_checkpoint(tmp_path / "nope", "bucket", "prefix", client=FakeS3Client())


def test_upload_checkpoint_rejects_an_empty_directory(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError):
        upload_checkpoint(empty, "bucket", "prefix", client=FakeS3Client())


def test_upload_checkpoint_handles_an_empty_prefix(tmp_path):
    ckpt = _make_checkpoint(tmp_path)
    client = FakeS3Client()

    result = upload_checkpoint(ckpt, "bucket", "", client=client)

    assert ("bucket", "config.json") in client.objects
    assert result.uri == "s3://bucket/"
