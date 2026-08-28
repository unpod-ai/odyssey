from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from odyssey_schemas import (
    JourneyDetailOut,
    JourneyMetricsOut,
    JourneySummaryOut,
    StepOut,
)

from odyssey_api import deps
from odyssey_api.domain import journeys as domain
from odyssey_api.settings import Settings

router = APIRouter(prefix="/journeys", tags=["journeys"])


@router.get("", response_model=List[JourneySummaryOut])
def list_journeys(
    settings: Settings = Depends(deps.get_settings_dep),
) -> List[JourneySummaryOut]:
    return [
        JourneySummaryOut(journey_id=journey_id, date=date, complete=complete)
        for journey_id, date, complete in domain.list_journeys_with_status(
            settings.journeys_dir
        )
    ]


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
