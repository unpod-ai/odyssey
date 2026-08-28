"""`checkpoints.py` — item 5.9: checkpoint -> object store.

Per `docs/adr/0002-artifacts-out-of-git.md`: git holds
`training/experiments/<exp_id>.yaml` (config sha + corpus version + metrics
ref); a checkpoint's own bytes — weights, optimizer state, tokenizer files
a `soup train` run writes under its `--output` dir — live in the object
store / MLflow, never git. This module does the upload half and returns the
pointer (`uri` + per-file sha256 + an aggregate manifest hash);
`experiments.write_experiment_manifest`'s `checkpoint_uri`/
`checkpoint_sha256` record it, the same "git holds the recipe and the
hash, the object store holds the bytes" split ADR 0002 already applies to
the corpus and model layers.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from odyssey.hashing import content_hash

__all__ = ["CheckpointUploadResult", "upload_checkpoint"]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class CheckpointUploadResult:
    uri: str
    files: List[Dict[str, Any]]
    manifest_sha256: str


def upload_checkpoint(
    checkpoint_dir: Path | str,
    bucket: str,
    prefix: str,
    *,
    endpoint_url: Optional[str] = None,
    client: Optional[Any] = None,
) -> CheckpointUploadResult:
    """Upload every file under ``checkpoint_dir`` to
    ``s3://bucket/prefix/<relative path>``.

    ``client`` is a `boto3` S3 client (or a test double exposing
    ``put_object``) — the same dependency-injection seam
    `odyssey_dataprep.collection.collect_from_object_store` (item 1.10)
    already uses. Omit it for a real ``boto3.client("s3",
    endpoint_url=endpoint_url)``, imported here, lazily — never at module
    scope — so `boto3` stays an optional extra (`odyssey-training[s3]`),
    not a dependency of this member's light install.

    Every file's sha256 is computed locally before its `put_object`, not
    trusted from the response — `build_manifest`'s own "recompute, don't
    trust the caller" discipline. Uploaded in sorted relative-path order so
    ``manifest_sha256`` (a `content_hash` over the sorted `(key, sha256)`
    set) is deterministic across repeat uploads of the same checkpoint.
    """
    if client is None:
        # pyrefly: ignore[missing-import]  — optional extra, odyssey-training[s3].
        import boto3  # noqa: PLC0415 - opt-in only when no client is injected

        client = boto3.client("s3", endpoint_url=endpoint_url)

    root = Path(checkpoint_dir)
    if not root.is_dir():
        raise NotADirectoryError(f"{root} is not a directory")

    paths = sorted(p for p in root.rglob("*") if p.is_file())
    if not paths:
        raise ValueError(f"{root} contains no files to upload")

    stripped_prefix = prefix.strip("/")
    files: List[Dict[str, Any]] = []
    for p in paths:
        rel = p.relative_to(root).as_posix()
        key = f"{stripped_prefix}/{rel}" if stripped_prefix else rel
        sha256 = _sha256_file(p)
        with open(p, "rb") as f:
            client.put_object(Bucket=bucket, Key=key, Body=f)
        files.append({"key": key, "sha256": sha256, "bytes": p.stat().st_size})

    manifest_sha256 = content_hash(sorted((f["key"], f["sha256"]) for f in files))
    uri = f"s3://{bucket}/{stripped_prefix}/" if stripped_prefix else f"s3://{bucket}/"
    return CheckpointUploadResult(uri=uri, files=files, manifest_sha256=manifest_sha256)
