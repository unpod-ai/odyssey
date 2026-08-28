"""`models/registry.yaml` — item 6.1: name -> version -> sha256 -> URI ->
base model -> corpus version.

Per `docs/STRUCTURE.md` and `docs/adr/0002-artifacts-out-of-git.md`:
`models/` is a registry, not weight storage — git tracks this file (and,
once item 6.2 exists, a model card next to it), the object store holds the
actual weights. This module writes the registry entry; it never touches
the weights themselves.

A registered model's `sha256`/`uri` are expected to be
`checkpoints.upload_checkpoint`'s own `manifest_sha256`/`uri` (item 5.9) —
this module does not re-upload or re-hash anything, the same "answer it
once" discipline `odyssey_dataprep.datasets` already applies to
`recipe_hash`/`corpus_version`. Promoting a checkpoint to a named model is
a deliberate, caller-invoked act (a human or a pipeline decides *this*
checkpoint is *the* v3 of `acme-support-agent`) — nothing here infers it
automatically from a training run finishing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import yaml

__all__ = ["next_version", "register_model"]


def next_version(name: str, registry_path: Path | str) -> int:
    """The next version for ``name`` — highest existing version plus one,
    ``1`` if ``name`` has no entries yet (or the registry file doesn't
    exist yet). Mirrors `odyssey_dataprep.datasets.next_version`'s own
    "highest existing + 1" rule for corpus versions."""
    path = Path(registry_path)
    if not path.exists():
        return 1
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    versions = doc.get("models", {}).get(name, [])
    return max((v.get("version", 0) for v in versions), default=0) + 1


def register_model(
    registry_path: Path | str,
    name: str,
    *,
    sha256: str,
    uri: str,
    base_model: str,
    corpus_version: str,
    version: Optional[int] = None,
) -> Dict[str, Any]:
    """Record one more version of ``name`` in `models/registry.yaml`.

    ``version`` defaults to `next_version(...)`. Passing one explicitly
    (e.g. to re-register the same checkpoint under a version already
    minted) is idempotent on ``(name, version)`` — re-running against the
    same version updates that entry in place rather than duplicating it,
    the same replace-in-place semantics
    `odyssey_dataprep.datasets.update_registry` already applies to corpus
    versions.

    ``sha256``/``uri``/``base_model``/``corpus_version`` are the caller's
    own claims: this module has no access to the checkpoint's actual bytes
    (only its already-uploaded pointer), so it cannot verify them itself —
    the same trust boundary `datasets.write_card`'s license/PII-posture
    fields already accept, a policy call no code can infer.
    """
    if version is None:
        version = next_version(name, registry_path)
    entry: Dict[str, Any] = {
        "version": version,
        "sha256": sha256,
        "uri": uri,
        "base_model": base_model,
        "corpus_version": corpus_version,
    }

    path = Path(registry_path)
    doc: Dict[str, Any] = {}
    if path.exists():
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    models = doc.setdefault("models", {})
    versions = models.setdefault(name, [])
    versions[:] = [v for v in versions if v.get("version") != version]
    versions.append(entry)
    versions.sort(key=lambda v: v["version"])

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(doc, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return entry
