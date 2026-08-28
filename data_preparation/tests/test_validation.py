"""Validation: schema, PII-redaction, leakage, drift (item 3.6)."""

from __future__ import annotations

import json

from odyssey_dataprep.validation import (
    check_drift,
    check_leakage,
    check_pii_redaction,
    compute_stats,
    validate_dir,
    validate_schema,
)

GOOD_JOURNEY = {
    "task": {"conversation_id": "j1"},
    "steps": [
        {
            "trainable_status": "trainable",
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hey"},
            ],
        }
    ],
}


def test_validate_schema_accepts_a_well_formed_journey():
    assert validate_schema(GOOD_JOURNEY) == []


def test_validate_schema_rejects_missing_task():
    errors = validate_schema({"steps": []})
    assert any("task" in e for e in errors)


def test_validate_schema_rejects_bad_role():
    journey = {
        "task": {},
        "steps": [{"messages": [{"role": "narrator", "content": "x"}]}],
    }
    errors = validate_schema(journey)
    assert any("role" in e for e in errors)


def test_validate_schema_rejects_bad_trainable_status():
    journey = {"task": {}, "steps": [{"trainable_status": "maybe", "messages": []}]}
    errors = validate_schema(journey)
    assert any("trainable_status" in e for e in errors)


def test_check_pii_redaction_passes_when_masked():
    journey = {"metadata": {"api_key": "[REDACTED]"}}
    assert check_pii_redaction(journey) == []


def test_check_pii_redaction_flags_a_leaked_secret():
    journey = {"metadata": {"api_key": "sk-live-123"}}
    errors = check_pii_redaction(journey)
    assert len(errors) == 1
    assert "api_key" in errors[0]


def test_check_pii_redaction_ignores_content_by_default():
    journey = {"steps": [{"messages": [{"role": "user", "content": "a@b.com"}]}]}
    assert check_pii_redaction(journey) == []


def test_check_pii_redaction_flags_content_when_rules_given():
    journey = {"steps": [{"messages": [{"role": "user", "content": "email a@b.com"}]}]}
    errors = check_pii_redaction(journey, content_rules=["EMAIL"])
    assert len(errors) == 1
    assert "EMAIL" in errors[0]
    assert "message.content" in errors[0]


def test_check_pii_redaction_only_scans_requested_content_rules():
    journey = {
        "steps": [{"messages": [{"role": "user", "content": "call 555-123-4567"}]}]
    }
    assert check_pii_redaction(journey, content_rules=["EMAIL"]) == []


def test_check_leakage_flags_an_id_in_two_splits():
    errors = check_leakage({"train": ["a", "b"], "val": ["b", "c"]})
    assert len(errors) == 1
    assert "b" in errors[0]


def test_check_leakage_passes_disjoint_splits():
    assert check_leakage({"train": ["a"], "val": ["b"], "test": ["c"]}) == []


def test_check_drift_flags_a_large_relative_change():
    errors = check_drift({"mean_steps": 5.0}, {"mean_steps": 2.0}, threshold=0.2)
    assert len(errors) == 1


def test_check_drift_ignores_a_small_relative_change():
    errors = check_drift({"mean_steps": 2.1}, {"mean_steps": 2.0}, threshold=0.2)
    assert errors == []


def test_compute_stats_over_a_directory(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps(GOOD_JOURNEY), encoding="utf-8")
    stats = compute_stats(tmp_path)
    assert stats["journeys"] == 1
    assert stats["mean_steps"] == 1.0
    assert stats["mean_messages"] == 2.0


def test_validate_dir_ok_on_clean_input(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps(GOOD_JOURNEY), encoding="utf-8")
    result = validate_dir(tmp_path)
    assert result.ok


def test_validate_dir_reports_schema_and_pii_breaches(tmp_path):
    bad = {"metadata": {"password": "hunter2"}, "steps": "not-a-list"}
    (tmp_path / "a.json").write_text(json.dumps(bad), encoding="utf-8")
    result = validate_dir(tmp_path)
    assert not result.ok
    assert any("steps" in e for e in result.errors)
    assert any("password" in e for e in result.errors)


def test_validate_dir_checks_leakage_when_splits_given(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps(GOOD_JOURNEY), encoding="utf-8")
    result = validate_dir(tmp_path, splits={"train": ["j1"], "val": ["j1"]})
    assert not result.ok
