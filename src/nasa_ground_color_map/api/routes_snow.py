from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Query

from ..gibs.client import GibsClient
from ..gibs.layers import SNOW_LAYER
from ..gibs.tilemath import pixel_deg, plan_tiles
from ..processing import mosaic, quality, snow
from . import deps
from .schemas import SnowResponse, SourceInfo

router = APIRouter(prefix="/v1", tags=["snow"])


@router.get(
    "/snow",
    response_model=SnowResponse,
    summary="Snow-cover statistics for a bounding box",
    description="Snow-cover fraction from the MODIS NDSI Snow Cover layer. "
    "snow_fraction is the mean NDSI (0..1) over observable land pixels; check "
    "valid_fraction and cloud_fraction to judge how trustworthy the reading is. "
    "Note: this layer lags a day or two behind real time and is unavailable at night "
    "or under heavy cloud.",
)
async def snow_stats(
    bbox: str = Query(..., description="minLon,minLat,maxLon,maxLat"),
    date: str | None = Query(None, description="YYYY-MM-DD, 'latest', or omit for the previous completed UTC day"),
    rows: int = Query(1, ge=1, le=256),
    cols: int = Query(1, ge=1, le=256),
    client: GibsClient = Depends(deps.get_client),
    latest_dates=Depends(deps.get_latest_dates),
):
    settings = client.settings
    box = deps.parse_bbox(bbox, settings)
    deps.validate_grid(rows, cols, settings)
    layer = SNOW_LAYER
    concrete_date, resolved_from = deps.resolve_date(date, layer.id, latest_dates, settings)
    try:
        plan = plan_tiles(box, rows, cols, layer.max_zoom, settings.max_tiles_per_request, layer.tile_px)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    tiles = await client.fetch_plan(layer, concrete_date, plan)
    cropped, missing = mosaic.stitch_and_crop(tiles, plan, mode="P")
    stats = snow.analyze(cropped)
    matrix = snow.analyze_grid(cropped, rows, cols) if rows * cols > 1 else None
    return SnowResponse(
        bbox=[box.min_lon, box.min_lat, box.max_lon, box.max_lat],
        date=concrete_date,
        date_resolved_from=resolved_from,
        layer=layer.id,
        snow_fraction=round(stats.snow_fraction, 4) if stats.snow_fraction is not None else None,
        valid_fraction=round(stats.valid_fraction, 4),
        cloud_fraction=round(stats.cloud_fraction, 4),
        water_fraction=round(stats.water_fraction, 4),
        matrix=matrix,
        source=SourceInfo(
            zoom=plan.zoom,
            zoom_degraded=plan.degraded,
            tiles_fetched=plan.tile_count,
            tiles_missing=missing,
            native_pixel_deg=pixel_deg(plan.zoom, layer.tile_px),
        ),
        observation_quality=asdict(quality.snow_quality(
            observable_fraction=stats.valid_fraction,
            cloud_fraction=stats.cloud_fraction,
            water_fraction=stats.water_fraction,
            tiles_missing=missing,
            tiles_fetched=plan.tile_count,
        )),
    )
