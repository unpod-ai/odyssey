"""`experiments/<exp_id>.yaml` — item 5.8: config sha + corpus version +
metrics ref.

Per `docs/STRUCTURE.md`, `experiments/` holds TRACKED manifests only — one
training run's provenance, small enough to commit even though the run's
own `checkpoints/`, `logs/`, `outputs/` are `.gitignore`'d. This module
writes that manifest; it never runs `soup train` itself.

Mirrors `odyssey_dataprep.datasets.build_manifest`'s own discipline: the
config is hashed as found on disk, not trusted from the caller, so the
manifest is a genuine verification target, not a caller's unchecked claim.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

__all__ = ["write_experiment_manifest"]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def write_experiment_manifest(
    exp_id: str,
    *,
    config_path: Path | str,
    corpus_version: str,
    experiments_root: Path | str,
    metrics_ref: Optional[str] = None,
    checkpoint_uri: Optional[str] = None,
    checkpoint_sha256: Optional[str] = None,
    overwrite: bool = False,
) -> Path:
    """Write `{experiments_root}/{exp_id}.yaml`.

    `corpus_version` is `odyssey data corpus-version`'s own output (item
    4.5) — this module does not recompute it, the same "answer it once"
    discipline `datasets.py` already applies to `recipe_hash`.
    `metrics_ref` is a pointer (an MLflow/W&B run URL, a report path, ...),
    not the metrics themselves — nothing here parses or interprets it.
    `checkpoint_uri`/`checkpoint_sha256` are `checkpoints.upload_checkpoint`'s
    own output (item 5.9) — the object-store pointer and aggregate manifest
    hash for this run's weights, per ADR 0002's "git holds the recipe and
    the hash, the object store holds the bytes." Neither is required: a
    manifest recorded before training finishes (or for a run whose
    checkpoint was never uploaded) simply omits them.

    Raises `FileExistsError` unless `overwrite=True`: silently overwriting
    an experiment id would lose the previous run's provenance, the one
    thing this file exists to keep.
    """
    root = Path(experiments_root)
    path = root / f"{exp_id}.yaml"
    if path.exists() and not overwrite:
        raise FileExistsError(
            f"{path} already exists — pass overwrite=True to replace it"
        )

    manifest: Dict[str, Any] = {
        "exp_id": exp_id,
        "config_path": str(config_path),
        "config_sha256": _sha256_file(Path(config_path)),
        "corpus_version": corpus_version,
        "metrics_ref": metrics_ref,
        "checkpoint_uri": checkpoint_uri,
        "checkpoint_sha256": checkpoint_sha256,
    }

    root.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(manifest, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return path
