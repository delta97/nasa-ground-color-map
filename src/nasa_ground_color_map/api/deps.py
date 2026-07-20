"""Shared request parsing/validation and app-state accessors."""

from datetime import datetime

from fastapi import HTTPException, Request

from ..config import Settings
from ..gibs.client import GibsClient
from ..gibs.tilemath import BBox


def get_client(request: Request) -> GibsClient:
    return request.app.state.gibs_client


def get_latest_dates(request: Request):
    return request.app.state.latest_dates


def parse_bbox(raw: str, settings: Settings) -> BBox:
    parts = raw.split(",")
    if len(parts) != 4:
        raise HTTPException(400, "bbox must be 'minLon,minLat,maxLon,maxLat'")
    try:
        min_lon, min_lat, max_lon, max_lat = (float(p) for p in parts)
    except ValueError:
        raise HTTPException(400, "bbox values must be numbers")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise HTTPException(400, "longitudes must be in [-180, 180]")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise HTTPException(400, "latitudes must be in [-90, 90]")
    if min_lon >= max_lon:
        raise HTTPException(
            400,
            "minLon must be < maxLon (antimeridian-crossing boxes are not supported; "
            "split the request at 180°)",
        )
    if min_lat >= max_lat:
        raise HTTPException(400, "minLat must be < maxLat")
    if settings.max_bbox_deg > 0:
        if max_lon - min_lon > settings.max_bbox_deg or max_lat - min_lat > settings.max_bbox_deg:
            raise HTTPException(
                400, f"bbox span must be <= {settings.max_bbox_deg} degrees per axis"
            )
    return BBox(min_lon, min_lat, max_lon, max_lat)


def resolve_date(date: str | None, layer_id: str, latest_dates) -> tuple[str, str]:
    """Return (concrete YYYY-MM-DD, resolved_from)."""
    if date is None:
        return latest_dates.latest_for(layer_id), "latest"
    try:
        parsed = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD")
    return parsed.date().isoformat(), "request"


def validate_grid(rows: int, cols: int, settings: Settings) -> None:
    if rows * cols > settings.max_grid_cells:
        raise HTTPException(400, f"rows*cols must be <= {settings.max_grid_cells}")
