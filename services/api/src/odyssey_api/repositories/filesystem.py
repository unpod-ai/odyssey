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
from typing import Any, Dict, List, Tuple

import yaml  # pyrefly: ignore[missing-import]

__all__ = [
    "list_journeys",
    "find_journey_path",
    "read_registry",
    "list_eval_reports",
    "list_exports",
    "list_metrics",
]


def list_journeys(journeys_dir: Path) -> List[Tuple[str, str]]:
    """``[(journey_id, date), ...]`` from ``<journeys_dir>/<date>/<journey_id>.jsonl``.

    Only the flat, non-product-scoped collector layout — a product-scoped
    deployment (`--products-file`) nests one more level (`<slug>/<date>/...`)
    and is out of scope here, same as the collector README's own "Not done
    here" note for cross-product listing.

    A directory directly under ``journeys_dir`` only counts as a date
    partition if its name parses as an ISO date — this mirrors
    ``odyssey_collector.prune.prune_dir``'s own discipline, and keeps
    non-date directories the collector also writes here (e.g. its
    ``metrics/`` subdirectory) from being misread as journey shards.
    """
    if not journeys_dir.is_dir():
        return []
    out: List[Tuple[str, str]] = []
    for date_dir in sorted(p for p in journeys_dir.iterdir() if p.is_dir()):
        try:
            date.fromisoformat(date_dir.name)
        except ValueError:
            continue
        for shard in sorted(date_dir.glob("*.jsonl")):
            out.append((shard.stem, date_dir.name))
    return out


def find_journey_path(journeys_dir: Path, journey_id: str) -> Path | None:
    """The on-disk shard for ``journey_id``, searching every date partition.

    A journey id is the collector's own filename stem (never a caller-
    supplied path fragment); the date is not part of the lookup key a
    caller has, so every partition is checked.
    """
    if not journeys_dir.is_dir():
        return None
    for date_dir in journeys_dir.iterdir():
        if not date_dir.is_dir():
            continue
        try:
            date.fromisoformat(date_dir.name)
        except ValueError:
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


def list_metrics(journeys_dir: Path) -> List[Dict[str, Any]]:
    """Host telemetry snapshots from ``<journeys_dir>/metrics/*.jsonl`` —
    mirrors `services/collector`'s single-key/open-mode storage path
    exactly. **Deliberately only the flat, non-product-scoped layout**,
    same documented scope cut `list_journeys`/`find_journey_path` already
    have. Malformed lines are skipped rather than raising, same
    defensiveness as `list_journeys_with_status`'s per-shard fold failure
    handling. Sorted by ``ts`` descending (newest first)."""
    metrics_dir = journeys_dir / "metrics"
    if not metrics_dir.is_dir():
        return []
    out: List[Dict[str, Any]] = []
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
