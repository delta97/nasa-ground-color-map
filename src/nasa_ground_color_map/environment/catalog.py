from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Product:
    slug: str
    layer_id: str
    title: str
    units: str | None
    cadence: str
    metrics: tuple[str, ...]
    decoder: str
    tile_matrix_set: str = "1km"
    max_zoom: int = 6
    ext: str = "png"


PRODUCTS = (
    Product("ndvi", "VIIRS_SNPP_NDVI_8Day", "Vegetation index", "NDVI", "8-day", ("mean", "min", "max", "valid_fraction"), "continuous", "500m", 7),
    Product("land-surface-temperature", "VIIRS_SNPP_Land_Surface_Temp_Day", "Land surface temperature", "°C", "daily", ("mean", "min", "max", "valid_fraction"), "temperature"),
    Product("flood", "VIIRS_Combined_Flood_1-Day", "Flood extent", None, "daily", ("flooded_fraction", "surface_water_fraction", "cloud_fraction", "no_data_fraction"), "categorical", "250m", 8),
    Product("thermal-anomalies", "VIIRS_SNPP_Thermal_Anomalies_375m_All", "Thermal anomalies", "MW", "daily", ("detection_count", "confidence_counts", "maximum_fire_radiative_power", "total_fire_radiative_power"), "vector", "500m", 7, "mvt"),
    Product("aerosol-index", "OMPS_Aerosol_Index", "UV aerosol index", "index", "daily", ("mean", "min", "max", "valid_fraction"), "continuous", "2km", 5),
    Product("soil-moisture", "SMAP_L3_Passive_Enhanced_Day_Soil_Moisture", "Surface soil moisture", "m³/m³", "daily", ("mean", "min", "max", "valid_fraction"), "continuous", "2km", 5),
)
_BY_SLUG = {p.slug: p for p in PRODUCTS}


def get_product(slug: str) -> Product | None:
    return _BY_SLUG.get(slug)
