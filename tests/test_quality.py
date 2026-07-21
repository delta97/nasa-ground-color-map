import numpy as np
import pytest
from PIL import Image

from nasa_ground_color_map.processing.quality import color_quality, snow_quality


def image_with_black_fraction(fraction: float) -> Image.Image:
    arr = np.full((100, 100, 3), 100, dtype=np.uint8)
    arr.reshape(-1, 3)[: round(arr.shape[0] * arr.shape[1] * fraction)] = 0
    return Image.fromarray(arr, "RGB")


@pytest.mark.parametrize(("black_fraction", "status"), [(0.0, "usable"), (0.05, "suspect"), (0.99, "unusable")])
def test_color_quality_near_black_thresholds(black_fraction, status):
    assert color_quality(image_with_black_fraction(black_fraction), 0, 4).status == status


def test_color_quality_partial_missing_is_suspect():
    quality = color_quality(image_with_black_fraction(0), 1, 4)
    assert quality.status == "suspect"
    assert quality.missing_tile_fraction == 0.25


@pytest.mark.parametrize(("observable", "status"), [(0.30, "usable"), (0.01, "suspect"), (0.0, "unusable")])
def test_snow_quality_thresholds(observable, status):
    assert snow_quality(
        observable_fraction=observable, cloud_fraction=0, water_fraction=0,
        tiles_missing=0, tiles_fetched=1,
    ).status == status
