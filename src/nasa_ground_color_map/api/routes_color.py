from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Query

from ..gibs import layers as layer_registry
from ..gibs.client import GibsClient
from ..gibs.tilemath import BBox, pixel_deg, plan_tiles
from ..processing import colors, mosaic, quality
from . import deps
from .schemas import (
    ColorMatrixResponse,
    ColorResponse,
    LayerInfo,
    LayersResponse,
    SourceInfo,
)

router = APIRouter(prefix="/v1", tags=["color"])

CLOUD_NOTE = (
    "Single-day satellite imagery frequently contains clouds (bright white/grey). "
    "If the result looks cloudy, try adjacent dates."
)


def _resolve_truecolor_layer(layer_id: str | None):
    if layer_id is None:
        return layer_registry.DEFAULT_TRUECOLOR
    layer = layer_registry.get_layer(layer_id)
    if layer is None or layer.kind != "truecolor":
        valid = [l.id for l in layer_registry.TRUECOLOR_LAYERS]
        raise HTTPException(400, f"unknown layer '{layer_id}'; valid: {valid}")
    return layer


async def _fetch_cropped(client: GibsClient, layer, date: str, bbox: BBox, rows: int, cols: int):
    settings = client.settings
    try:
        plan = plan_tiles(bbox, rows, cols, layer.max_zoom, settings.max_tiles_per_request, layer.tile_px)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    tiles = await client.fetch_plan(layer, date, plan)
    cropped, missing = mosaic.stitch_and_crop(tiles, plan, mode="RGB")
    source = SourceInfo(
        zoom=plan.zoom,
        zoom_degraded=plan.degraded,
        tiles_fetched=plan.tile_count,
        tiles_missing=missing,
        native_pixel_deg=pixel_deg(plan.zoom, layer.tile_px),
    )
    return cropped, source


@router.get(
    "/color-matrix",
    response_model=ColorMatrixResponse,
    summary="Grid of average ground colors for a bounding box",
    description="Area-averaged RGB color per grid cell, sampled from NASA GIBS daily "
    "true-color imagery. Row 0 is the northernmost row; cell [0][0] is the northwest corner. "
    + CLOUD_NOTE,
)
async def color_matrix(
    bbox: str = Query(..., description="minLon,minLat,maxLon,maxLat"),
    date: str | None = Query(None, description="YYYY-MM-DD, 'latest', or omit for the previous completed UTC day"),
    layer: str | None = Query(None, description="True-color layer id (see /v1/layers)"),
    rows: int = Query(16, ge=1, le=256),
    cols: int = Query(16, ge=1, le=256),
    client: GibsClient = Depends(deps.get_client),
    latest_dates=Depends(deps.get_latest_dates),
):
    settings = client.settings
    box = deps.parse_bbox(bbox, settings)
    deps.validate_grid(rows, cols, settings)
    lyr = _resolve_truecolor_layer(layer)
    concrete_date, resolved_from = deps.resolve_date(date, lyr.id, latest_dates, settings)
    cropped, source = await _fetch_cropped(client, lyr, concrete_date, box, rows, cols)
    matrix = colors.to_grid(cropped, rows, cols)
    return ColorMatrixResponse(
        bbox=[box.min_lon, box.min_lat, box.max_lon, box.max_lat],
        date=concrete_date,
        date_resolved_from=resolved_from,
        layer=lyr.id,
        rows=rows,
        cols=cols,
        cell_size_deg=[box.width / cols, box.height / rows],
        source=source,
        observation_quality=asdict(quality.color_quality(cropped, source.tiles_missing, source.tiles_fetched)),
        matrix=matrix,
    )


@router.get(
    "/color",
    response_model=ColorResponse,
    summary="Single composite ground color for a bounding box",
    description="Area-weighted mean RGB of the bounding box from NASA GIBS daily "
    "true-color imagery. " + CLOUD_NOTE,
)
async def color(
    bbox: str = Query(..., description="minLon,minLat,maxLon,maxLat"),
    date: str | None = Query(None, description="YYYY-MM-DD, 'latest', or omit for the previous completed UTC day"),
    layer: str | None = Query(None, description="True-color layer id (see /v1/layers)"),
    client: GibsClient = Depends(deps.get_client),
    latest_dates=Depends(deps.get_latest_dates),
):
    settings = client.settings
    box = deps.parse_bbox(bbox, settings)
    lyr = _resolve_truecolor_layer(layer)
    concrete_date, resolved_from = deps.resolve_date(date, lyr.id, latest_dates, settings)
    cropped, source = await _fetch_cropped(client, lyr, concrete_date, box, rows=1, cols=1)
    rgb = colors.average(cropped)
    return ColorResponse(
        bbox=[box.min_lon, box.min_lat, box.max_lon, box.max_lat],
        date=concrete_date,
        date_resolved_from=resolved_from,
        layer=lyr.id,
        rgb=list(rgb),
        hex=colors.rgb_to_hex(rgb),
        source=source,
        observation_quality=asdict(quality.color_quality(cropped, source.tiles_missing, source.tiles_fetched)),
    )


@router.get("/layers", response_model=LayersResponse, summary="Available imagery layers")
async def list_layers(latest_dates=Depends(deps.get_latest_dates)):
    infos = [
        LayerInfo(
            id=l.id,
            tile_matrix_set=l.tile_matrix_set,
            format=l.ext,
            max_zoom=l.max_zoom,
            kind=l.kind,
            latest_available_date=latest_dates.latest_for(l.id),
        )
        for l in layer_registry.all_layers()
    ]
    return LayersResponse(default_layer=layer_registry.DEFAULT_TRUECOLOR.id, layers=infos)
