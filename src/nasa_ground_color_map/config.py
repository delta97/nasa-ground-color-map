from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    gibs_base_url: str = "https://gibs.earthdata.nasa.gov/wmts/epsg4326/best"
    user_agent: str = "nasa-ground-color-map/0.1 (+https://github.com/delta97/nasa-ground-color-map)"

    cache_dir: str = "/data/tile-cache"
    cache_max_bytes: int = 1_073_741_824  # 1 GiB
    cache_eviction_check_interval: int = 100  # writes between eviction checks

    max_tiles_per_request: int = 64
    gibs_max_concurrency: int = 8
    request_timeout_seconds: float = 10.0
    fetch_retries: int = 2

    max_bbox_deg: float = 60.0  # max span per axis; set <= 0 to disable
    max_grid_cells: int = 65536  # rows * cols cap

    capabilities_refresh_seconds: int = 21600  # 6h


@lru_cache
def get_settings() -> Settings:
    return Settings()
