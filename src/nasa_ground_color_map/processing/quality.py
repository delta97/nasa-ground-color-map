"""Reproducible, advisory quality signals for sampled observations."""

from dataclasses import dataclass

import numpy as np
from PIL import Image


@dataclass(frozen=True)
class ObservationQuality:
    status: str
    reasons: list[str]
    missing_tile_fraction: float
    near_black_pixel_fraction: float | None = None
    observable_fraction: float | None = None
    cloud_fraction: float | None = None
    water_fraction: float | None = None


def color_quality(image: Image.Image, tiles_missing: int, tiles_fetched: int) -> ObservationQuality:
    """Classify true-color sampling without changing pixels or dates."""
    arr = np.asarray(image.convert("RGB"), dtype=np.uint8)
    near_black = float(np.mean(np.all(arr <= 5, axis=-1))) if arr.size else 1.0
    missing = tiles_missing / tiles_fetched if tiles_fetched else 1.0
    reasons: list[str] = []
    if tiles_missing == tiles_fetched:
        status = "unusable"
        reasons.append("All requested source tiles were unavailable and were rendered as black fill.")
    elif near_black >= 0.99:
        status = "unusable"
        reasons.append(f"{near_black:.1%} of sampled pixels are near-black, suggesting a swath gap or night imagery.")
    elif tiles_missing or near_black >= 0.05:
        status = "suspect"
        if tiles_missing:
            reasons.append(f"{missing:.1%} of requested source tiles were unavailable and rendered as black fill.")
        if near_black >= 0.05:
            reasons.append(f"{near_black:.1%} of sampled pixels are near-black.")
    else:
        status = "usable"
        reasons.append("Source tile coverage and near-black-pixel checks are within advisory thresholds.")
    return ObservationQuality(status, reasons, missing, near_black_pixel_fraction=near_black)


def snow_quality(
    *, observable_fraction: float, cloud_fraction: float, water_fraction: float,
    tiles_missing: int, tiles_fetched: int,
) -> ObservationQuality:
    missing = tiles_missing / tiles_fetched if tiles_fetched else 1.0
    if observable_fraction == 0:
        status = "unusable"
        reasons = ["No observable land pixels were available for the snow estimate."]
    elif observable_fraction < 0.30:
        status = "suspect"
        reasons = [f"Only {observable_fraction:.1%} of pixels were observable land; below the 30% advisory threshold."]
    else:
        status = "usable"
        reasons = [f"{observable_fraction:.1%} of pixels were observable land, meeting the 30% advisory threshold."]
    if tiles_missing:
        reasons.append(f"{missing:.1%} of requested source tiles were unavailable.")
    if cloud_fraction:
        reasons.append(f"Cloud covers {cloud_fraction:.1%} of sampled pixels.")
    if water_fraction:
        reasons.append(f"Water covers {water_fraction:.1%} of sampled pixels.")
    return ObservationQuality(
        status, reasons, missing, observable_fraction=observable_fraction,
        cloud_fraction=cloud_fraction, water_fraction=water_fraction,
    )
