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
    "JourneyProvenanceOut",
    "JourneySummaryOut",
    "JourneyDetailOut",
    "JourneyPageOut",
    "DatasetVersionOut",
    "DatasetOut",
    "ModelVersionOut",
    "ModelOut",
    "EvalRunOut",
    "EvalRunPageOut",
    "ExportArtifactOut",
    "ExportPageOut",
    "MetricsSnapshotOut",
    "MetricsPageOut",
    "ProductOut",
    "ProductCountOut",
    "ProjectCountOut",
    "DateCountOut",
    "CountsOut",
]


class HealthOut(BaseModel):
    status: str


class StepOut(BaseModel):
    index: int
    trainable_status: str
    message_count: int
    # Schema 2.1, off the step's own assistant turn: who served it and what the
    # caller waited for. `None` for a step recorded before 2.1, or by an
    # integration that cannot time its provider (see `latency_ms` in
    # `docs/journey-schema.md`).
    provider: Optional[str] = None
    latency_ms: Optional[float] = None
    ttft_ms: Optional[float] = None


class JourneyMetricsOut(BaseModel):
    steps: Optional[int] = None
    aggregated_reward: Optional[float] = None
    num_tool_calls: Optional[int] = None
    num_tool_failures: Optional[int] = None
    tool_error_rate: Optional[float] = None


class JourneyProvenanceOut(BaseModel):
    """Schema 2.1: what recorded a journey, what served it, how long it took.

    Averages rather than totals — a journey's turns are a conversation, and the
    number a deployment tunes against is what one reply costs.
    """

    framework: Optional[str] = None
    parent_journey_id: Optional[str] = None
    providers: List[str] = []
    avg_latency_ms: Optional[float] = None
    avg_ttft_ms: Optional[float] = None


class JourneySummaryOut(BaseModel):
    journey_id: str
    date: str
    complete: bool
    provenance: JourneyProvenanceOut = JourneyProvenanceOut()


class JourneyDetailOut(BaseModel):
    journey_id: str
    complete: bool
    incomplete_reason: Optional[str] = None
    metrics: JourneyMetricsOut
    provenance: JourneyProvenanceOut = JourneyProvenanceOut()
    steps: List[StepOut]


class JourneyPageOut(BaseModel):
    items: List[JourneySummaryOut]
    next_cursor: Optional[str] = None
    has_more: bool
    total: int


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


class EvalRunPageOut(BaseModel):
    items: List[EvalRunOut]
    next_cursor: Optional[str] = None
    has_more: bool
    total: int


class ExportArtifactOut(BaseModel):
    name: str
    path: str
    rows: int
    sha256: str


class ExportPageOut(BaseModel):
    items: List[ExportArtifactOut]
    next_cursor: Optional[str] = None
    has_more: bool
    total: int


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


class MetricsPageOut(BaseModel):
    items: List[MetricsSnapshotOut]
    next_cursor: Optional[str] = None
    has_more: bool
    total: int


class ProductOut(BaseModel):
    slug: str
    name: str


class ProductCountOut(BaseModel):
    product_slug: Optional[str] = None
    count: int


class ProjectCountOut(BaseModel):
    product_slug: Optional[str] = None
    project: Optional[str] = None
    count: int


class DateCountOut(BaseModel):
    date: str
    count: int


class CountsOut(BaseModel):
    by_product: List[ProductCountOut]
    by_project: List[ProjectCountOut]
    by_date: List[DateCountOut] = []
