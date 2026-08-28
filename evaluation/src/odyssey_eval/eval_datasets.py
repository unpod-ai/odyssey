"""`datasets/` — item 7.2: frozen eval sets, never trained on.

Mirrors `odyssey_dataprep.datasets`' manifest/registry/card shape (the exact
"TRACKED manifests + cards only" contract `docs/STRUCTURE.md` gives both
members) rather than inventing a new schema. Named `eval_datasets` to avoid
colliding with the stdlib-shaped `datasets` name `odyssey_dataprep` already
owns.

An eval set has no `recipe_hash`/`curated_watermark` — those describe *how a
training corpus was curated*, a question that doesn't apply to a frozen,
hand-built or vendored eval set. "Frozen" itself is a property this module
does not enforce (no write-protection) — it is enforced downstream, by
item 7.4's no-overlap gate (`overlap.py`) refusing to let an eval id appear
in a training split.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List

__all__ = [
    "next_version",
    "build_manifest",
    "write_manifest",
    "update_registry",
    "write_card",
]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _count_rows(path: Path) -> int:
    with open(path, "rb") as f:
        return sum(1 for _ in f)


def _shard_info(path: Path) -> Dict[str, Any]:
    return {
        "path": str(path),
        "sha256": _sha256_file(path),
        "rows": _count_rows(path),
    }


def next_version(name: str, manifests_root: Path | str) -> int:
    """The next manifest version for ``name`` — highest existing ``v<N>.json``
    plus one, ``1`` if none exist yet. Same rule as
    `odyssey_dataprep.datasets.next_version`."""
    eval_set_dir = Path(manifests_root) / name
    if not eval_set_dir.is_dir():
        return 1
    versions = []
    for p in eval_set_dir.glob("v*.json"):
        try:
            versions.append(int(p.stem[1:]))
        except ValueError:
            continue
    return max(versions, default=0) + 1


def build_manifest(
    name: str,
    manifests_root: Path | str,
    *,
    shard_paths: List[Path | str],
) -> Dict[str, Any]:
    """Assemble one eval set version's manifest — does not write it.

    ``shard_paths`` are the actual eval-set files (e.g. a benchmark's
    journeys dir contents, or a `*.jsonl` of prompts+references); their
    sha256 and row count are computed here, not trusted from the caller, so
    the manifest is a genuine verification target."""
    return {
        "name": name,
        "version": next_version(name, manifests_root),
        "shards": [_shard_info(Path(p)) for p in shard_paths],
    }


def write_manifest(manifest: Dict[str, Any], manifests_root: Path | str) -> Path:
    """Write ``{manifests_root}/{name}/v{version}.json``, atomically."""
    eval_set_dir = Path(manifests_root) / manifest["name"]
    eval_set_dir.mkdir(parents=True, exist_ok=True)
    path = eval_set_dir / f"v{manifest['version']}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    tmp.replace(path)
    return path


def update_registry(
    registry_path: Path | str, name: str, manifest_path: Path | str
) -> Path:
    """Record one more version for ``name`` in ``datasets/registry.yaml``.

    Idempotent on ``(name, version)`` — same replace-in-place rule as
    `odyssey_dataprep.datasets.update_registry`."""
    import yaml  # pyrefly: ignore[missing-import]

    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    version = manifest["version"]
    entry = {
        "version": version,
        "manifest_sha256": _sha256_file(Path(manifest_path)),
        "uri": str(manifest_path),
    }

    path = Path(registry_path)
    doc: Dict[str, Any] = {}
    if path.exists():
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    eval_sets = doc.setdefault("eval_sets", {})
    versions = eval_sets.setdefault(name, [])
    versions[:] = [v for v in versions if v.get("version") != version]
    versions.append(entry)
    versions.sort(key=lambda v: v["version"])

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(doc, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return path


def write_card(
    manifest: Dict[str, Any],
    cards_root: Path | str,
    *,
    license: str,
    intended_use: str,
    provenance: str,
) -> Path:
    """Write ``{cards_root}/{name}-v{version}.md`` — the human-facing half of
    a frozen eval set. ``provenance`` (where the tasks/references came from)
    is the curator's own claim, same treatment `datasets.write_card` gives
    license/PII-posture/intended-use."""
    name = manifest["name"]
    version = manifest["version"]
    total_rows = sum(s["rows"] for s in manifest["shards"])
    lines = [
        f"# {name} v{version}",
        "",
        "## Provenance",
        provenance,
        f"- shards: {len(manifest['shards'])} ({total_rows} rows total)",
        "",
        "## License",
        license,
        "",
        "## Frozen",
        "This eval set is never trained on — enforced by the no-overlap gate "
        "(item 7.4, `odyssey_eval.overlap`), not by any write-protection here.",
        "",
        "## Intended use",
        intended_use,
        "",
    ]

    path = Path(cards_root) / f"{name}-v{version}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(path)
    return path
