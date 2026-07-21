"""Raster bundles and Web Mercator renderer endpoints."""

from __future__ import annotations

from dataclasses import asdict
from io import BytesIO
import math

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from PIL import Image

from ..gibs import layers as layer_registry
from ..gibs.client import GibsClient
from ..gibs.tilemath import BBox, plan_tiles
from ..processing import colors, exports, mosaic, quality
from ..processing.temporal import color_rank_key, composite_rgb, inclusive_dates
from . import deps

router = APIRouter(prefix="/v1", tags=["exports"])


async def _single_matrix(client, latest_dates, box, rows, cols, date=None, layer_id=None):
    layer = layer_registry.get_layer(layer_id) if layer_id else layer_registry.DEFAULT_TRUECOLOR
    if layer is None or layer.kind != "truecolor": raise HTTPException(400, "unknown true-color layer")
    concrete, resolved = deps.resolve_date(date, layer.id, latest_dates, client.settings)
    plan = plan_tiles(box, rows, cols, layer.max_zoom, client.settings.max_tiles_per_request, layer.tile_px)
    tiles = await client.fetch_plan(layer, concrete, plan)
    cropped, missing = mosaic.stitch_and_crop(tiles, plan, "RGB")
    return {"bbox": [box.min_lon, box.min_lat, box.max_lon, box.max_lat], "rows": rows, "cols": cols,
            "date": concrete, "date_resolved_from": resolved, "layer": layer.id,
            "matrix": colors.to_grid(cropped, rows, cols),
            "observation_quality": asdict(quality.color_quality(cropped, missing, plan.tile_count))}


async def _observation(request, body, client, latest_dates):
    obs = body.get("observation", {})
    rows, cols = int(body.get("rows", 256)), int(body.get("cols", 256))
    if body.get("geometry"):
        from .routes_areas import sample_geometry_payload
        nested = dict(body); nested["request"] = request
        return await sample_geometry_payload(nested, client, latest_dates)
    box = deps.parse_bbox(body.get("bbox", ""), client.settings)
    mode = obs.get("mode", "single")
    if mode == "single": return await _single_matrix(client, latest_dates, box, rows, cols, obs.get("date"), obs.get("layer"))
    from .routes_temporal import best, composite
    if mode == "composite":
        return await composite(request, body["bbox"], obs.get("end"), int(obs.get("days", 7)), obs.get("layer"), rows, cols, client)
    if mode == "best":
        selection = await best(request, body["bbox"], "color", obs.get("end"), int(obs.get("lookback_days", 7)), obs.get("layer"), client)
        return await _single_matrix(client, latest_dates, box, rows, cols, selection["selected"]["date"], obs.get("layer"))
    raise HTTPException(400, "observation mode must be single, best, or composite")


@router.post("/exports", summary="Create a georeferenced raster ZIP bundle")
async def create_export(request: Request, body: dict, client: GibsClient = Depends(deps.get_client), latest_dates=Depends(deps.get_latest_dates)):
    kind = body.get("format", "png-bundle")
    aliases = {"png": "png-bundle", "geotiff": "geotiff-bundle", "cog": "cog-bundle"}
    kind = aliases.get(kind, kind)
    if kind not in {"png-bundle", "geotiff-bundle", "cog-bundle"}:
        raise HTTPException(400, "format must be png-bundle, geotiff-bundle, or cog-bundle")
    sampled = await _observation(request, body, client, latest_dates)
    bbox = sampled["bbox"]
    metadata = {"bbox": bbox, "crs": "EPSG:4326", "origin": "northwest",
                "transform": [(bbox[2]-bbox[0])/sampled["cols"], 0, bbox[0], 0, -(bbox[3]-bbox[1])/sampled["rows"], bbox[3]],
                "date": sampled.get("date"), "dates_used": sampled.get("dates_used"), "layer": sampled.get("layer"),
                "quality": sampled.get("observation_quality"), "attribution": "NASA GIBS, NASA/GSFC/ESDIS",
                "generation_parameters": {k: v for k, v in body.items() if k != "geometry"}}
    try: data = exports.raster_bundle(sampled["matrix"], bbox, metadata, kind, sampled.get("observation_counts"))
    except RuntimeError as exc: raise HTTPException(501, str(exc))
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="ground-color-{kind}.zip"'})


