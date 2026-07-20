"""Shared request parsing/validation and app-state accessors."""

from datetime import datetime

from fastapi import HTTPException, Request

from ..config import Settings
from ..gibs.client import GibsClient
from ..gibs.tilemath import BBox, parse_bbox_string


def get_client(request: Request) -> GibsClient:
    return request.app.state.gibs_client


def get_latest_dates(request: Request):
    return request.app.state.latest_dates


def parse_bbox(raw: str, settings: Settings) -> BBox:
    try:
        return parse_bbox_string(raw, settings.max_bbox_deg)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


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
