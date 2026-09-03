"""The generator against the real, committed `services/api/openapi.json` —
not a synthetic schema, so a real drift is what this actually catches."""

from __future__ import annotations

from odyssey_sdk.codegen import check_drift, load_openapi, render_all


def test_committed_openapi_is_the_real_narrow_shape():
    openapi = load_openapi()
    assert "/journeys" in openapi["paths"]
    assert "/health" in openapi["paths"]


def test_render_all_produces_one_module_per_resource():
    rendered = render_all(load_openapi())
    assert set(rendered) == {
        "journeys",
        "datasets",
        "models",
        "runs",
        "exports",
        "metrics",
        "products",
    }
    assert "class JourneysResource:" in rendered["journeys"]


def test_no_drift_against_committed_resources():
    assert check_drift() == []
