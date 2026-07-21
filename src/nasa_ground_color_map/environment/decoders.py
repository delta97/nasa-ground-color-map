"""Decode RGB visualizations with deliberately pinned local tables."""

from __future__ import annotations

import json
from importlib.resources import files

import numpy as np
from PIL import Image


def load_tables() -> dict:
    return json.loads(files("nasa_ground_color_map.environment").joinpath("pinned_colormaps.json").read_text())


def decode_raster(image: Image.Image, slug: str):
    """Return numeric/category values, using nearest pinned RGB entry.

    Nearest-color matching tolerates small RGB changes introduced by rendered
    tiles while the value table itself remains immutable until explicitly
    refreshed and reviewed.
    """
    spec = load_tables()[slug]
    arr = np.asarray(image.convert("RGB"), dtype=np.int32)
    colors = np.asarray([entry["rgb"] for entry in spec["entries"]], dtype=np.int32)
    flat = arr.reshape(-1, 3)
    distance = np.sum((flat[:, None, :] - colors[None, :, :]) ** 2, axis=2)
    chosen = np.argmin(distance, axis=1)
    values = np.asarray([entry.get("value", np.nan) for entry in spec["entries"]], dtype=float)[chosen]
    transparent = np.asarray([entry.get("nodata", False) for entry in spec["entries"]])[chosen]
    values[transparent] = np.nan
    return values.reshape(arr.shape[:2]), chosen.reshape(arr.shape[:2]), spec


def numeric_summary(values: np.ndarray, rows: int, cols: int, matrix_name: str = "matrix") -> dict:
    from PIL import Image
    valid = np.isfinite(values)
    result = {"mean": None, "min": None, "max": None, "valid_fraction": float(valid.mean())}
    if np.any(valid):
        result.update(mean=float(np.nanmean(values)), min=float(np.nanmin(values)), max=float(np.nanmax(values)))
    # Resize value and validity separately so invalid source pixels do not turn
    # into apparently valid values during area averaging.
    filled = np.nan_to_num(values, nan=0).astype(np.float32)
    sums = np.asarray(Image.fromarray(filled).resize((cols, rows), Image.Resampling.BOX))
    weights = np.asarray(Image.fromarray(valid.astype(np.float32)).resize((cols, rows), Image.Resampling.BOX))
    grid = np.divide(sums, weights, out=np.full_like(sums, np.nan), where=weights > 0)
    result[matrix_name] = [[round(float(v), 6) if np.isfinite(v) else None for v in row] for row in grid]
    return result


def decode_thermal_tiles(tiles: dict, plan, bbox: list[float]) -> list[dict]:
    """Decode GIBS Mapbox vector tiles into clipped EPSG:4326 features."""
    import mapbox_vector_tile
    from shapely.geometry import Point, box as shape_box, shape as shape_geo
    from ..gibs.tilemath import tile_span_deg

    clip = shape_box(*bbox); span = tile_span_deg(plan.zoom); features = []
    for (row, col), data in tiles.items():
        if not data: continue
        west, north = -180 + col * span, 90 - row * span
        def project(x, y): return west + x / 4096 * span, north - y / 4096 * span
        decoded = mapbox_vector_tile.decode(data, default_options={"y_coord_down": True, "transformer": project})
        for layer in decoded.values():
            for feature in layer.get("features", []):
                geometry = feature.get("geometry")
                if geometry and shape_geo(geometry).intersects(clip):
                    features.append({"type": "Feature", "geometry": geometry, "properties": feature.get("properties", {})})
    return features


def summarize_thermal_features(features: list[dict]) -> dict:
    frp = []
    confidence: dict[str, int] = {}
    for feature in features:
        props = feature.get("properties", {})
        try: frp.append(float(props.get("FRP")))
        except (TypeError, ValueError): pass
        level = str(props.get("CONFIDENCE", "unknown")).lower()
        level = {"l": "low", "n": "nominal", "h": "high"}.get(level, level)
        confidence[level] = confidence.get(level, 0) + 1
    return {"detection_count": len(features), "confidence_counts": confidence,
            "maximum_fire_radiative_power": max(frp) if frp else None,
            "total_fire_radiative_power": sum(frp), "features": features}
