import numpy as np
from PIL import Image

from nasa_ground_color_map.processing import colors


def test_grid_exact_halves():
    im = Image.new("RGB", (100, 100), (255, 0, 0))
    im.paste(Image.new("RGB", (50, 100), (0, 0, 255)), (50, 0))
    grid = colors.to_grid(im, 1, 2)
    assert grid == [[[255, 0, 0], [0, 0, 255]]]


def test_grid_area_average():
    im = Image.new("RGB", (2, 1))
    im.putpixel((0, 0), (0, 0, 0))
    im.putpixel((1, 0), (200, 100, 50))
    grid = colors.to_grid(im, 1, 1)
    assert grid == [[[100, 50, 25]]]


def test_average_color():
    im = Image.new("RGB", (10, 10), (10, 20, 30))
    im.paste(Image.new("RGB", (10, 5), (30, 40, 50)), (0, 5))
    assert colors.average(im) == (20, 30, 40)


def test_grid_shape():
    im = Image.new("RGB", (512, 256), (5, 5, 5))
    grid = colors.to_grid(im, 4, 8)
    arr = np.array(grid)
    assert arr.shape == (4, 8, 3)


def test_rgb_to_hex():
    assert colors.rgb_to_hex((176, 148, 108)) == "#b0946c"
    assert colors.rgb_to_hex((0, 0, 0)) == "#000000"
