"""Real filesystem reads against storage other members already own.

Every function here is read-only — this service has no write path onto
any of these files; registering a dataset/model/eval-set version, or
draining a journey, stays `odyssey data`/`odyssey model`/`odyssey eval`/
`services/collector`'s job.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml  # pyrefly: ignore[missing-import]

__all__ = [
    "is_date_dir",
    "list_journeys",
    "find_journey_path",
    "read_registry",
    "list_eval_reports",
    "list_exports",
    "list_metrics",
    "read_products",
]


def is_date_dir(path: Path) -> bool:
    """True for a directory whose name parses as an ISO date — the same
    partition-name discipline ``odyssey_collector.prune.prune_dir`` uses,
    which is what keeps a non-date directory (e.g. the collector's own
    ``metrics/`` subdirectory, or a product-slug directory one level up)
    from being misread as a date partition."""
    if not path.is_dir():
        return False
    try:
        date.fromisoformat(path.name)
    except ValueError:
        return False
    return True


def _list_journeys_flat(dir_path: Path) -> List[Tuple[str, str]]:
    """``[(journey_id, date), ...]`` from date-partition subdirectories
    directly under ``dir_path`` — the flat-layout walk, reused both for a
    bare ``journeys_dir`` and for one product-slug subdirectory of it."""
    if not dir_path.is_dir():
        return []
    out: List[Tuple[str, str]] = []
    for date_dir in sorted(p for p in dir_path.iterdir() if is_date_dir(p)):
        for shard in sorted(date_dir.glob("*.jsonl")):
            out.append((shard.stem, date_dir.name))
    return out


def list_journeys(
    journeys_dir: Path, product_slug: Optional[str] = None
) -> List[Tuple[str, str]]:
    """``[(journey_id, date), ...]`` from ``<journeys_dir>/<date>/<journey_id>.jsonl``
    (flat collector layout) or ``<journeys_dir>/<product_slug>/<date>/<journey_id>.jsonl``
    (product-scoped layout, ``--products-file``) — both are walked, since a
    single ``journeys_dir`` only ever holds one or the other in practice and
    a caller here has no reliable way to know in advance which mode the
    collector was started in.

    A directory directly under ``journeys_dir`` is treated as a date
    partition if its name parses as an ISO date (flat layout); otherwise —
    unless it's the collector's own ``metrics/`` subdirectory — it's treated
    as a product-slug directory and its own date-partition subdirectories
    are walked one level deeper. Two products both writing a journey with
    the same id on the same date collide in the returned list the same way
    they always could on disk (there is no per-product namespacing here);
    that's the same "isolation is structural, not by id" tradeoff
    ``services/collector`` itself makes.

    ``product_slug``, when given, narrows the walk to just
    ``<journeys_dir>/<product_slug>`` (a product-scoped deployment's own
    partition) instead of pooling every product together.
    """
    if product_slug is not None:
        return _list_journeys_flat(journeys_dir / product_slug)
    if not journeys_dir.is_dir():
        return []
    out: List[Tuple[str, str]] = []
    for entry in sorted(p for p in journeys_dir.iterdir() if p.is_dir()):
        if is_date_dir(entry):
            for shard in sorted(entry.glob("*.jsonl")):
                out.append((shard.stem, entry.name))
            continue
        if entry.name == "metrics":
            continue
        out.extend(_list_journeys_flat(entry))
    return out


def find_journey_path(journeys_dir: Path, journey_id: str) -> Path | None:
    """The on-disk shard for ``journey_id``, searching every date partition
    in both the flat and product-scoped layouts (see ``list_journeys``).

    A journey id is the collector's own filename stem (never a caller-
    supplied path fragment); neither the date nor the product slug is part
    of the lookup key a caller has, so every partition is checked. If two
    products both hold a same-named journey, whichever is found first wins
    — the same collision `list_journeys` doesn't resolve either.
    """
    if not journeys_dir.is_dir():
        return None
    for entry in journeys_dir.iterdir():
        if not entry.is_dir():
            continue
        if is_date_dir(entry):
            candidate = entry / f"{journey_id}.jsonl"
            if candidate.is_file():
                return candidate
            continue
        if entry.name == "metrics":
            continue
        for date_dir in entry.iterdir():
            if not is_date_dir(date_dir):
                continue
            candidate = date_dir / f"{journey_id}.jsonl"
            if candidate.is_file():
                return candidate
    return None


def read_registry(
    registry_path: Path, group_key: str
) -> Dict[str, List[Dict[str, Any]]]:
    """Raw ``{name: [version entry, ...]}`` under ``group_key`` in a
    `registry.yaml` — the exact shape `odyssey_dataprep.datasets.
    update_registry` / `odyssey_training.models_registry.register_model` /
    `odyssey_eval.eval_datasets.update_registry` each write. Empty dict if
    the registry file doesn't exist yet, same as those writers' own
    "nothing registered yet" starting state.
    """
    if not registry_path.exists():
        return {}
    doc = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    return doc.get(group_key, {})


def list_eval_reports(reports_dir: Path) -> List[Path]:
    """Every `odyssey eval run`-written ``*.json`` report, newest write not
    assumed — callers sort/filter as they need."""
    if not reports_dir.is_dir():
        return []
    return sorted(reports_dir.glob("*.json"))


def list_exports(exports_dir: Path) -> List[Dict[str, Any]]:
    """``*.jsonl`` shards in a caller-configured exports directory (e.g. an
    `odyssey sft`/`odyssey dpo` output dir) — sha256/row count computed
    fresh from the bytes on disk, not trusted from any registry, since no
    export registry exists anywhere in this repo today."""
    if not exports_dir.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    for shard in sorted(exports_dir.glob("*.jsonl")):
        h = hashlib.sha256()
        rows = 0
        with open(shard, "rb") as f:
            for line in f:
                if line.strip():
                    rows += 1
                h.update(line)
        out.append(
            {
                "name": shard.name,
                "path": str(shard),
                "rows": rows,
                "sha256": h.hexdigest(),
            }
        )
    return out


def list_metrics(
    journeys_dir: Path, product_slug: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Host telemetry snapshots from ``<journeys_dir>/metrics/*.jsonl``
    (flat collector layout) and ``<journeys_dir>/<product_slug>/metrics/*.jsonl``
    (product-scoped layout, ``--products-file``) — mirrors
    `services/collector`'s own per-mode storage path exactly, same as
    `list_journeys`/`find_journey_path` now do for journey shards. Snapshots
    from every product are pooled into one list unless ``product_slug`` is
    given, in which case only that product's ``metrics/`` directory is read.
    Malformed lines are skipped rather than raising, same defensiveness as
    `list_journeys_with_status`'s per-shard fold failure handling. Sorted by
    ``ts`` descending (newest first)."""
    if not journeys_dir.is_dir():
        return []
    if product_slug is not None:
        metrics_dirs = [journeys_dir / product_slug / "metrics"]
    else:
        metrics_dirs = [journeys_dir / "metrics"]
        for entry in journeys_dir.iterdir():
            if entry.is_dir() and not is_date_dir(entry) and entry.name != "metrics":
                metrics_dirs.append(entry / "metrics")
    out: List[Dict[str, Any]] = []
    for metrics_dir in metrics_dirs:
        if not metrics_dir.is_dir():
            continue
        for shard in sorted(metrics_dir.glob("*.jsonl")):
            with open(shard, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    out.sort(key=lambda snapshot: snapshot.get("ts", ""), reverse=True)
    return out


def read_products(path: Optional[Path]) -> List[Dict[str, Any]]:
    """``[{"slug": ..., "name": ...}, ...]`` from a `services/collector`
    ``--products-file`` roster (see ``odyssey_collector.server.
    _load_products_file`` for the on-disk shape). ``api_key`` is dropped
    here, at the read boundary, rather than trusted to every caller to
    filter out downstream -- this is a read-only listing endpoint's data
    source and must never be able to leak the tenant secret.

    Empty list if ``path`` is unset, missing, or malformed -- this is a
    read-only consumer of a file another member owns, not the collector's
    own fail-fast startup check, so a bad roster degrades to "no products"
    rather than 500ing every request.
    """
    if path is None or not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    entries = raw.get("products") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        return []
    out: List[Dict[str, Any]] = []
    for entry in entries:
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("slug"), str)
            and isinstance(entry.get("name"), str)
        ):
            out.append({"slug": entry["slug"], "name": entry["name"]})
    return out
