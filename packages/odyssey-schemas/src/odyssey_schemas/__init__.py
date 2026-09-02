"""odyssey-schemas — pydantic DTOs for `services/api` (item 8.1).

Every model here is a stable, narrowed wire shape for data that already has
one real source of truth elsewhere in the monorepo (a dataclass in
`odyssey.primitives`, or a `registry.yaml` entry written by
`odyssey_dataprep.datasets` / `odyssey_training.models_registry` /
`odyssey_eval.eval_datasets`). This package adds no new data and no
business logic — only the response/request shapes `services/api`'s
routers return, and the input `openapi.json` generation (item 8.3) needs.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel

__all__ = [
    "HealthOut",
    "StepOut",
    "JourneyMetricsOut",
    "JourneySummaryOut",
    "JourneyDetailOut",
    "DatasetVersionOut",
    "DatasetOut",
    "ModelVersionOut",
    "ModelOut",
    "EvalRunOut",
    "ExportArtifactOut",
    "MetricsSnapshotOut",
]


class HealthOut(BaseModel):
    status: str


class StepOut(BaseModel):
    index: int
    trainable_status: str
    message_count: int


class JourneyMetricsOut(BaseModel):
    steps: Optional[int] = None
    aggregated_reward: Optional[float] = None
    num_tool_calls: Optional[int] = None
    num_tool_failures: Optional[int] = None
    tool_error_rate: Optional[float] = None


class JourneySummaryOut(BaseModel):
    journey_id: str
    date: str
    complete: bool


class JourneyDetailOut(BaseModel):
    journey_id: str
    complete: bool
    incomplete_reason: Optional[str] = None
    metrics: JourneyMetricsOut
    steps: List[StepOut]


class DatasetVersionOut(BaseModel):
    version: int
    manifest_sha256: str
    uri: str


class DatasetOut(BaseModel):
    name: str
    versions: List[DatasetVersionOut]


class ModelVersionOut(BaseModel):
    version: int
    sha256: str
    uri: str
    base_model: Optional[str] = None
    corpus_version: Optional[str] = None


class ModelOut(BaseModel):
    name: str
    versions: List[ModelVersionOut]


class EvalRunOut(BaseModel):
    benchmark_name: str
    metric_name: str
    mean_score: float
    report_path: str


class ExportArtifactOut(BaseModel):
    name: str
    path: str
    rows: int
    sha256: str


class MetricsSnapshotOut(BaseModel):
    ts: str
    hostname: str
    os: str
    cpu_count: Optional[int] = None
    memory_total_bytes: Optional[int] = None
    memory_available_bytes: Optional[int] = None
    disk_total_bytes: Optional[int] = None
    disk_free_bytes: Optional[int] = None
    project: Optional[str] = None
    public_ip: Optional[str] = None
