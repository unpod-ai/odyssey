"""Datasets/models use-cases — both are `{name: [version entry, ...]}` in a
`registry.yaml` (`odyssey_dataprep.datasets`'s `"corpora"` key,
`odyssey_training.models_registry`'s `"models"` key), so one function
covers both; routers pick the `group_key` and the DTO shape.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from odyssey_api.repositories import filesystem

__all__ = ["list_datasets", "list_models"]


def list_datasets(registry_path: Path) -> Dict[str, List[Dict[str, Any]]]:
    return filesystem.read_registry(registry_path, "corpora")


def list_models(registry_path: Path) -> Dict[str, List[Dict[str, Any]]]:
    return filesystem.read_registry(registry_path, "models")
