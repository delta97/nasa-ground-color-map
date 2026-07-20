"""Decode MODIS NDSI_Snow_Cover paletted tiles into snow-cover statistics.

GIBS serves the snow layer as a paletted (P-mode) PNG whose palette indices
follow the layer's GIBS colormap (MODIS_NDSI_Snow_Cover.xml), verified
against a live tile:

    0-100   NDSI snow cover percent (index == NDSI value)
    101     missing data         102  no decision
    103     night                104  inland water
    105     ocean                106  cloud
    107     detector saturated   108  fill

Indices above 108 do not occur in real tiles; anything not listed is treated
as unobserved. "Valid" below means observable land: indices 0-100.
"""

from dataclasses import dataclass

import numpy as np
from PIL import Image

WATER_INDICES = (104, 105)  # inland water, ocean
CLOUD_INDEX = 106


@dataclass(frozen=True)
class SnowStats:
    snow_fraction: float | None  # mean NDSI/100 over valid land pixels; None if no valid pixels
    valid_fraction: float
    cloud_fraction: float
    water_fraction: float


def _stats(values: np.ndarray) -> SnowStats:
    total = values.size
    if total == 0:
        return SnowStats(None, 0.0, 0.0, 0.0)
    valid_mask = values <= 100
    cloud = float(np.count_nonzero(values == CLOUD_INDEX)) / total
    water = float(np.count_nonzero(np.isin(values, WATER_INDICES))) / total
    n_valid = int(np.count_nonzero(valid_mask))
    snow = float(values[valid_mask].mean() / 100.0) if n_valid else None
    return SnowStats(snow, n_valid / total, cloud, water)


def analyze(image: Image.Image) -> SnowStats:
    if image.mode != "P":
        raise ValueError(f"expected P-mode snow image, got {image.mode}")
    return _stats(np.asarray(image))


def analyze_grid(image: Image.Image, rows: int, cols: int) -> list[list[float | None]]:
    """Per-cell snow fraction over a rows x cols grid (None where no valid pixels)."""
    if image.mode != "P":
        raise ValueError(f"expected P-mode snow image, got {image.mode}")
    arr = np.asarray(image)
    h, w = arr.shape
    row_edges = np.linspace(0, h, rows + 1).round().astype(int)
    col_edges = np.linspace(0, w, cols + 1).round().astype(int)
    grid: list[list[float | None]] = []
    for r in range(rows):
        line: list[float | None] = []
        for c in range(cols):
            block = arr[row_edges[r]:row_edges[r + 1], col_edges[c]:col_edges[c + 1]]
            stats = _stats(block.ravel())
            line.append(round(stats.snow_fraction, 4) if stats.snow_fraction is not None else None)
        grid.append(line)
    return grid
