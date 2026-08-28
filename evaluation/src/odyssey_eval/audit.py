"""dataset-audit.yml (item 7.4, named explicitly in `docs/STRUCTURE.md`):
manifest sha256 integrity gate over every registered training corpus
(`data_preparation/datasets/registry.yaml`) and frozen eval set
(`evaluation/datasets/registry.yaml`).

Nothing else in this repo checks "does the registry's recorded
`manifest_sha256` still match the manifest file on disk" — catches a
hand-edited or corrupted manifest before a training run trusts it. The
no-overlap check itself (item 7.4's other half) is `overlap.check_no_overlap`,
run via `odyssey eval check-overlap` against real journeys dirs the caller
names — not run unconditionally here, since no fixed pipeline path for
those dirs exists yet: both registries are still empty scaffolding as of
this module's own creation, and an audit over zero registered datasets is
a real "nothing to check yet" pass, not a silently faked one.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import List

import yaml  # pyrefly: ignore[missing-import]

__all__ = ["audit_registry", "main"]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def audit_registry(registry_path: Path | str, group_key: str) -> List[str]:
    """Errors, one per entry whose recorded ``manifest_sha256`` no longer
    matches the manifest file on disk (or is missing entirely).
    ``group_key`` is ``"corpora"`` for a training registry
    (`odyssey_dataprep.datasets.update_registry`'s shape) or ``"eval_sets"``
    for an eval registry (`eval_datasets.update_registry`'s shape)."""
    path = Path(registry_path)
    if not path.exists():
        return []
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    errors: List[str] = []
    for name, versions in (doc.get(group_key) or {}).items():
        for entry in versions:
            manifest_path = Path(entry["uri"])
            if not manifest_path.exists():
                errors.append(
                    f"{name} v{entry['version']}: manifest missing at {manifest_path}"
                )
                continue
            actual = _sha256_file(manifest_path)
            if actual != entry["manifest_sha256"]:
                errors.append(
                    f"{name} v{entry['version']}: manifest_sha256 mismatch "
                    f"(registry {entry['manifest_sha256']}, on-disk {actual})"
                )
    return errors


def main(argv: List[str] | None = None) -> int:
    errors: List[str] = []
    errors += audit_registry("data_preparation/datasets/registry.yaml", "corpora")
    errors += audit_registry("evaluation/datasets/registry.yaml", "eval_sets")
    for err in errors:
        print(err, file=sys.stderr)
    if not errors:
        print("ok: no manifest integrity breaches (or no registries yet)")
        return 0
    print(f"FAILED: {len(errors)} breach(es)")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
