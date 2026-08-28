"""checkpoint -> object store upload (item 5.9)."""

from __future__ import annotations

import hashlib

import pytest

from odyssey_training.checkpoints import (
    download_checkpoint,
    parse_s3_uri,
    upload_checkpoint,
)


class FakeS3Client:
    """A minimal boto3 S3 client double covering both directions —
    ``put_object`` for `upload_checkpoint`, ``list_objects_v2``/
    ``get_object`` for `download_checkpoint` — mirroring
    `test_collection.py`'s own `FakeS3Client` for `collect_from_object_store`
    (item 1.10)."""

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
        body = self.objects[(Bucket, Key)]
        return {"Body": _FakeBody(body)}


class _FakeBody:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


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


def test_parse_s3_uri_splits_bucket_and_prefix():
    assert parse_s3_uri("s3://bucket/checkpoints/exp1/") == (
        "bucket",
        "checkpoints/exp1",
    )


def test_parse_s3_uri_handles_no_prefix():
    assert parse_s3_uri("s3://bucket/") == ("bucket", "")


def test_parse_s3_uri_rejects_a_non_s3_uri():
    with pytest.raises(ValueError):
        parse_s3_uri("https://example.com/x")


def test_download_checkpoint_is_the_inverse_of_upload(tmp_path):
    ckpt = _make_checkpoint(tmp_path)
    client = FakeS3Client()
    uploaded = upload_checkpoint(ckpt, "bucket", "checkpoints/exp1", client=client)

    out = tmp_path / "downloaded"
    downloaded = download_checkpoint(uploaded.uri, out, client=client)

    assert (out / "config.json").read_text() == '{"base": "x"}'
    assert (out / "adapter" / "weights.bin").read_bytes() == (
        b"\x00\x01\x02fake-weights"
    )
    assert downloaded.manifest_sha256 == uploaded.manifest_sha256


def test_download_checkpoint_rejects_a_uri_with_nothing_under_it(tmp_path):
    with pytest.raises(ValueError):
        download_checkpoint(
            "s3://bucket/nothing-here/", tmp_path / "out", client=FakeS3Client()
        )
