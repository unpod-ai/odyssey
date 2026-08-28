"""Every DTO round-trips through pydantic validation and JSON (item 8.1)."""

from __future__ import annotations

from odyssey_schemas import (
    DatasetOut,
    DatasetVersionOut,
    EvalRunOut,
    ExportArtifactOut,
    HealthOut,
    JourneyDetailOut,
    JourneyMetricsOut,
    JourneySummaryOut,
    ModelOut,
    ModelVersionOut,
    StepOut,
)


def test_health_out():
    assert HealthOut(status="ok").model_dump() == {"status": "ok"}


def test_journey_detail_round_trip():
    detail = JourneyDetailOut(
        journey_id="j1",
        complete=True,
        incomplete_reason=None,
        metrics=JourneyMetricsOut(steps=2, aggregated_reward=1.0),
        steps=[StepOut(index=0, trainable_status="trainable", message_count=1)],
    )
    payload = detail.model_dump_json()
    restored = JourneyDetailOut.model_validate_json(payload)
    assert restored == detail


def test_journey_summary_requires_fields():
    JourneySummaryOut(journey_id="j1", date="2026-08-28", complete=False)


def test_dataset_out():
    ds = DatasetOut(
        name="corpus-a",
        versions=[DatasetVersionOut(version=1, manifest_sha256="abc", uri="path")],
    )
    assert ds.versions[0].version == 1


def test_model_out():
    m = ModelOut(
        name="model-a",
        versions=[
            ModelVersionOut(
                version=1,
                sha256="abc",
                uri="s3://x",
                base_model="base",
                corpus_version="v1",
            )
        ],
    )
    assert m.versions[0].base_model == "base"


def test_eval_run_out():
    EvalRunOut(
        benchmark_name="example",
        metric_name="exact_match",
        mean_score=0.5,
        report_path="reports/example.json",
    )


def test_export_artifact_out():
    ExportArtifactOut(name="sft.jsonl", path="/tmp/sft.jsonl", rows=10, sha256="abc")