def xyz_bbox(z: int, x: int, y: int) -> BBox:
    n = 2 ** z
    if not (0 <= x < n and 0 <= y < n): raise HTTPException(400, "x and y must be valid for zoom z")
    def lat(row): return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * row / n))))
    return BBox(x / n * 360 - 180, lat(y + 1), (x + 1) / n * 360 - 180, lat(y))


@router.get("/tiles/{z}/{x}/{y}.png", summary="Derived 256px Web Mercator XYZ tile")
async def xyz_tile(request: Request, z: int, x: int, y: int, mode: str = "single", date: str | None = None,
                   end: str | None = None, days: int = 7, layer: str | None = None,
                   client: GibsClient = Depends(deps.get_client), latest_dates=Depends(deps.get_latest_dates)):
    if not 0 <= z <= 12: raise HTTPException(400, "zoom must be in [0, 12]")
    if mode not in {"single", "best", "composite"}: raise HTTPException(400, "mode must be single, best, or composite")
    box = xyz_bbox(z, x, y)
    key = f"xyz:{z}:{x}:{y}:{mode}:{date}:{end}:{days}:{layer}"
    if (cached := request.app.state.result_cache.get(key)) is not None:
        return Response(cached, media_type="image/png", headers={"Cache-Control": "public, max-age=21600", "X-Derived-Cache": "hit"})
    if mode == "single":
        sampled = await _single_matrix(client, latest_dates, box, 256, 256, date, layer)
    else:
        from datetime import datetime, timedelta
        lyr = layer_registry.get_layer(layer) if layer else layer_registry.DEFAULT_TRUECOLOR
        normal_end, _ = deps.resolve_date(end, lyr.id, latest_dates, client.settings)
        end_day = datetime.strptime(normal_end, "%Y-%m-%d").date()
        if not 1 <= days <= client.settings.max_composite_days: raise HTTPException(400, f"days must be in [1, {client.settings.max_composite_days}]")
        daily = []
        for day in inclusive_dates(end_day - timedelta(days=days - 1), end_day):
            item = await _single_matrix(client, latest_dates, box, 256, 256, day, layer); daily.append(item)
        if mode == "best": sampled = sorted(daily, key=color_rank_key)[0]
        else:
            usable = [item for item in daily if item["observation_quality"]["status"] != "unusable"]
            matrix, counts, rgb = composite_rgb([item["matrix"] for item in usable], minimum_observations=client.settings.min_composite_observations)
            sampled = {"matrix": matrix, "observation_counts": counts, "rgb": rgb}
    array = exports.rgba_array(sampled["matrix"])
    out = BytesIO(); Image.fromarray(array).save(out, "PNG")
    data = out.getvalue(); request.app.state.result_cache.set(key, data, request.app.state.settings.derived_cache_ttl_seconds)
    return Response(data, media_type="image/png", headers={"Cache-Control": "public, max-age=21600", "X-Derived-Cache": "miss"})


@router.get("/tilejson.json", summary="TileJSON metadata for the derived renderer")
async def tilejson(request: Request, mode: str = Query("single", pattern="^(single|best|composite)$"),
                   date: str | None = None, end: str | None = None, days: int = 7, layer: str | None = None):
    params = [f"mode={mode}"]
    for key, value in (("date", date), ("end", end), ("days", days), ("layer", layer)):
        if value is not None: params.append(f"{key}={value}")
    base = str(request.base_url).rstrip("/")
    return {"tilejson": "3.0.0", "name": "NASA Ground Color", "tiles": [f"{base}/v1/tiles/{{z}}/{{x}}/{{y}}.png?{'&'.join(params)}"],
            "bounds": [-180, -85.05112878, 180, 85.05112878], "minzoom": 0, "maxzoom": 12,
            "attribution": "NASA GIBS, NASA/GSFC/ESDIS", "observation": {"mode": mode, "date": date, "end": end, "days": days, "layer": layer}}
