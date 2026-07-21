from typing import Literal

from pydantic import BaseModel, Field


class SourceInfo(BaseModel):
    zoom: int = Field(description="WMTS zoom level actually sampled")
    zoom_degraded: bool = Field(description="True if zoom was lowered to respect the tile budget")
    tiles_fetched: int
    tiles_missing: int = Field(description="Tiles GIBS could not provide (rendered as black fill)")
    native_pixel_deg: float = Field(description="Degrees per source pixel at the sampled zoom")


class ObservationQuality(BaseModel):
    status: Literal["usable", "suspect", "unusable"]
    reasons: list[str]
    missing_tile_fraction: float
    near_black_pixel_fraction: float | None = None
    observable_fraction: float | None = None
    cloud_fraction: float | None = None
    water_fraction: float | None = None


class ColorMatrixResponse(BaseModel):
    bbox: list[float]
    date: str
    date_resolved_from: Literal["previous_completed_day", "latest", "latest_available_fallback", "request"]
    layer: str
    rows: int
    cols: int
    origin: str = "northwest"
    cell_size_deg: list[float] = Field(description="[lon_deg, lat_deg] per cell")
    source: SourceInfo
    observation_quality: ObservationQuality
    matrix: list[list[list[int]]] = Field(description="rows x cols of [r, g, b] 0-255; row 0 is northernmost")


class ColorResponse(BaseModel):
    bbox: list[float]
    date: str
    date_resolved_from: Literal["previous_completed_day", "latest", "latest_available_fallback", "request"]
    layer: str
    rgb: list[int]
    hex: str
    source: SourceInfo
    observation_quality: ObservationQuality


class SnowResponse(BaseModel):
    bbox: list[float]
    date: str
    date_resolved_from: Literal["previous_completed_day", "latest", "latest_available_fallback", "request"]
    layer: str
    snow_fraction: float | None = Field(description="Mean NDSI/100 over observable land pixels; null if none")
    valid_fraction: float = Field(description="Fraction of pixels that were observable land")
    cloud_fraction: float
    water_fraction: float
    matrix: list[list[float | None]] | None = None
    source: SourceInfo
    observation_quality: ObservationQuality


class LayerInfo(BaseModel):
    id: str
    tile_matrix_set: str
    format: str
    max_zoom: int
    kind: str
    latest_available_date: str | None = None


class LayersResponse(BaseModel):
    default_layer: str
    layers: list[LayerInfo]
