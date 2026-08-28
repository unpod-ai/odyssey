"""Exports use-case — lists `odyssey sft`/`odyssey dpo` output shards in a
caller-configured directory. No export registry exists anywhere in this
repo (unlike datasets/models/eval-sets), so this is a directory listing,
not a registry read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from odyssey_api.repositories import filesystem

__all__ = ["list_exports"]


def list_exports(exports_dir: Path) -> List[Dict[str, Any]]:
    return filesystem.list_exports(exports_dir)
