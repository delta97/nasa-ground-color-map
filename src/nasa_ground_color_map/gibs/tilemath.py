"""Pure tile-grid math for the GIBS EPSG:4326 WMTS tile matrix sets.

Grid facts taken from the live GetCapabilities TileMatrixSet definitions
(250m/500m sets): every level is anchored at TopLeftCorner (-180, 90) with
512px square tiles, and the tile span at level z is 288 / 2^z degrees
(z=8 -> 1.125 deg, i.e. ~244m/px, the "250m" resolution). The matrix is
ceil(360/span) columns by ceil(180/span) rows — e.g. 2x1 at z=0, 10x5 at
z=3, 320x160 at z=8 — so edge tiles at coarse zooms extend past the
south/east edges of the world and are padded with fill.
"""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class BBox:
    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float

    @property
    def width(self) -> float:
        return self.max_lon - self.min_lon

    @property
    def height(self) -> float:
        return self.max_lat - self.min_lat


@dataclass(frozen=True)
class TilePlan:
    zoom: int
    col_min: int
    col_max: int
    row_min: int
    row_max: int
    tile_px: int
    # bbox crop rectangle in mosaic pixel coordinates (mosaic = stitched tile range)
    crop_left: int
    crop_top: int
    crop_right: int
    crop_bottom: int
    degraded: bool  # True if zoom was lowered to respect the tile budget

    @property
    def n_cols(self) -> int:
        return self.col_max - self.col_min + 1

    @property
    def n_rows(self) -> int:
        return self.row_max - self.row_min + 1

    @property
    def tile_count(self) -> int:
        return self.n_cols * self.n_rows

    def tiles(self):
        for row in range(self.row_min, self.row_max + 1):
            for col in range(self.col_min, self.col_max + 1):
                yield row, col


BASE_SPAN_DEG = 288.0  # level-0 tile span of the GIBS EPSG:4326 tile matrix sets


def tile_span_deg(zoom: int) -> float:
    return BASE_SPAN_DEG / (1 << zoom)


def matrix_size(zoom: int) -> tuple[int, int]:
    """(columns, rows) of the tile matrix at this zoom."""
    span = tile_span_deg(zoom)
    return math.ceil(360.0 / span), math.ceil(180.0 / span)


def pixel_deg(zoom: int, tile_px: int = 512) -> float:
    return tile_span_deg(zoom) / tile_px


def lonlat_to_tile(lon: float, lat: float, zoom: int) -> tuple[int, int]:
    """Tile (col, row) containing the point; points on the far edges clamp inward."""
    span = tile_span_deg(zoom)
    n_cols, n_rows = matrix_size(zoom)
    col = min(int((lon + 180.0) / span), n_cols - 1)
    row = min(int((90.0 - lat) / span), n_rows - 1)
    return col, row


def select_zoom(
    bbox: BBox,
    rows: int,
    cols: int,
    max_zoom: int,
    tile_px: int = 512,
    min_axis_px: int = 64,
) -> int:
    """Smallest zoom that satisfies both:
    - pixels <= half the requested output cell size (each cell averages >= 4
      native pixels), and
    - the bbox's smaller axis spans >= min_axis_px pixels (quality floor so a
      small bbox with a coarse grid still samples meaningful imagery).
    Clamped to [0, max_zoom]."""
    cell_deg = min(bbox.width / cols, bbox.height / rows)
    target = min(cell_deg / 2.0, min(bbox.width, bbox.height) / min_axis_px)
    for zoom in range(0, max_zoom + 1):
        if pixel_deg(zoom, tile_px) <= target:
            return zoom
    return max_zoom


def _tile_range(bbox: BBox, zoom: int) -> tuple[int, int, int, int]:
    col_min, row_min = lonlat_to_tile(bbox.min_lon, bbox.max_lat, zoom)
    col_max, row_max = lonlat_to_tile(bbox.max_lon, bbox.min_lat, zoom)
    # A bbox edge exactly on a tile boundary should not drag in the next tile.
    span = tile_span_deg(zoom)
    if col_max > col_min and math.isclose((bbox.max_lon + 180.0) / span, col_max, abs_tol=1e-9):
        col_max -= 1
    if row_max > row_min and math.isclose((90.0 - bbox.min_lat) / span, row_max, abs_tol=1e-9):
        row_max -= 1
    return col_min, col_max, row_min, row_max


def plan_tiles(
    bbox: BBox,
    rows: int,
    cols: int,
    max_zoom: int,
    max_tiles: int,
    tile_px: int = 512,
) -> TilePlan:
    """Choose a zoom and tile range covering the bbox within the tile budget.

    If the ideal zoom needs more than max_tiles tiles, the zoom is lowered
    (coarser imagery) until the request fits — reported via `degraded`.
    """
    zoom = select_zoom(bbox, rows, cols, max_zoom, tile_px)
    degraded = False
    while True:
        col_min, col_max, row_min, row_max = _tile_range(bbox, zoom)
        count = (col_max - col_min + 1) * (row_max - row_min + 1)
        if count <= max_tiles or zoom == 0:
            break
        zoom -= 1
        degraded = True
    if count > max_tiles:
        raise ValueError(f"bbox needs {count} tiles even at zoom 0 (budget {max_tiles})")

    px = pixel_deg(zoom, tile_px)
    span = tile_span_deg(zoom)
    mosaic_west = col_min * span - 180.0
    mosaic_north = 90.0 - row_min * span
    left = math.floor((bbox.min_lon - mosaic_west) / px)
    top = math.floor((mosaic_north - bbox.max_lat) / px)
    right = math.ceil((bbox.max_lon - mosaic_west) / px)
    bottom = math.ceil((mosaic_north - bbox.min_lat) / px)
    # Guarantee a non-empty crop even for degenerate/thin boxes.
    right = max(right, left + 1)
    bottom = max(bottom, top + 1)
    mosaic_w = (col_max - col_min + 1) * tile_px
    mosaic_h = (row_max - row_min + 1) * tile_px
    right = min(right, mosaic_w)
    bottom = min(bottom, mosaic_h)

    return TilePlan(
        zoom=zoom,
        col_min=col_min,
        col_max=col_max,
        row_min=row_min,
        row_max=row_max,
        tile_px=tile_px,
        crop_left=left,
        crop_top=top,
        crop_right=right,
        crop_bottom=bottom,
        degraded=degraded,
    )
