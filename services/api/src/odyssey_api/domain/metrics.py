"""Metrics use-case — lists `services/collector`'s `POST /metrics`-written
host telemetry snapshots. No metrics registry exists anywhere in this repo
(same shape as exports), so this is a directory listing, not a registry
read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from odyssey_api.repositories import filesystem

__all__ = ["list_metrics"]


def list_metrics(
    journeys_dir: Path, product_slug: Optional[str] = None
) -> List[Dict[str, Any]]:
    return filesystem.list_metrics(journeys_dir, product_slug)
