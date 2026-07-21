"""Bounded multi-date observation APIs."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..gibs import layers as layer_registry
from ..gibs.client import GibsClient
from ..gibs.tilemath import pixel_deg, plan_tiles
from ..processing import colors, mosaic, quality, snow
from ..processing.temporal import color_rank_key, composite_rgb, inclusive_dates, snow_rank_key
from . import deps

router = APIRouter(prefix="/v1", tags=["temporal"])


def _day(raw: str | None, fallback: date, name: str) -> date:
    if raw is None:
        return fallback
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, f"{name} must be YYYY-MM-DD")


def _range(start: str | None, end: str | None, settings, max_days: int | None = None):
    normal_end = deps.utc_today() - timedelta(days=settings.default_imagery_lag_days)
    end_day = _day(end, normal_end, "end")
    start_day = _day(start, end_day - timedelta(days=6), "start")
    days = (end_day - start_day).days + 1
    maximum = max_days or settings.max_history_days
    if days < 1:
        raise HTTPException(400, "start must be on or before end")
    if days > maximum:
        raise HTTPException(400, f"date range must contain at most {maximum} inclusive days")
    return start_day, end_day, normal_end


def _source(plan, missing, layer):
    return {
        "zoom": plan.zoom, "zoom_degraded": plan.degraded,
        "tiles_fetched": plan.tile_count, "tiles_missing": missing,
        "native_pixel_deg": pixel_deg(plan.zoom, layer.tile_px),
    }


async def _color_day(client, layer, day, plan, rows=1, cols=1, include_matrix=False):
    tiles = await client.fetch_plan(layer, day, plan)
    cropped, missing = mosaic.stitch_and_crop(tiles, plan, mode="RGB")
    rgb = colors.average(cropped)
    result = {
        "date": day, "layer": layer.id, "rgb": list(rgb), "hex": colors.rgb_to_hex(rgb),
        "source": _source(plan, missing, layer),
        "observation_quality": asdict(quality.color_quality(cropped, missing, plan.tile_count)),
    }
    if include_matrix:
        result.update(rows=rows, cols=cols, matrix=colors.to_grid(cropped, rows, cols))
    return result


async def _snow_day(client, day, plan):
    layer = layer_registry.SNOW_LAYER
    tiles = await client.fetch_plan(layer, day, plan)
    cropped, missing = mosaic.stitch_and_crop(tiles, plan, mode="P")
    stats = snow.analyze(cropped)
    return {
        "date": day, "layer": layer.id,
        "snow_fraction": round(stats.snow_fraction, 4) if stats.snow_fraction is not None else None,
        "valid_fraction": round(stats.valid_fraction, 4),
        "cloud_fraction": round(stats.cloud_fraction, 4), "water_fraction": round(stats.water_fraction, 4),
        "source": _source(plan, missing, layer),
        "observation_quality": asdict(quality.snow_quality(
            observable_fraction=stats.valid_fraction, cloud_fraction=stats.cloud_fraction,
            water_fraction=stats.water_fraction, tiles_missing=missing, tiles_fetched=plan.tile_count,
        )),
    }


def _cache(request: Request, name: str, params: dict, normal_end: date, actual_end: date):
    key = name + ":" + json.dumps(params, sort_keys=True, separators=(",", ":"))
    ttl = request.app.state.settings.derived_cache_ttl_seconds if actual_end >= normal_end else None
    return request.app.state.result_cache, key, ttl


@router.get("/history", summary="Daily color and snow observations over a bounded date range")
async def history(
    request: Request, bbox: str = Query(...), start: str | None = None, end: str | None = None,
    layer: str | None = None, metrics: str = "color,snow",
    client: GibsClient = Depends(deps.get_client),
):
    settings = client.settings
    box = deps.parse_bbox(bbox, settings)
    start_day, end_day, normal_end = _range(start, end, settings)
    requested = tuple(dict.fromkeys(x.strip() for x in metrics.split(",") if x.strip()))
    if not requested or any(x not in {"color", "snow"} for x in requested):
        raise HTTPException(400, "metrics must contain color and/or snow")
    color_layer = layer_registry.get_layer(layer) if layer else layer_registry.DEFAULT_TRUECOLOR
    if "color" in requested and (color_layer is None or color_layer.kind != "truecolor"):
        raise HTTPException(400, "layer must be a known true-color layer")
    cache, key, ttl = _cache(request, "history", {
        "bbox": [box.min_lon, box.min_lat, box.max_lon, box.max_lat], "start": start_day.isoformat(),
        "end": end_day.isoformat(), "layer": color_layer.id if color_layer else None, "metrics": requested,
    }, normal_end, end_day)
    if (hit := cache.get(key)) is not None:
        return hit
    plans = {}
    try:
        if "color" in requested:
            plans["color"] = plan_tiles(box, 1, 1, color_layer.max_zoom, settings.max_tiles_per_request, color_layer.tile_px)
        if "snow" in requested:
            snow_layer = layer_registry.SNOW_LAYER
            plans["snow"] = plan_tiles(box, 1, 1, snow_layer.max_zoom, settings.max_tiles_per_request, snow_layer.tile_px)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    entries = []
    for day in inclusive_dates(start_day, end_day):
        item = {"date": day}
        errors = {}
        for metric in requested:
            try:
                item[metric] = (await _color_day(client, color_layer, day, plans[metric]) if metric == "color"
                                else await _snow_day(client, day, plans[metric]))
            except Exception as exc:
                errors[metric] = str(exc)
        if errors:
            item["error"] = errors
        entries.append(item)
    payload = {
        "bbox": [box.min_lon, box.min_lat, box.max_lon, box.max_lat],
        "start": start_day.isoformat(), "end": end_day.isoformat(), "metrics": list(requested),
        "observations": entries,
    }
    cache.set(key, payload, ttl)
    return payload


@router.get("/best", summary="Best observation in a recent bounded window")
async def best(
    request: Request, bbox: str = Query(...), metric: str = Query("color", pattern="^(color|snow)$"),
    end: str | None = None, lookback_days: int = Query(7, ge=1), layer: str | None = None,
    client: GibsClient = Depends(deps.get_client),
):
    settings = client.settings
    if lookback_days > settings.max_composite_days:
        raise HTTPException(400, f"lookback_days must be <= {settings.max_composite_days}")
    box = deps.parse_bbox(bbox, settings)
    normal_end = deps.utc_today() - timedelta(days=settings.default_imagery_lag_days)
    end_day = _day(end, normal_end, "end")
    lyr = layer_registry.SNOW_LAYER if metric == "snow" else (layer_registry.get_layer(layer) if layer else layer_registry.DEFAULT_TRUECOLOR)
    if lyr is None or (metric == "color" and lyr.kind != "truecolor"):
        raise HTTPException(400, "layer must be a known true-color layer")
    cache, key, ttl = _cache(request, "best", {"bbox": [box.min_lon, box.min_lat, box.max_lon, box.max_lat], "metric": metric, "end": end_day.isoformat(), "days": lookback_days, "layer": lyr.id}, normal_end, end_day)
    if (hit := cache.get(key)) is not None:
        return hit
    plan = plan_tiles(box, 1, 1, lyr.max_zoom, settings.max_tiles_per_request, lyr.tile_px)
    candidates = []
    errors = []
    for day in inclusive_dates(end_day - timedelta(days=lookback_days - 1), end_day):
        try:
            candidates.append(await (_snow_day(client, day, plan) if metric == "snow" else _color_day(client, lyr, day, plan)))
        except Exception as exc:
            errors.append({"date": day, "error": str(exc)})
    if not candidates:
        raise HTTPException(502, "no candidate observations could be loaded")
    ranked = sorted(candidates, key=snow_rank_key if metric == "snow" else color_rank_key)
    compact = [{"date": x["date"], "observation_quality": x["observation_quality"]} for x in ranked]
    payload = {"metric": metric, "selected": ranked[0], "candidates": compact, "errors": errors}
    cache.set(key, payload, ttl)
    return payload


@router.get("/composite", summary="Median true-color composite over recent days")
async def composite(
    request: Request, bbox: str = Query(...), end: str | None = None,
    days: int = Query(7, ge=1), layer: str | None = None,
    rows: int = Query(16, ge=1, le=256), cols: int = Query(16, ge=1, le=256),
    client: GibsClient = Depends(deps.get_client),
):
    settings = client.settings
    if days > settings.max_composite_days:
        raise HTTPException(400, f"days must be <= {settings.max_composite_days}")
    deps.validate_grid(rows, cols, settings)
    box = deps.parse_bbox(bbox, settings)
    normal_end = deps.utc_today() - timedelta(days=settings.default_imagery_lag_days)
    end_day = _day(end, normal_end, "end")
    start_day = end_day - timedelta(days=days - 1)
    lyr = layer_registry.get_layer(layer) if layer else layer_registry.DEFAULT_TRUECOLOR
    if lyr is None or lyr.kind != "truecolor":
        raise HTTPException(400, "layer must be a known true-color layer")
    cache, key, ttl = _cache(request, "composite", {"bbox": [box.min_lon, box.min_lat, box.max_lon, box.max_lat], "end": end_day.isoformat(), "days": days, "layer": lyr.id, "rows": rows, "cols": cols}, normal_end, end_day)
    if (hit := cache.get(key)) is not None:
        return hit
    plan = plan_tiles(box, rows, cols, lyr.max_zoom, settings.max_tiles_per_request, lyr.tile_px)
    attempted = inclusive_dates(start_day, end_day)
    used, grids, sources, errors = [], [], [], []
    for day in attempted:
        try:
            obs = await _color_day(client, lyr, day, plan, rows, cols, include_matrix=True)
            if obs["observation_quality"]["status"] != "unusable":
                used.append(day); grids.append(obs["matrix"]); sources.append(obs["source"])
        except Exception as exc:
            errors.append({"date": day, "error": str(exc)})
    matrix, counts, rgb = composite_rgb(grids, minimum_observations=settings.min_composite_observations)
    valid = sum(cell is not None for row in matrix for cell in row)
    total = rows * cols
    status = "usable" if valid == total and valid else ("suspect" if valid else "unusable")
    payload = {
        "bbox": [box.min_lon, box.min_lat, box.max_lon, box.max_lat], "start": start_day.isoformat(),
        "end": end_day.isoformat(), "layer": lyr.id, "rows": rows, "cols": cols, "origin": "northwest",
        "cell_size_deg": [box.width / cols, box.height / rows], "matrix": matrix,
        "rgb": rgb, "hex": colors.rgb_to_hex(tuple(rgb)) if rgb else None,
        "observation_counts": counts, "dates_attempted": attempted, "dates_used": used, "errors": errors,
        "source": {"daily_sources": sources, "attribution": "NASA GIBS, NASA/GSFC/ESDIS"},
        "observation_quality": {"status": status, "reasons": [f"{valid} of {total} cells met the minimum of {settings.min_composite_observations} observations."], "valid_cell_fraction": valid / total if total else 0},
    }
    cache.set(key, payload, ttl)
    return payload
