"""Polite async tile fetcher for GIBS WMTS, with disk-cache integration."""

import asyncio
import logging

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from ..config import Settings
from .cache import TileCache
from .layers import Layer
from .tilemath import TilePlan

logger = logging.getLogger(__name__)


class TileFetchError(Exception):
    """A tile could not be fetched (after retries)."""


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError))


class GibsClient:
    def __init__(self, settings: Settings, cache: TileCache, http: httpx.AsyncClient):
        self.settings = settings
        self.cache = cache
        self.http = http
        self.semaphore = asyncio.Semaphore(settings.gibs_max_concurrency)

    def tile_url(self, layer: Layer, date: str, zoom: int, row: int, col: int) -> str:
        return (
            f"{self.settings.gibs_base_url}/{layer.id}/default/{date}"
            f"/{layer.tile_matrix_set}/{zoom}/{row}/{col}.{layer.ext}"
        )

    async def fetch_tile(self, layer: Layer, date: str, zoom: int, row: int, col: int) -> bytes | None:
        """Return tile bytes, or None if the tile is unavailable."""
        cached = self.cache.get(layer.id, date, zoom, row, col, layer.ext)
        if cached is not None:
            return cached
        try:
            data = await self._fetch_remote(layer, date, zoom, row, col)
        except Exception as exc:
            logger.warning("tile fetch failed %s/%s z=%d r=%d c=%d: %s", layer.id, date, zoom, row, col, exc)
            return None
        self.cache.put(layer.id, date, zoom, row, col, layer.ext, data)
        return data

    async def _fetch_remote(self, layer: Layer, date: str, zoom: int, row: int, col: int) -> bytes:
        url = self.tile_url(layer, date, zoom, row, col)

        @retry(
            retry=retry_if_exception(_is_retryable),
            stop=stop_after_attempt(self.settings.fetch_retries + 1),
            wait=wait_exponential(multiplier=0.5, max=8),
            reraise=True,
        )
        async def _get() -> bytes:
            async with self.semaphore:
                resp = await self.http.get(url)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if "xml" in content_type:
                # GIBS returns an XML exception body (sometimes with HTTP 200)
                # for dates/tiles with no imagery.
                raise TileFetchError(f"GIBS returned an exception document for {url}")
            return resp.content

        return await _get()

    async def fetch_plan(self, layer: Layer, date: str, plan: TilePlan) -> dict[tuple[int, int], bytes | None]:
        """Fetch every tile in the plan concurrently. Missing tiles map to None."""
        coords = list(plan.tiles())
        results = await asyncio.gather(
            *(self.fetch_tile(layer, date, plan.zoom, row, col) for row, col in coords)
        )
        return dict(zip(coords, results))
