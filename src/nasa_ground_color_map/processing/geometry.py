"""Geometry normalization, corridor creation, and grid masking."""

from __future__ import annotations

from collections.abc import Callable

from shapely import affinity
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Point, Polygon, mapping, shape
from shapely.ops import split, transform
from shapely.validation import make_valid


def _geometry_object(value: dict) -> dict:
    if value.get("type") == "Feature":
        value = value.get("geometry") or {}
    if value.get("type") == "FeatureCollection":
        from shapely.ops import unary_union
        return mapping(unary_union([shape(f["geometry"]) for f in value.get("features", [])]))
    return value


def _walk_coordinates(coords, visit: Callable[[float, float], tuple[float, float]]):
    if coords and isinstance(coords[0], (int, float)):
        x, y = visit(float(coords[0]), float(coords[1]))
        return [x, y, *coords[2:]]
    return [_walk_coordinates(item, visit) for item in coords]


def normalize_geometry(value: dict) -> tuple[dict, bool]:
    raw = _geometry_object(value)
    if raw.get("type") not in {"Polygon", "MultiPolygon"}:
        raise ValueError("geometry must be a GeoJSON Polygon or MultiPolygon")
    def validate_position(x, y):
        if not -180 <= x <= 180: raise ValueError("geometry longitude must be within [-180, 180]")
        if not -90 <= y <= 90: raise ValueError("geometry latitude must be within [-90, 90]")
        return x, y
    _walk_coordinates(raw.get("coordinates", []), validate_position)
    geom = make_valid(shape(raw))
    if geom.is_empty or geom.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError("geometry must contain a non-empty polygonal area")
    minx, miny, maxx, maxy = geom.bounds
    if miny < -90 or maxy > 90:
        raise ValueError("geometry latitude must be within [-90, 90]")
    wraps = maxx - minx > 180
    if wraps:
        # Interpret a >180 degree ring as taking the short route across ±180.
        shifted = transform(lambda x, y, z=None: (x + 360 if x < 0 else x, y), geom)
        cutter = LineString([(180, -90), (180, 90)])
        parts = split(shifted, cutter).geoms
        normalized = []
        for part in parts:
            if part.bounds[0] >= 180:
                part = affinity.translate(part, xoff=-360)
            normalized.append(part)
        geom = MultiPolygon([p for p in normalized if isinstance(p, Polygon)])
    return mapping(geom), wraps


def corridor_geometry(lines, corridor_km: float) -> dict:
    if not 0.1 <= corridor_km <= 100:
        raise ValueError("corridor_km must be in [0.1, 100]")
    geom = lines if hasattr(lines, "geom_type") else shape(lines)
    if geom.geom_type not in {"LineString", "MultiLineString"}:
        raise ValueError("corridors require line or track geometry")
    center = geom.centroid
    try:
        from pyproj import CRS, Transformer
        local = CRS.from_proj4(f"+proj=aeqd +lat_0={center.y} +lon_0={center.x} +datum=WGS84 +units=m")
        forward = Transformer.from_crs("EPSG:4326", local, always_xy=True).transform
        reverse = Transformer.from_crs(local, "EPSG:4326", always_xy=True).transform
        polygon = transform(reverse, transform(forward, geom).buffer(corridor_km * 500))
    except ImportError:  # Useful in minimal source checkouts; packaged installs include pyproj.
        polygon = geom.buffer(corridor_km / 222.0)
    return mapping(polygon)


def mask_grid(matrix, geometry: dict, bbox: list[float]):
    geom = shape(geometry)
    rows, cols = len(matrix), len(matrix[0]) if matrix else 0
    minx, miny, maxx, maxy = bbox
    result = []
    for r, row in enumerate(matrix):
        lat = maxy - (r + 0.5) * (maxy - miny) / rows
        out = []
        for c, value in enumerate(row):
            lon = minx + (c + 0.5) * (maxx - minx) / cols
            out.append(value if geom.covers(Point(lon, lat)) else None)
        result.append(out)
    return result


def aggregate_rgb(matrix) -> list[int] | None:
    import numpy as np
    values = [cell for row in matrix for cell in row if cell is not None]
    return np.rint(np.asarray(values).mean(axis=0)).astype(int).tolist() if values else None
