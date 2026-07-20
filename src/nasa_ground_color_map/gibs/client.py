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
        data, _ = await self.fetch_tile_cached(layer, date, zoom, row, col)
        return data

    async def fetch_tile_cached(
        self, layer: Layer, date: str, zoom: int, row: int, col: int
    ) -> tuple[bytes | None, bool]:
        """Like fetch_tile, but also reports whether the tile came from the disk cache."""
        cached = self.cache.get(layer.id, date, zoom, row, col, layer.ext)
        if cached is not None:
            return cached, True
        try:
            data = await self._fetch_remote(layer, date, zoom, row, col)
        except Exception as exc:
            logger.warning("tile fetch failed %s/%s z=%d r=%d c=%d: %s", layer.id, date, zoom, row, col, exc)
            return None, False
        self.cache.put(layer.id, date, zoom, row, col, layer.ext, data)
        return data, False

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

    async def fetch_plan(
        self, layer: Layer, date: str, plan: TilePlan, on_tile=None
    ) -> dict[tuple[int, int], bytes | None]:
        """Fetch every tile in the plan concurrently. Missing tiles map to None.

        on_tile, if given, is called as on_tile(ok, from_cache) as each tile
        completes — a progress hook for interactive callers.
        """
        coords = list(plan.tiles())

        async def fetch_one(row: int, col: int) -> bytes | None:
            data, from_cache = await self.fetch_tile_cached(layer, date, plan.zoom, row, col)
            if on_tile is not None:
                on_tile(data is not None, from_cache)
            return data

        results = await asyncio.gather(*(fetch_one(row, col) for row, col in coords))
        return dict(zip(coords, results))
