"""Corpus versioning: curated_watermark + version = sha(recipe_hash + watermark)
(items 4.4/4.5, design.md Decision 9)."""

from __future__ import annotations

import json

import pytest

from odyssey_dataprep.versioning import compute_curated_watermark, corpus_version


def write_journey(dir_, conversation_id, content_hash_val, **extra_task):
    doc = {
        "task": {"conversation_id": conversation_id, **extra_task},
        "telemetry": {"source": "test", "data": {"content_hash": content_hash_val}},
    }
    (dir_ / f"{conversation_id}.json").write_text(json.dumps(doc), encoding="utf-8")


def test_watermark_is_order_independent(tmp_path):
    a_dir = tmp_path / "a"
    a_dir.mkdir()
    write_journey(a_dir, "j1", "h1")
    write_journey(a_dir, "j2", "h2")

    b_dir = tmp_path / "b"
    b_dir.mkdir()
    write_journey(b_dir, "j2", "h2")
    write_journey(b_dir, "j1", "h1")

    wa = compute_curated_watermark(a_dir, seq=1)
    wb = compute_curated_watermark(b_dir, seq=1)
    assert wa["hash"] == wb["hash"]


def test_watermark_seq_is_passed_through_unaltered(tmp_path):
    d = tmp_path / "curated"
    d.mkdir()
    write_journey(d, "j1", "h1")
    assert compute_curated_watermark(d, seq=47)["seq"] == 47


def test_watermark_changes_on_retraction(tmp_path):
    d = tmp_path / "curated"
    d.mkdir()
    write_journey(d, "j1", "h1")
    write_journey(d, "j2", "h2")
    before = compute_curated_watermark(d, seq=1)["hash"]

    (d / "j2.json").unlink()
    after = compute_curated_watermark(d, seq=1)["hash"]
    assert before != after


def test_watermark_changes_on_correction_even_if_id_set_is_unchanged(tmp_path):
    d = tmp_path / "curated"
    d.mkdir()
    write_journey(d, "j1", "h1")
    before = compute_curated_watermark(d, seq=1)["hash"]

    write_journey(d, "j1", "h1-corrected")
    after = compute_curated_watermark(d, seq=1)["hash"]
    assert before != after


def test_watermark_requires_a_journey_id(tmp_path):
    d = tmp_path / "curated"
    d.mkdir()
    (d / "broken.json").write_text(
        json.dumps({"task": {}, "telemetry": {"data": {"content_hash": "h1"}}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="conversation_id"):
        compute_curated_watermark(d, seq=1)


def test_corpus_version_is_deterministic_and_sensitive_to_both_inputs():
    watermark = {"seq": 1, "hash": "h"}
    v = corpus_version("recipe-hash-1", watermark)
    assert v == corpus_version("recipe-hash-1", watermark)
    assert v != corpus_version("recipe-hash-2", watermark)
    assert v != corpus_version("recipe-hash-1", {"seq": 2, "hash": "h"})
