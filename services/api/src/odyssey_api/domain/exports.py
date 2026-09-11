"""Exports use-case — lists `odyssey sft`/`odyssey dpo` output shards in a
caller-configured directory. No export registry exists anywhere in this
repo (unlike datasets/models/eval-sets), so this is a directory listing,
not a registry read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from odyssey_api.index.manager import IndexHandle
from odyssey_api.repositories import filesystem

__all__ = ["list_exports", "list_exports_indexed"]


def list_exports(exports_dir: Path) -> List[Dict[str, Any]]:
    return filesystem.list_exports(exports_dir)


def list_exports_indexed(index: IndexHandle) -> List[Dict[str, Any]]:
    """Index-backed replacement for `list_exports`: reads artifact fields
    straight out of the `exports` table (populated at index time) instead
    of re-walking `exports_dir` on every request."""
    rows = index.query("SELECT name, path, rows, sha256 FROM exports ORDER BY name")
    return [dict(row) for row in rows]
