"""GeoJSON/KML/GPX ingestion and geometry-aware sampling."""

from __future__ import annotations

from dataclasses import asdict
import json

from defusedxml import ElementTree
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from shapely.geometry import LineString, MultiLineString, mapping, shape
from shapely.ops import unary_union

from ..gibs import layers as layer_registry
from ..gibs.client import GibsClient
from ..gibs.tilemath import BBox, plan_tiles
from ..processing import colors, geometry as geometry_ops, mosaic, quality
from . import deps

router = APIRouter(prefix="/v1/areas", tags=["areas"])
MAX_UPLOAD = 5 * 1024 * 1024


def _normalized(value: dict):
    try:
        geometry, wraps = geometry_ops.normalize_geometry(value)
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(400, str(exc))
    geom = shape(geometry)
    return {
        "type": "Feature", "properties": {}, "geometry": geometry,
        "bbox": list(geom.bounds), "wraps_antimeridian": wraps,
    }


@router.post("/normalize", summary="Validate and normalize polygonal GeoJSON")
async def normalize(body: dict):
    return _normalized(body)


def _coords(text: str):
    result = []
    for token in text.replace("\n", " ").split():
        pieces = token.split(",")
        if len(pieces) >= 2:
            result.append((float(pieces[0]), float(pieces[1])))
    return result


def _parse_kml(data: bytes, corridor_km: float):
    root = ElementTree.fromstring(data)
    polygons, lines = [], []
    for node in root.iter():
        name = node.tag.rsplit("}", 1)[-1]
        if name == "Polygon":
            rings = [_coords(n.text or "") for n in node.iter() if n.tag.rsplit("}", 1)[-1] == "coordinates"]
            if rings and len(rings[0]) >= 4:
                from shapely.geometry import Polygon
                polygons.append(Polygon(rings[0], rings[1:]))
        elif name in {"LineString", "Track"}:
            points = []
            for n in node.iter():
                child_name = n.tag.rsplit("}", 1)[-1]
                if child_name == "coordinates": points.extend(_coords(n.text or ""))
                elif child_name == "coord" and n.text:
                    p = n.text.split(); points.append((float(p[0]), float(p[1])))
            if len(points) >= 2: lines.append(LineString(points))
    areas = list(polygons)
    if lines:
        areas.append(shape(geometry_ops.corridor_geometry(MultiLineString(lines), corridor_km)))
    if not areas:
        raise ValueError("KML did not contain a polygon, line, or track")
    return mapping(unary_union(areas))


def _parse_gpx(data: bytes, corridor_km: float):
    root = ElementTree.fromstring(data)
    lines = []
    for parent in root.iter():
        if parent.tag.rsplit("}", 1)[-1] not in {"trkseg", "rte"}: continue
        points = []
        for node in parent:
            if node.tag.rsplit("}", 1)[-1] in {"trkpt", "rtept"}:
                points.append((float(node.attrib["lon"]), float(node.attrib["lat"])))
        if len(points) >= 2: lines.append(LineString(points))
    if not lines:
        raise ValueError("GPX did not contain a route or track")
    return geometry_ops.corridor_geometry(MultiLineString(lines), corridor_km)


@router.post("/upload", summary="Normalize an uploaded GeoJSON, KML, or GPX area")
async def upload(file: UploadFile = File(...), corridor_km: float = Form(2.0)):
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "file exceeds the 5 MB limit")
    name = (file.filename or "").lower()
    try:
        if name.endswith((".geojson", ".json")):
            geometry = json.loads(data)
        elif name.endswith(".kml"):
            geometry = _parse_kml(data, corridor_km)
        elif name.endswith(".gpx"):
            geometry = _parse_gpx(data, corridor_km)
        else:
            raise ValueError("file must be GeoJSON, KML, or GPX")
    except (ValueError, TypeError, ElementTree.ParseError, json.JSONDecodeError) as exc:
        raise HTTPException(400, str(exc))
    return _normalized(geometry)


