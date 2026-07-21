"""Pure temporal ranking and compositing helpers."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

import numpy as np


QUALITY_RANK = {"usable": 0, "suspect": 1, "unusable": 2}


def inclusive_dates(start: date, end: date) -> list[str]:
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


def color_rank_key(item: dict) -> tuple:
    q = item["observation_quality"]
    return (
        QUALITY_RANK.get(q["status"], 3),
        q.get("missing_tile_fraction", 1.0),
        q.get("near_black_pixel_fraction", 1.0),
        -date.fromisoformat(item["date"]).toordinal(),
    )


def snow_rank_key(item: dict) -> tuple:
    q = item["observation_quality"]
    return (
        QUALITY_RANK.get(q["status"], 3),
        -(q.get("observable_fraction") or 0.0),
        q.get("cloud_fraction") if q.get("cloud_fraction") is not None else 1.0,
        -date.fromisoformat(item["date"]).toordinal(),
    )


def composite_rgb(
    daily_grids: Iterable[list[list[list[int]]]],
    *,
    minimum_observations: int = 2,
    near_black_max: int = 5,
) -> tuple[list[list[list[int] | None]], list[list[int]], list[int] | None]:
    """Channel-wise median, excluding near-black cells.

    Returns (matrix, observation_counts, aggregate_rgb). For an even number
    of inputs NumPy's midpoint median is rounded to the nearest integer.
    """
    arrays = [np.asarray(grid, dtype=np.uint8) for grid in daily_grids]
    if not arrays:
        return [], [], None
    stack = np.stack(arrays).astype(float)
    invalid = np.all(stack <= near_black_max, axis=-1)
    stack[invalid] = np.nan
    counts = np.sum(~invalid, axis=0)
    with np.errstate(all="ignore"):
        med = np.nanmedian(stack, axis=0)
    valid = counts >= minimum_observations
    matrix: list[list[list[int] | None]] = []
    for r in range(med.shape[0]):
        row = []
        for c in range(med.shape[1]):
            row.append(np.rint(med[r, c]).astype(int).tolist() if valid[r, c] else None)
        matrix.append(row)
    if np.any(valid):
        aggregate = np.rint(med[valid].mean(axis=0)).astype(int).tolist()
    else:
        aggregate = None
    return matrix, counts.astype(int).tolist(), aggregate
