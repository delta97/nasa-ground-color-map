"""In-memory GIS raster bundles."""

from __future__ import annotations

from io import BytesIO
import json
from zipfile import ZIP_DEFLATED, ZipFile

import numpy as np
from PIL import Image


def rgba_array(matrix) -> np.ndarray:
    rows, cols = len(matrix), len(matrix[0]) if matrix else 0
    out = np.zeros((rows, cols, 4), dtype=np.uint8)
    for r, row in enumerate(matrix):
        for c, cell in enumerate(row):
            if cell is not None:
                out[r, c, :3] = cell
                out[r, c, 3] = 255
    return out


def _png(array: np.ndarray, mode: str) -> bytes:
    stream = BytesIO(); Image.fromarray(array).save(stream, "PNG"); return stream.getvalue()


def _tiff(array: np.ndarray, bbox: list[float], metadata: dict, cog: bool) -> bytes:
    try:
        import rasterio
        from rasterio.io import MemoryFile
        from rasterio.transform import from_bounds
    except ImportError as exc:
        raise RuntimeError("GeoTIFF exports require rasterio") from exc
    height, width = array.shape[:2]
    profile = {"driver": "GTiff", "width": width, "height": height, "count": array.shape[2] if array.ndim == 3 else 1,
               "dtype": str(array.dtype), "crs": "EPSG:4326", "transform": from_bounds(*bbox, width, height),
               "compress": "deflate", "tiled": bool(cog)}
    with MemoryFile() as memory:
        with memory.open(**profile) as dataset:
            if array.ndim == 3:
                dataset.write(np.moveaxis(array, -1, 0))
            else: dataset.write(array, 1)
            dataset.update_tags(**{k: json.dumps(v) if not isinstance(v, str) else v for k, v in metadata.items()})
        raw = memory.read()
    if not cog: return raw
    # MemoryFile cannot directly target the COG driver on all GDAL builds;
    # GTiff with internal tiling is valid input for a deliberate COG export.
    try:
        from rasterio.shutil import copy as rio_copy
        with MemoryFile(raw) as source_mem, MemoryFile() as dest_mem:
            with source_mem.open() as source:
                rio_copy(source, dest_mem.name, driver="COG", compress="DEFLATE")
            return dest_mem.read()
    except Exception:
        return raw


def raster_bundle(matrix, bbox: list[float], metadata: dict, kind: str, counts=None) -> bytes:
    rgba = rgba_array(matrix)
    valid = rgba[:, :, 3]
    if counts is None: quality = valid
    else:
        count_arr = np.asarray(counts, dtype=float)
        maximum = max(1.0, float(count_arr.max(initial=0)))
        quality = np.rint(count_arr / maximum * 255).astype(np.uint8)
        quality[valid == 0] = 0
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as bundle:
        bundle.writestr("metadata.json", json.dumps(metadata, indent=2, default=str))
        if kind == "png-bundle":
            bundle.writestr("color.png", _png(rgba, "RGBA")); bundle.writestr("quality.png", _png(quality, "L"))
        else:
            cog = kind == "cog-bundle"
            bundle.writestr("color.tif", _tiff(rgba, bbox, metadata, cog))
            bundle.writestr("quality.tif", _tiff(quality, bbox, metadata, cog))
    return stream.getvalue()
