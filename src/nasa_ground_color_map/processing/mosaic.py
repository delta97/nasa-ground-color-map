"""Stitch fetched tiles into one image and crop to the request bbox."""

from io import BytesIO

from PIL import Image

from ..gibs.tilemath import TilePlan


def stitch_and_crop(
    tiles: dict[tuple[int, int], bytes | None],
    plan: TilePlan,
    mode: str = "RGB",
) -> tuple[Image.Image, int]:
    """Paste tiles into a mosaic, crop to the bbox rectangle.

    Missing tiles are left as black fill (RGB) or the 255 fill value (P) and
    counted. For mode "P" the palette of the first present tile is adopted so
    the raw data values survive; tiles are pasted without conversion.
    """
    size = (plan.n_cols * plan.tile_px, plan.n_rows * plan.tile_px)
    mosaic = Image.new("RGB", size) if mode == "RGB" else Image.new("P", size, 255)
    missing = 0
    palette_set = mode == "RGB"
    for (row, col), data in tiles.items():
        if data is None:
            missing += 1
            continue
        tile = Image.open(BytesIO(data))
        if mode == "RGB":
            tile = tile.convert("RGB")
        elif tile.mode != "P":
            raise ValueError(f"expected paletted tile, got mode {tile.mode}")
        elif not palette_set:
            mosaic.putpalette(tile.getpalette())
            palette_set = True
        x = (col - plan.col_min) * plan.tile_px
        y = (row - plan.row_min) * plan.tile_px
        mosaic.paste(tile, (x, y))
    cropped = mosaic.crop((plan.crop_left, plan.crop_top, plan.crop_right, plan.crop_bottom))
    return cropped, missing
