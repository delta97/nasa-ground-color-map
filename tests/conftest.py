from io import BytesIO

import pytest
from PIL import Image


def make_tile_bytes(color: tuple[int, int, int], size: int = 512, fmt: str = "JPEG") -> bytes:
    buf = BytesIO()
    Image.new("RGB", (size, size), color).save(buf, format=fmt)
    return buf.getvalue()


def make_snow_tile_bytes(index_value: int, size: int = 512) -> bytes:
    """P-mode PNG where every pixel has the given palette index (data value)."""
    im = Image.new("P", (size, size), index_value)
    palette = []
    for i in range(256):
        palette.extend([i, i, i])
    im.putpalette(palette)
    buf = BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def red_tile() -> bytes:
    return make_tile_bytes((255, 0, 0))


@pytest.fixture
def blue_tile() -> bytes:
    return make_tile_bytes((0, 0, 255))
