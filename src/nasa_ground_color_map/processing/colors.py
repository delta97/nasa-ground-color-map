"""Color math: downsample a cropped image to an N x M grid; average color."""

import numpy as np
from PIL import Image


def to_grid(image: Image.Image, rows: int, cols: int) -> list[list[list[int]]]:
    """Area-averaged rows x cols grid of [r, g, b] triplets.

    BOX resampling is a true area average, i.e. each output cell is the mean
    color of the source pixels it covers.
    """
    small = image.resize((cols, rows), Image.Resampling.BOX)
    return np.asarray(small, dtype=np.uint8).tolist()


def average(image: Image.Image) -> tuple[int, int, int]:
    arr = np.asarray(image.convert("RGB"), dtype=np.float64)
    r, g, b = arr.reshape(-1, 3).mean(axis=0).round().astype(int)
    return int(r), int(g), int(b)


def rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)
