"""Static registry of GIBS layers this service knows how to sample.

All layers are EPSG:4326. TileMatrixSet names are GIBS resolution names; a set
named `250m` spans zoom levels 0..8, `500m` 0..7, `1km` 0..6 (512px tiles).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Layer:
    id: str
    tile_matrix_set: str
    ext: str  # file extension: jpg or png
    max_zoom: int
    tile_px: int = 512
    kind: str = "truecolor"  # truecolor | snow


TRUECOLOR_LAYERS = [
    Layer("VIIRS_SNPP_CorrectedReflectance_TrueColor", "250m", "jpg", 8),
    Layer("VIIRS_NOAA20_CorrectedReflectance_TrueColor", "250m", "jpg", 8),
    Layer("MODIS_Terra_CorrectedReflectance_TrueColor", "250m", "jpg", 8),
    Layer("MODIS_Aqua_CorrectedReflectance_TrueColor", "250m", "jpg", 8),
]

SNOW_LAYER = Layer("MODIS_Terra_NDSI_Snow_Cover", "500m", "png", 7, kind="snow")

DEFAULT_TRUECOLOR = TRUECOLOR_LAYERS[0]

_ALL = {layer.id: layer for layer in [*TRUECOLOR_LAYERS, SNOW_LAYER]}


def get_layer(layer_id: str) -> Layer | None:
    return _ALL.get(layer_id)


def all_layers() -> list[Layer]:
    return list(_ALL.values())
