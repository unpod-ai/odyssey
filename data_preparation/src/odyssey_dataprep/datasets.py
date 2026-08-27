"""`datasets/` — items 4.6/4.7/4.8: registry, manifests, cards.

Per `docs/adr/0002-artifacts-out-of-git.md` and `docs/STRUCTURE.md`: git
holds the recipe and the hashes, the object store holds the bytes. This
module writes the three git-tracked pieces for one corpus version —
`versioning.py` (4.4/4.5) already answers "what is this corpus," this
module answers "where do I find it":

- **manifest** (`datasets/manifests/<name>/v<N>.json`, 4.7) — shards + their
  sha256 + row counts + `recipe_hash`, so a corpus is verifiable without
  being present.
- **registry** (`datasets/registry.yaml`, 4.6) — `name -> versions ->
  manifest sha -> URI`, per `docs/STRUCTURE.md`'s own contract.
- **card** (`datasets/cards/<name>-v<N>.md`, 4.8) — provenance, license, PII
  posture, splits, intended use. License/PII/intended-use/splits are policy
  calls no code can infer, so they are caller-supplied, not derived.

No object-store integration exists yet (item 1.10) — `uri` in the registry
falls back to the manifest's own tracked-in-git path, the only location the
bytes are actually reachable from today.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

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
    plus one, ``1`` if none exist yet.

    This is also the ``curated_watermark.seq`` for the run that produces it:
    `design.md` Decision 9 defines ``seq`` as "one curation run = one seq,"
    and a manifest version *is* one curation run's durable record — no
    separate counter to keep in sync.
    """
    corpus_dir = Path(manifests_root) / name
    if not corpus_dir.is_dir():
        return 1
    versions = []
    for p in corpus_dir.glob("v*.json"):
        try:
            versions.append(int(p.stem[1:]))
        except ValueError:
            continue
    return max(versions, default=0) + 1


def build_manifest(
    name: str,
    manifests_root: Path | str,
    *,
    corpus_version: str,
    recipe_hash: str,
    curated_watermark: Dict[str, Any],
    shard_paths: List[Path | str],
) -> Dict[str, Any]:
    """Assemble one corpus version's manifest — does not write it.

    ``shard_paths`` are the actual files this corpus version consists of
    (e.g. an `odyssey sft`/`odyssey dpo` output); their sha256 and row count
    are computed here, not trusted from the caller, so the manifest is a
    genuine verification target per ADR 0002.
    """
    return {
        "name": name,
        "version": next_version(name, manifests_root),
        "corpus_version": corpus_version,
        "recipe_hash": recipe_hash,
        "curated_watermark": curated_watermark,
        "shards": [_shard_info(Path(p)) for p in shard_paths],
    }


def write_manifest(manifest: Dict[str, Any], manifests_root: Path | str) -> Path:
    """Write ``{manifests_root}/{name}/v{version}.json``, atomically."""
    corpus_dir = Path(manifests_root) / manifest["name"]
    corpus_dir.mkdir(parents=True, exist_ok=True)
    path = corpus_dir / f"v{manifest['version']}.json"
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

    Schema per `docs/STRUCTURE.md`: ``name -> versions -> manifest sha ->
    URI``. Idempotent on ``(name, version)`` — re-running against the same
    manifest file updates that entry in place rather than duplicating it,
    since a manifest can legitimately be rewritten (e.g. after adding a
    shard) without minting a new version.
    """
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
    corpora = doc.setdefault("corpora", {})
    versions = corpora.setdefault(name, [])
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
    pii_posture: str,
    intended_use: str,
    splits: Optional[str] = None,
) -> Path:
    """Write ``{cards_root}/{name}-v{version}.md`` — the human-facing half of
    a corpus version. License, PII posture, and intended use are the
    curator's own claims, not inferred from the manifest; ``splits``
    defaults to "not yet split" since `splitting/` (item 3.7) does not
    exist yet.
    """
    name = manifest["name"]
    version = manifest["version"]
    total_rows = sum(s["rows"] for s in manifest["shards"])
    lines = [
        f"# {name} v{version}",
        "",
        "## Provenance",
        f"- corpus_version: `{manifest['corpus_version']}`",
        f"- recipe_hash: `{manifest['recipe_hash']}`",
        f"- curated_watermark: `{manifest['curated_watermark']}`",
        f"- shards: {len(manifest['shards'])} ({total_rows} rows total)",
        "",
        "## License",
        license,
        "",
        "## PII posture",
        pii_posture,
        "",
        "## Splits",
        splits or "Not yet split — see `data_preparation/splitting` (item 3.7).",
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
