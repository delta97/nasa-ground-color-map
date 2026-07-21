"""Optional, tightly-scoped OpenRouter interpretation of derived observations."""

import json
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from ..gibs.client import GibsClient
from . import deps
from .routes_color import color, color_matrix
from .routes_snow import snow_stats

router = APIRouter(prefix="/v1", tags=["interpretation"])


class InterpretationRequest(BaseModel):
    bbox: str = Field(description="minLon,minLat,maxLon,maxLat")
    date_mode: Literal["previous_completed_day", "latest", "request"] = "previous_completed_day"
    date: str | None = None
    layer: str | None = None
    rows: int = Field(16, ge=1, le=256)
    cols: int = Field(16, ge=1, le=256)
    question: str | None = Field(None, max_length=800)


class InterpretationResult(BaseModel):
    summary: str = Field(max_length=2000)
    observations: list[str] = Field(max_length=12)
    confidence: Literal["low", "medium", "high"]
    limitations: list[str] = Field(max_length=12)
    recommended_next_checks: list[str] = Field(max_length=12)


class InterpretationResponse(BaseModel):
    interpretation: InterpretationResult
    observation_evidence: dict = Field(description="Derived observation evidence supplied to the model")


def _enabled(settings) -> bool:
    return bool(settings.openrouter_api_key and settings.openrouter_model and settings.interpretation_access_token)


def _date_argument(request: InterpretationRequest) -> str | None:
    if request.date_mode == "previous_completed_day":
        return None
    if request.date_mode == "latest":
        return "latest"
    if not request.date:
        raise HTTPException(422, "date is required when date_mode is 'request'")
    return request.date


def _derived_evidence(composite, matrix, snow) -> dict:
    # Never include source pixels/matrices or imagery in this payload.
    return {
        "date": composite.date,
        "date_resolved_from": composite.date_resolved_from,
        "bbox": composite.bbox,
        "layer": composite.layer,
        "grid_dimensions": {"rows": matrix.rows, "cols": matrix.cols},
        "aggregate_color": {"rgb": composite.rgb, "hex": composite.hex},
        "color_quality": composite.observation_quality.model_dump(),
        "snow": {
            "snow_fraction": snow.snow_fraction,
            "valid_fraction": snow.valid_fraction,
            "cloud_fraction": snow.cloud_fraction,
            "water_fraction": snow.water_fraction,
            "quality": snow.observation_quality.model_dump(),
        },
    }


SYSTEM_PROMPT = """You interpret only the supplied derived satellite-observation evidence.
State uncertainty plainly. Do not infer facts not supported by the evidence. Do not provide
scientific, medical, legal, safety-critical, operational, or emergency conclusions. Return JSON
with exactly: summary, observations, confidence (low|medium|high), limitations,
recommended_next_checks. Keep all statements observational and advise verification where needed."""


@router.get("/features", summary="Non-secret capability flags")
async def features(client: GibsClient = Depends(deps.get_client)):
    settings = client.settings
    result = {"interpretation": _enabled(settings)}
    # Preserve the exact legacy response while disabled; advertise the additive
    # capability when monitoring has actually been configured.
    if settings.monitoring_enabled and settings.monitoring_admin_token:
        result["monitoring"] = True
    return result


@router.post("/interpret", response_model=InterpretationResponse, summary="Optional derived-metrics interpretation")
async def interpret(
    request: InterpretationRequest,
    x_interpretation_token: str | None = Header(None),
    client: GibsClient = Depends(deps.get_client),
    latest_dates=Depends(deps.get_latest_dates),
):
    settings = client.settings
    if not _enabled(settings):
        raise HTTPException(503, "interpretation is not configured")
    if x_interpretation_token != settings.interpretation_access_token:
        raise HTTPException(401, "invalid or missing interpretation token")

    date = _date_argument(request)
    # Re-run server-side from the declared request; the model receives only derived metrics.
    matrix = await color_matrix(request.bbox, date, request.layer, request.rows, request.cols, client, latest_dates)
    composite = await color(request.bbox, date, request.layer, client, latest_dates)
    snow = await snow_stats(request.bbox, date, request.rows, request.cols, client, latest_dates)
    evidence = _derived_evidence(composite, matrix, snow)
    user_content = json.dumps({"evidence": evidence, "question": request.question}, separators=(",", ":"))
    payload = {
        "model": settings.openrouter_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
    }
    try:
        response = await client.http.post(
            "https://openrouter.ai/api/v1/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
            timeout=settings.request_timeout_seconds,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        parsed = InterpretationResult.model_validate_json(content)
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        raise HTTPException(502, "interpretation provider returned an invalid response") from exc
    # Raw provider text is intentionally not stored or returned.
    return {"interpretation": parsed.model_dump(), "observation_evidence": evidence}
