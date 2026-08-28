"""Env-first config, explicit argument wins — same precedence rule every
other member (`odyssey.config.resolve()`, `services/collector`'s server
config) already uses.

Every path here points at storage another member already owns and writes:
`journeys_dir` is `services/collector`'s own `--data-dir` (the flat
``<date>/<journey_id>.jsonl`` layout, non-project-scoped case only — see
README's "Not done here"), the three registry paths are
`odyssey_dataprep.datasets` / `odyssey_training.models_registry` /
`odyssey_eval.eval_datasets`'s own `registry.yaml` files, and
``eval_reports_dir``/``exports_dir`` are directories a caller points a CLI
command at. This service never writes to any of them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    host: str = field(
        default_factory=lambda: os.environ.get("ODYSSEY_API_HOST", "127.0.0.1")
    )
    port: int = field(
        default_factory=lambda: int(os.environ.get("ODYSSEY_API_PORT", "8000"))
    )
    journeys_dir: Path = field(
        default_factory=lambda: _env_path(
            "ODYSSEY_API_JOURNEYS_DIR", "./collector-data"
        )
    )
    datasets_registry: Path = field(
        default_factory=lambda: _env_path(
            "ODYSSEY_API_DATASETS_REGISTRY", "data_preparation/datasets/registry.yaml"
        )
    )
    models_registry: Path = field(
        default_factory=lambda: _env_path(
            "ODYSSEY_API_MODELS_REGISTRY", "training/models/registry.yaml"
        )
    )
    eval_registry: Path = field(
        default_factory=lambda: _env_path(
            "ODYSSEY_API_EVAL_REGISTRY", "evaluation/datasets/registry.yaml"
        )
    )
    eval_reports_dir: Path = field(
        default_factory=lambda: _env_path(
            "ODYSSEY_API_EVAL_REPORTS_DIR", "evaluation/reports"
        )
    )
    exports_dir: Path = field(
        default_factory=lambda: _env_path("ODYSSEY_API_EXPORTS_DIR", "./exports")
    )


def get_settings() -> Settings:
    return Settings()