async def sample_geometry_payload(body: dict, client: GibsClient, latest_dates):
    feature = _normalized(body.get("geometry") or body.get("area") or {})
    geometry = feature["geometry"]
    geom = shape(geometry)
    minx, miny, maxx, maxy = geom.bounds
    if feature["wraps_antimeridian"]:
        parts = list(geom.geoms)
        total_width = sum(part.bounds[2] - part.bounds[0] for part in parts)
        requested_cols = int(body.get("cols", 16))
        allocations = [max(1, round(requested_cols * (part.bounds[2] - part.bounds[0]) / total_width)) for part in parts]
        allocations[-1] += requested_cols - sum(allocations)
        if allocations[-1] < 1:
            allocations[-2] += allocations[-1] - 1; allocations[-1] = 1
        samples = []
        for part, part_cols in zip(parts, allocations):
            nested = dict(body); nested["geometry"] = mapping(part); nested["cols"] = part_cols
            samples.append(await sample_geometry_payload(nested, client, latest_dates))
        merged = dict(samples[0])
        merged["matrix"] = [sum((sample["matrix"][r] for sample in samples), []) for r in range(len(samples[0]["matrix"]))]
        if all("observation_counts" in sample for sample in samples):
            merged["observation_counts"] = [sum((sample["observation_counts"][r] for sample in samples), []) for r in range(len(samples[0]["matrix"]))]
        merged["rgb"] = geometry_ops.aggregate_rgb(merged["matrix"])
        merged["geometry"] = geometry; merged["cols"] = requested_cols; merged["wraps_antimeridian"] = True
        merged["sampling_bboxes"] = [sample["bbox"] for sample in samples]
        merged["bbox"] = list(geom.bounds)
        return merged
    settings = client.settings
    box = BBox(minx, miny, maxx, maxy)
    rows, cols = int(body.get("rows", 16)), int(body.get("cols", 16))
    deps.validate_grid(rows, cols, settings)
    observation = body.get("observation", {})
    mode = observation.get("mode", "single")
    layer = layer_registry.get_layer(observation.get("layer")) if observation.get("layer") else layer_registry.DEFAULT_TRUECOLOR
    if layer is None or layer.kind != "truecolor": raise HTTPException(400, "unknown true-color layer")
    if mode != "single":
        # Reuse the public bounded temporal implementations to keep ranking and
        # compositing behavior identical across bbox and geometry APIs.
        from .routes_temporal import best, composite
        bbox_raw = f"{minx},{miny},{maxx},{maxy}"
        if mode == "composite":
            sampled = await composite(body["request"], bbox_raw, observation.get("end"), int(observation.get("days", 7)), layer.id, rows, cols, client)
        elif mode == "best":
            selected = await best(body["request"], bbox_raw, "color", observation.get("end"), int(observation.get("lookback_days", 7)), layer.id, client)
            observation = {"mode": "single", "date": selected["selected"]["date"], "layer": layer.id}
            mode = "single"
        else: raise HTTPException(400, "observation mode must be single, best, or composite")
    if mode == "single":
        concrete, resolved = deps.resolve_date(observation.get("date"), layer.id, latest_dates, settings)
        plan = plan_tiles(box, rows, cols, layer.max_zoom, settings.max_tiles_per_request, layer.tile_px)
        tiles = await client.fetch_plan(layer, concrete, plan)
        cropped, missing = mosaic.stitch_and_crop(tiles, plan, mode="RGB")
        sampled = {"date": concrete, "date_resolved_from": resolved, "layer": layer.id,
                   "matrix": colors.to_grid(cropped, rows, cols),
                   "observation_quality": asdict(quality.color_quality(cropped, missing, plan.tile_count))}
    sampled["matrix"] = geometry_ops.mask_grid(sampled["matrix"], geometry, [minx, miny, maxx, maxy])
    sampled["rgb"] = geometry_ops.aggregate_rgb(sampled["matrix"])
    sampled.update(geometry=geometry, bbox=[minx, miny, maxx, maxy], rows=rows, cols=cols,
                   wraps_antimeridian=feature["wraps_antimeridian"])
    return sampled


@router.post("/sample", summary="Sample an observation and mask cells outside a geometry")
async def sample_area(request: Request, body: dict, client: GibsClient = Depends(deps.get_client), latest_dates=Depends(deps.get_latest_dates)):
    body = dict(body); body["request"] = request
    return await sample_geometry_payload(body, client, latest_dates)
