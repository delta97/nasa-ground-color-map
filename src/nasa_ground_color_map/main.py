import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import routes_color, routes_snow
from .config import get_settings
from .gibs.cache import TileCache
from .gibs.capabilities import LatestDates
from .gibs.client import GibsClient
from .gibs.layers import all_layers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

DESCRIPTION = """
Samples NASA GIBS daily satellite imagery (the imagery behind NASA Worldview) and
returns ground-color data for lat/lon bounding boxes: a color matrix, a composite
color, and snow-cover statistics — all date-addressable.

**Cloud caveat:** single-day true-color imagery frequently contains clouds. If a
result looks white/grey, try adjacent dates.

Imagery courtesy NASA Global Imagery Browse Services (GIBS), NASA/GSFC/ESDIS.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    cache = TileCache(settings.cache_dir, settings.cache_max_bytes, settings.cache_eviction_check_interval)
    http = httpx.AsyncClient(
        timeout=settings.request_timeout_seconds,
        headers={"User-Agent": settings.user_agent},
        follow_redirects=True,
    )
    app.state.gibs_client = GibsClient(settings, cache, http)
    app.state.latest_dates = LatestDates(
        f"{settings.gibs_base_url}/1.0.0/WMTSCapabilities.xml",
        {layer.id for layer in all_layers()},
    )

    async def refresh_loop():
        while True:
            await app.state.latest_dates.refresh(http)
            await asyncio.sleep(settings.capabilities_refresh_seconds)

    refresher = asyncio.create_task(refresh_loop())
    try:
        yield
    finally:
        refresher.cancel()
        await http.aclose()


app = FastAPI(
    title="NASA Ground Color Map",
    version=__version__,
    description=DESCRIPTION,
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # read-only public-data API
    allow_methods=["GET"],
    allow_headers=["*"],
)
app.include_router(routes_color.router)
app.include_router(routes_snow.router)


@app.get("/healthz", tags=["ops"], summary="Liveness check (does not touch GIBS)")
async def healthz():
    return {"status": "ok", "version": __version__}


def _find_frontend_dir() -> Path | None:
    import os

    candidates = [
        os.environ.get("FRONTEND_DIR"),
        Path(__file__).resolve().parents[2] / "frontend",  # repo checkout (editable install)
        Path.cwd() / "frontend",  # docker: WORKDIR /app + COPY frontend
    ]
    for cand in candidates:
        if cand and Path(cand).is_dir():
            return Path(cand)
    return None


_frontend = _find_frontend_dir()
if _frontend is not None:
    # Mounted last so API routes and /docs keep precedence.
    app.mount("/", StaticFiles(directory=_frontend, html=True), name="frontend")
