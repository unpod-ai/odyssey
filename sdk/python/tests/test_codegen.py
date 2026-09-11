"""The generator against the real, committed `services/api/openapi.json` —
not a synthetic schema, so a real drift is what this actually catches."""

from __future__ import annotations

import pytest

from odyssey_sdk.codegen import (
    UnsupportedOperationError,
    _operations_by_resource,
    check_drift,
    load_openapi,
    render_all,
)


def _get_op(response_ref: str = "#/components/schemas/Widget"):
    return {
        "get": {
            "responses": {
                "200": {
                    "content": {"application/json": {"schema": {"$ref": response_ref}}}
                }
            }
        }
    }


def test_distinct_last_segments_get_distinct_method_names():
    openapi = {
        "paths": {
            "/widgets": _get_op(),
            "/widgets/counts": _get_op(),
            "/widgets/totals": _get_op(),
        }
    }
    by_resource = _operations_by_resource(openapi)
    method_names = {op.method_name for op in by_resource["widgets"]}
    assert method_names == {"list", "counts", "totals"}


def test_colliding_last_segments_raise_instead_of_clobbering():
    openapi = {
        "paths": {
            # Two different resources' sub-paths that happen to share a
            # last segment name would be fine (they're different classes);
            # this is two paths *on the same resource* deriving the same
            # method name -- the exact collision the guard exists for.
            "/widgets/a/counts": _get_op(),
            "/widgets/b/counts": _get_op(),
        }
    }
    with pytest.raises(UnsupportedOperationError):
        _operations_by_resource(openapi)


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
