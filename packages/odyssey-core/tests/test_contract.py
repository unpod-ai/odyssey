"""The cross-project contract: the golden fixture, the round-trip, no coupling.

These are the tests superdialog's producer side must also satisfy. If any of them
changes, the interface between the two projects has changed and both sides need
to know — that is the whole reason the fixture is committed rather than generated
at test time.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from odyssey.fold import fold
from odyssey.jsonl import encode_event, read_events, read_schema_version, write_events
from odyssey.primitives import SCHEMA_VERSION, Step
from odyssey.spool import Spool, SpoolConfig

REPO = Path(__file__).resolve().parent.parent
GOLDEN = REPO / "tests/fixtures/golden_journey.jsonl"


@pytest.fixture
def golden_events():
    return read_events(GOLDEN).events


# --------------------------------------------------------------------------
# 6.1 — the golden fixture
# --------------------------------------------------------------------------


def test_golden_fixture_is_committed_and_readable():
    assert GOLDEN.exists(), "the shared contract artifact must be committed"
    result = read_events(GOLDEN)
    assert result.clean
    assert len(result.events) == 12
    assert result.schema_version == SCHEMA_VERSION


def test_golden_fixture_is_not_stale():
    """Regenerating must be a no-op — otherwise the committed bytes lie."""
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts/make_golden.py"), "--check"],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    assert proc.returncode == 0, proc.stderr


def test_golden_fixture_covers_every_event_kind(golden_events):
    assert {e.kind for e in golden_events} == {
        "message",
        "signal",
        "reward",
        "terminal",
    }


def test_golden_fixture_preserves_tool_call_correlation(golden_events):
    """tool_call_id ↔ tool-result linkage is the thing Soup's tool-calling
    format destroys; the wire format must not."""
    call = next(e for e in golden_events if e.message and e.message.tool_calls)
    resp = next(e for e in golden_events if e.message and e.message.tool_response)
    assert call.message.tool_calls[0].id == resp.message.tool_response.id


def test_golden_fixture_folds_to_a_complete_journey(golden_events):
    r = fold(golden_events, data_source="golden")
    assert r.complete is True
    assert r.trainable is True
    assert r.missing_seqs == []
    assert r.duplicates_dropped == 0
    assert r.rejected_after_terminal == 0


def test_golden_fixture_yields_a_usable_preference_chain(golden_events):
    """A regenerated answer, an edit, and a thumbs-up — the minimum for DPO."""
    r = fold(golden_events, data_source="golden")
    statuses = {
        m.content: m.trainable_status for m in r.journey.steps[-1].messages if m.content
    }
    assert statuses["Booked!"] == "superseded"
    assert statuses["Booked for Tuesday at 3pm."] == "superseded"
    assert statuses["You're all set for Tuesday at 3pm."] == "trainable"
    assert {s.signal for s in r.signals} == {"regenerated", "user_edit", "thumbs_up"}


def test_golden_fixture_reward_survives_the_wire(golden_events):
    r = fold(golden_events, data_source="golden")
    assert r.journey.metrics.aggregated_reward == 0.92
    assert [c.name for c in r.journey.reward.components] == [
        "task_success",
        "efficiency",
    ]
    assert r.journey.reward.components[0].range == (0.0, 1.0)


# --------------------------------------------------------------------------
# 6.3 — write → read → fold → project → re-serialize
# --------------------------------------------------------------------------


def test_full_round_trip_reserializes_to_events_only(tmp_path, golden_events):
    out = tmp_path / "rt.jsonl"
    write_events(out, golden_events)
    reread = read_events(out).events
    assert reread == golden_events

    folded = fold(reread, data_source="golden")
    assert folded.journey.steps  # the projection happened

    # Re-serializing the journey goes back to EVENTS. No Step ever hits the wire.
    again = tmp_path / "rt2.jsonl"
    write_events(again, reread)
    text = again.read_text()
    assert "steps" not in text
    for line in text.splitlines()[1:]:
        obj = json.loads(line)
        assert set(obj) <= {
            "journey_id",
            "seq",
            "kind",
            "ts",
            "event_id",
            "message",
            "signal",
            "reward",
            "terminal",
            "model_id",
            "metadata",
        }


def test_no_step_record_is_ever_encoded():
    """Step is not part of the wire vocabulary at all."""
    assert "Step" not in {"message", "signal", "reward", "terminal"}
    encoded = encode_event(read_events(GOLDEN).events[0])
    assert "trainable_status" in encoded  # per-message, yes
    assert "messages" not in encoded  # cumulative lists, never


def test_spool_to_fold_is_lossless(tmp_path, golden_events):
    """The path a real producer takes: record locally, drain, fold."""
    s = Spool(SpoolConfig(root=tmp_path / "spool"))
    s.record_all(golden_events)
    recovered = s.read("j_golden_0001")
    assert [e.seq for e in recovered] == list(range(12))
    assert fold(recovered, data_source="golden").complete is True


def test_projection_is_cumulative_and_monotonic(golden_events):
    steps = fold(golden_events, data_source="golden").journey.steps
    counts = [len(s.messages) for s in steps]
    assert counts == sorted(counts)
    assert all(isinstance(s, Step) for s in steps)


def test_schema_version_readable_from_the_fixture_header_alone():
    assert read_schema_version(GOLDEN) == SCHEMA_VERSION


# --------------------------------------------------------------------------
# 6.2 — no import coupling in either direction
# --------------------------------------------------------------------------


def test_odyssey_does_not_depend_on_superdialog():
    meta = tomllib.loads((REPO / "pyproject.toml").read_text())
    declared = " ".join(
        meta["project"].get("dependencies", [])
        + [
            spec
            for group in meta["project"].get("optional-dependencies", {}).values()
            for spec in group
        ]
    ).lower()
    assert "superdialog" not in declared


def test_odyssey_source_never_imports_superdialog():
    """Parsed imports, not a text grep.

    A grep would flag the comments that *describe* the relationship — and those
    comments are the documentation of the seam, so they must stay legal.
    """
    offenders: list[str] = []
    for p in (REPO / "src").rglob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            if any(n.split(".")[0] == "superdialog" for n in names):
                offenders.append(f"{p.relative_to(REPO)}:{node.lineno}")
    assert offenders == [], f"import coupling introduced at {offenders}"


def test_superdialog_does_not_depend_on_odyssey():
    """The reverse direction. Skipped when the sibling checkout is absent."""
    sibling = REPO.parent / "superdialog" / "pyproject.toml"
    if not sibling.exists():
        pytest.skip("superdialog checkout not present")
    meta = tomllib.loads(sibling.read_text())
    declared = " ".join(
        meta["project"].get("dependencies", [])
        + [
            spec
            for group in meta["project"].get("optional-dependencies", {}).values()
            for spec in group
        ]
    ).lower()
    assert "odyssey" not in declared


def test_the_contract_is_a_file_format_not_an_import():
    """Documents the intent: the only shared thing is the schema version."""
    assert read_schema_version(GOLDEN) == SCHEMA_VERSION
    assert (REPO / "src/odyssey/jsonl.py").exists()


# --------------------------------------------------------------------------
# The documented quickstart — the most-read code in the project
# --------------------------------------------------------------------------


def test_docs_quickstart_still_works(tmp_path):
    """Mirror of the `docs/README.md` 60-second example.

    Worth a test because the first version shipped broken: it recorded messages
    and no terminal event, so `result.trainable` was False and the example's own
    `train_on(...)` branch never ran. A doc example that silently does nothing is
    worse than no example.
    """
    from odyssey.primitives import JourneyEvent, Message, Terminal
    from odyssey.spool import Spool, SpoolConfig

    spool = Spool(SpoolConfig(root=tmp_path / ".odyssey"))

    def record(seq, **payload):
        spool.record(JourneyEvent(journey_id="call_8891", seq=seq, **payload))

    record(
        0,
        kind="message",
        message=Message(role="user", content="Book me for Tuesday at 3."),
    )
    record(
        1, kind="message", message=Message(role="assistant", content="Booked for 3pm.")
    )
    record(2, kind="terminal", terminal=Terminal(termination_reason="ENV_DONE"))

    result = fold(spool.read("call_8891"), data_source="voice")
    assert result.trainable
    assert len(result.journey.steps) == 2
    assert [s.trainable_status for s in result.journey.steps] == [
        "not_trainable",
        "trainable",
    ]


def test_docs_reference_only_symbols_that_exist():
    """Every `Backtick` symbol in docs/ that looks like ours must resolve.

    Cheap guard against docs drifting after a rename — which this project has
    already done once (Trajectory -> Journey) and will do again for the Soup port.
    """
    import re

    external = {
        "DEFAULT_EXCLUDES",  # black's constant
        "EXPECTED_RAISES",  # lives in scripts/, not src/
        "ModuleNotFoundError",
        "SIGKILL",
        "VIRTUAL_ENV",
        "PATH",
    }
    src_text = "\n".join(
        p.read_text(encoding="utf-8") for p in (REPO / "src").rglob("*.py")
    )
    missing: list[str] = []
    for doc in (REPO / "docs").glob("*.md"):
        for m in re.finditer(r"`([A-Za-z_][A-Za-z0-9_]*)(?:\(\))?`", doc.read_text()):
            sym = m.group(1)
            if len(sym) <= 3 or sym in external:
                continue
            if not (sym[0].isupper() or "_" in sym):
                continue
            if sym not in src_text:
                missing.append(f"{doc.name}: {sym}")
    assert missing == [], f"docs reference symbols that no longer exist: {missing}"
