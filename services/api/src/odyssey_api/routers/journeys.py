from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from odyssey_schemas import (
    CountsOut,
    JourneyDetailOut,
    JourneyMetricsOut,
    JourneyPageOut,
    JourneySummaryOut,
    ProductCountOut,
    ProjectCountOut,
    DateCountOut,
    StepOut,
)

from odyssey_api import deps
from odyssey_api.deps import get_index_dep
from odyssey_api.domain import journeys as domain
from odyssey_api.index.manager import IndexHandle
from odyssey_api.pagination import paginate
from odyssey_api.settings import Settings

router = APIRouter(prefix="/journeys", tags=["journeys"])


@router.get("", response_model=JourneyPageOut)
def list_journeys(
    product: Optional[str] = None,
    date: Optional[str] = None,
    cursor: Optional[str] = None,
    limit: Optional[int] = None,
    index: IndexHandle = Depends(get_index_dep),
) -> JourneyPageOut:
    """``?product=<slug>``/``?date=<YYYY-MM-DD>`` filter server-side against
    the SQLite index (see `odyssey_api.index`) rather than the filesystem.
    ``?cursor=``/``?limit=`` paginate the (already-filtered) result."""
    all_journeys = [
        JourneySummaryOut(journey_id=journey_id, date=journey_date, complete=complete)
        for journey_id, journey_date, complete in domain.list_journeys_with_status_indexed(
            index, product, date
        )
    ]
    items, next_cursor, has_more, total = paginate(all_journeys, cursor, limit)
    return JourneyPageOut(items=items, next_cursor=next_cursor, has_more=has_more, total=total)


@router.get("/counts", response_model=CountsOut)
def journey_counts(index: IndexHandle = Depends(get_index_dep)) -> CountsOut:
    """Registered before `/{journey_id}` -- otherwise FastAPI would match
    ``GET /journeys/counts`` as ``GET /journeys/{journey_id}`` with
    ``journey_id="counts"``."""
    by_product = index.query(
        "SELECT product_slug, COUNT(*) AS count FROM journeys GROUP BY product_slug"
    )
    by_project = index.query(
        "SELECT product_slug, project, COUNT(*) AS count FROM journeys GROUP BY product_slug, project"
    )
    by_date = index.query(
        "SELECT date, COUNT(*) AS count FROM journeys GROUP BY date ORDER BY date"
    )
    return CountsOut(
        by_product=[ProductCountOut(**dict(r)) for r in by_product],
        by_project=[ProjectCountOut(**dict(r)) for r in by_project],
        by_date=[DateCountOut(**dict(r)) for r in by_date],
    )


@router.get("/{journey_id}", response_model=JourneyDetailOut)
def get_journey(
    journey_id: str, settings: Settings = Depends(deps.get_settings_dep)
) -> JourneyDetailOut:
    try:
        result = domain.get_journey(settings.journeys_dir, journey_id)
    except domain.JourneyNotFoundError:
        raise HTTPException(status_code=404, detail=f"journey {journey_id!r} not found")

    metrics = result.journey.metrics
    return JourneyDetailOut(
        journey_id=result.journey_id,
        complete=result.complete,
        incomplete_reason=result.incomplete_reason,
        metrics=JourneyMetricsOut(
            steps=metrics.steps if metrics else None,
            aggregated_reward=metrics.aggregated_reward if metrics else None,
            num_tool_calls=metrics.num_tool_calls if metrics else None,
            num_tool_failures=metrics.num_tool_failures if metrics else None,
            tool_error_rate=metrics.tool_error_rate if metrics else None,
        ),
        steps=[
            StepOut(
                index=i,
                trainable_status=step.trainable_status,
                message_count=len(step.messages),
            )
            for i, step in enumerate(result.journey.steps)
        ],
    )
