import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import routes_areas, routes_color, routes_environment, routes_exports, routes_interpret, routes_monitoring, routes_snow, routes_temporal
from .api.result_cache import ResultCache
from .config import get_settings
from .gibs.cache import TileCache
from .gibs.capabilities import LatestDates
from .gibs.client import GibsClient
from .gibs.layers import all_layers
from .environment.catalog import PRODUCTS

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
    app.state.settings = settings
    app.state.result_cache = ResultCache()
    cache = TileCache(settings.cache_dir, settings.cache_max_bytes, settings.cache_eviction_check_interval)
    http = httpx.AsyncClient(
        timeout=settings.request_timeout_seconds,
        headers={"User-Agent": settings.user_agent},
        follow_redirects=True,
    )
    app.state.gibs_client = GibsClient(settings, cache, http)
    app.state.latest_dates = LatestDates(
        f"{settings.gibs_base_url}/1.0.0/WMTSCapabilities.xml",
        {layer.id for layer in all_layers()} | {product.layer_id for product in PRODUCTS},
    )
    app.state.monitoring_store = None
    app.state.monitor_scheduler_healthy = False
    app.state.monitor_last_cycle = None
    monitor_task = None
    if settings.monitoring_enabled and settings.monitoring_admin_token:
        from datetime import datetime, timedelta, timezone
        from .monitoring import MonitoringStore
        app.state.monitoring_store = await MonitoringStore(settings.database_path).open()

        async def monitor_loop():
            from .api.routes_monitoring import execute_monitor
            while True:
                try:
                    now = datetime.now(timezone.utc)
                    due = await app.state.monitoring_store.fetchall("SELECT id FROM monitors WHERE enabled=1 AND run_hour=?", (now.hour,))
                    observation_day = (now.date() - timedelta(days=settings.default_imagery_lag_days)).isoformat()
                    for item in due: await execute_monitor(app, item["id"], observation_day)
                    await app.state.monitoring_store.prune(settings.monitor_retention_days)
                    app.state.monitor_last_cycle = datetime.now(timezone.utc).isoformat()
                    app.state.monitor_scheduler_healthy = True
                except Exception:
                    logging.exception("monitor scheduler cycle failed")
                    app.state.monitor_scheduler_healthy = False
                await asyncio.sleep(60)
        monitor_task = asyncio.create_task(monitor_loop())

    async def refresh_loop():
        while True:
            await app.state.latest_dates.refresh(http)
            await asyncio.sleep(settings.capabilities_refresh_seconds)

    refresher = asyncio.create_task(refresh_loop())
    try:
        yield
    finally:
        refresher.cancel()
        if monitor_task: monitor_task.cancel()
        if app.state.monitoring_store: await app.state.monitoring_store.close()
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
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "X-Interpretation-Token", "Content-Type"],
)
app.include_router(routes_color.router)
app.include_router(routes_snow.router)
app.include_router(routes_interpret.router)
app.include_router(routes_temporal.router)
app.include_router(routes_areas.router)
app.include_router(routes_exports.router)
app.include_router(routes_environment.router)
app.include_router(routes_monitoring.router)


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
