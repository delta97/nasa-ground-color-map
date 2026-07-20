import numpy as np
import pytest
from PIL import Image

from nasa_ground_color_map.processing import snow


def make_snow_image(values: np.ndarray) -> Image.Image:
    im = Image.fromarray(values.astype(np.uint8), mode="P")
    palette = []
    for i in range(256):
        palette.extend([i, i, i])
    im.putpalette(palette)
    return im


def test_all_snow():
    im = make_snow_image(np.full((10, 10), 100))
    stats = snow.analyze(im)
    assert stats.snow_fraction == pytest.approx(1.0)
    assert stats.valid_fraction == 1.0
    assert stats.cloud_fraction == 0.0


def test_mixed_values():
    values = np.zeros((2, 2))
    values[0, 0] = 80   # 80% NDSI
    values[0, 1] = 40   # 40% NDSI
    values[1, 0] = 106  # cloud (GIBS palette index)
    values[1, 1] = 104  # inland water
    stats = snow.analyze(make_snow_image(values))
    assert stats.snow_fraction == pytest.approx(0.6)  # mean of 0.8, 0.4
    assert stats.valid_fraction == 0.5
    assert stats.cloud_fraction == 0.25
    assert stats.water_fraction == 0.25


def test_no_valid_pixels():
    im = make_snow_image(np.full((4, 4), 106))
    stats = snow.analyze(im)
    assert stats.snow_fraction is None
    assert stats.cloud_fraction == 1.0


def test_fill_and_night_are_invalid_but_not_cloud_or_water():
    values = np.array([[101, 103], [108, 255]])  # missing, night, fill, mosaic fill
    stats = snow.analyze(make_snow_image(values))
    assert stats.snow_fraction is None
    assert stats.valid_fraction == 0.0
    assert stats.cloud_fraction == 0.0
    assert stats.water_fraction == 0.0


def test_grid():
    values = np.zeros((4, 4))
    values[:2, :2] = 100  # NW quadrant full snow
    values[2:, 2:] = 106  # SE quadrant cloud
    grid = snow.analyze_grid(make_snow_image(values), 2, 2)
    assert grid[0][0] == pytest.approx(1.0)
    assert grid[0][1] == pytest.approx(0.0)
    assert grid[1][0] == pytest.approx(0.0)
    assert grid[1][1] is None  # all cloud


def test_rejects_rgb():
    with pytest.raises(ValueError):
        snow.analyze(Image.new("RGB", (4, 4)))
