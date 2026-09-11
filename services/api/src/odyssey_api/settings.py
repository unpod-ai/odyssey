"""Env-first config, explicit argument wins — same precedence rule every
other member (`odyssey.config.resolve()`, `services/collector`'s server
config) already uses.

Every path here points at storage another member already owns and writes:
`journeys_dir` is `services/collector`'s own `--data-dir` — either the flat
``<date>/<journey_id>.jsonl`` layout or a product-scoped
``<product_slug>/<date>/<journey_id>.jsonl`` one (`--products-file`); both
`/journeys` (`repositories/filesystem.list_journeys`/`find_journey_path`)
and `/metrics` (`repositories/filesystem.list_metrics`) handle both
layouts. The three registry paths are
`odyssey_dataprep.datasets` / `odyssey_training.models_registry` /
`odyssey_eval.eval_datasets`'s own `registry.yaml` files, and
``eval_reports_dir``/``exports_dir`` are directories a caller points a CLI
command at. ``products_file`` is `services/collector`'s own
``--products-file`` roster (unset by default — most deployments run
single-tenant); ``/products`` returns an empty list, not an error, when
it's unset or missing, mirroring every other optional resource here.
This service never writes to any of them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


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
    products_file: Optional[Path] = field(
        default_factory=lambda: (
            Path(v) if (v := os.environ.get("ODYSSEY_API_PRODUCTS_FILE")) else None
        )
    )
    api_key: Optional[str] = field(
        default_factory=lambda: os.environ.get("ODYSSEY_API_AUTH_KEY")
    )
    db_uri: str = field(
        default_factory=lambda: os.environ.get(
            "ODYSSEY_DB_URI", "sqlite:///./odyssey.sqlite3"
        )
    )
    index_interval_seconds: int = field(
        default_factory=lambda: int(
            os.environ.get("ODYSSEY_API_INDEX_INTERVAL_SECONDS", "5")
        )
    )
    index_reconcile_every: int = field(
        default_factory=lambda: int(
            os.environ.get("ODYSSEY_API_INDEX_RECONCILE_EVERY", "20")
        )
    )


def get_settings() -> Settings:
    return Settings()
