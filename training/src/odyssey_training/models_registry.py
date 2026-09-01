"""`models/registry.yaml` + `models/cards/` — items 6.1/6.2/6.4: registry,
model cards, promote/export.

Per `docs/STRUCTURE.md` and `docs/adr/0002-artifacts-out-of-git.md`:
`models/` is a registry, not weight storage — git tracks the registry file
and the cards next to it, the object store holds the actual weights. This
module writes/reads that git-tracked half; it downloads weights back only
for `export_model` (6.4), and even then just to hand them to a caller, not
to keep or convert them.

A registered model's `sha256`/`uri` are expected to be
`checkpoints.upload_checkpoint`'s own `manifest_sha256`/`uri` (item 5.9) —
this module does not re-upload or re-hash anything, the same "answer it
once" discipline `odyssey_dataprep.datasets` already applies to
`recipe_hash`/`corpus_version`. Registering (`register_model`) and
promoting (`promote_model`) are deliberately separate, caller-invoked
acts: minting a version and deciding it's the one to serve are different
decisions, often made by different people or at different times — nothing
here infers either automatically from a training run finishing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import yaml

__all__ = [
    "next_version",
    "register_model",
    "resolve_model",
    "promote_model",
    "export_model",
    "write_model_card",
]


def _load(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _save(path: Path, doc: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(doc, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def next_version(name: str, registry_path: Path | str) -> int:
    """The next version for ``name`` — highest existing version plus one,
    ``1`` if ``name`` has no entries yet (or the registry file doesn't
    exist yet). Mirrors `odyssey_dataprep.datasets.next_version`'s own
    "highest existing + 1" rule for corpus versions."""
    doc = _load(Path(registry_path))
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
    doc = _load(path)
    models = doc.setdefault("models", {})
    versions = models.setdefault(name, [])
    versions[:] = [v for v in versions if v.get("version") != version]
    versions.append(entry)
    versions.sort(key=lambda v: v["version"])

    _save(path, doc)
    return entry


def resolve_model(
    registry_path: Path | str,
    name: str,
    *,
    version: Optional[int] = None,
    alias: Optional[str] = None,
) -> Dict[str, Any]:
    """Look up one registered version of ``name`` — by an explicit
    ``version`` or via an ``alias`` `promote_model` set. Exactly one of the
    two is required, the same mutually-exclusive-params discipline
    `HttpSink(api_key=...)` / `--products-file` already use elsewhere in this
    repo. Raises `KeyError` for an unknown name/version/alias rather than
    returning `None` — a caller resolving a model it's about to export or
    serve should fail loudly, not silently get nothing.
    """
    if (version is None) == (alias is None):
        raise ValueError("pass exactly one of version= or alias=")

    path = Path(registry_path)
    doc = _load(path)
    if alias is not None:
        version = doc.get("aliases", {}).get(name, {}).get(alias)
        if version is None:
            raise KeyError(f"no alias {alias!r} registered for {name!r} in {path}")

    for v in doc.get("models", {}).get(name, []):
        if v.get("version") == version:
            return v
    raise KeyError(f"{name!r} v{version} is not registered in {path}")


def promote_model(
    registry_path: Path | str,
    name: str,
    version: int,
    *,
    alias: str = "production",
) -> Dict[str, Any]:
    """Point ``alias`` (default ``"production"``) at an already-registered
    ``(name, version)`` (item 6.4) — the "this is the one to serve/export"
    act, kept as a separate step from `register_model` since minting a
    version and deciding it's ready are different decisions, often made by
    different people or at different times.

    Raises `KeyError` if ``(name, version)`` isn't registered yet —
    promoting a version that doesn't exist would silently create a
    dangling alias `resolve_model`/`export_model` would then fail on
    anyway, just later and less clearly.
    """
    path = Path(registry_path)
    doc = _load(path)
    versions = doc.get("models", {}).get(name, [])
    if not any(v.get("version") == version for v in versions):
        raise KeyError(f"{name!r} v{version} is not registered in {path}")

    aliases = doc.setdefault("aliases", {})
    aliases.setdefault(name, {})[alias] = version
    _save(path, doc)
    return {"name": name, "alias": alias, "version": version}


def export_model(
    registry_path: Path | str,
    name: str,
    out_dir: Path | str,
    *,
    version: Optional[int] = None,
    alias: Optional[str] = None,
    endpoint_url: Optional[str] = None,
    client: Optional[Any] = None,
) -> Any:
    """Download a registered model version's checkpoint bytes to
    ``out_dir`` (item 6.4) — resolves the entry via `resolve_model`, then
    `checkpoints.download_checkpoint`'s the object-store half, the inverse
    of `checkpoints.upload_checkpoint`.

    The freshly downloaded manifest sha256 is verified against the
    registry's own recorded ``sha256``; a mismatch raises `ValueError`
    rather than silently handing back bytes that don't match what was
    registered.

    This downloads the checkpoint's original files as uploaded — it does
    **not** convert them to a serving format (GGUF/ONNX/safetensors,
    `models/exported/`'s own stated purpose per `docs/STRUCTURE.md`).
    Format conversion is real, format-specific ML tooling with no
    consumer named yet in this repo — the same explicit, documented scope
    cut item 0.11's OTel bridge and item 3.5's LLM augmentation extra
    already got before a real need was named.
    """
    from odyssey_training.checkpoints import download_checkpoint

    entry = resolve_model(registry_path, name, version=version, alias=alias)
    result = download_checkpoint(
        entry["uri"], out_dir, endpoint_url=endpoint_url, client=client
    )
    if result.manifest_sha256 != entry["sha256"]:
        raise ValueError(
            f"downloaded checkpoint for {name!r} v{entry['version']} does not "
            f"match its registered sha256 "
            f"({result.manifest_sha256} != {entry['sha256']})"
        )
    return result


def write_model_card(
    entry: Dict[str, Any],
    name: str,
    cards_root: Path | str,
    *,
    license: str,
    intended_use: str,
    limitations: str,
    eval_summary: Optional[str] = None,
) -> Path:
    """Write ``{cards_root}/{name}-v{version}.md`` (item 6.2) — the
    human-facing half of a registered model version, mirroring
    `datasets.write_card`'s own shape: provenance pulled from the entry
    itself, license/intended-use/limitations as the caller's own policy
    claims (no code can infer them). ``eval_summary`` defaults to a
    placeholder since `evaluation/` (Step 7) doesn't exist yet — the same
    "not yet" honesty `write_card`'s own ``splits`` default already used
    for `splitting/` before item 3.7 existed.
    """
    version = entry["version"]
    lines = [
        f"# {name} v{version}",
        "",
        "## Provenance",
        f"- base_model: `{entry['base_model']}`",
        f"- corpus_version: `{entry['corpus_version']}`",
        f"- sha256: `{entry['sha256']}`",
        f"- uri: `{entry['uri']}`",
        "",
        "## License",
        license,
        "",
        "## Evaluation",
        eval_summary or "Not yet evaluated — see `evaluation/` (Step 7, not built).",
        "",
        "## Limitations",
        limitations,
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
