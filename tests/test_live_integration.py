"""Live tests against real GIBS. Skipped unless LIVE_GIBS_TESTS=1."""

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("LIVE_GIBS_TESTS") != "1",
    reason="set LIVE_GIBS_TESTS=1 to run live GIBS tests",
)


def test_known_example_tile_fetches():
    import httpx

    url = (
        "https://gibs.earthdata.nasa.gov/wmts/epsg4326/best/"
        "MODIS_Terra_CorrectedReflectance_TrueColor/default/2012-07-09/250m/6/13/36.jpg"
    )
    resp = httpx.get(url, timeout=30)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/jpeg")


def test_live_sahara_color(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from nasa_ground_color_map import config

    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    config.get_settings.cache_clear()
    from nasa_ground_color_map.main import app

    with TestClient(app) as client:
        resp = client.get("/v1/color", params={"bbox": "2,26,6,29", "date": "2026-07-01"})
        assert resp.status_code == 200
        r, g, b = resp.json()["rgb"]
        # Sahara sand: warm tones, red channel dominant
        assert r > g > b
        assert r > 120
    config.get_settings.cache_clear()


@pytest.mark.parametrize("product,date", [
    ("ndvi", "2026-07-01"),
    ("flood", "2026-07-01"),
    ("thermal-anomalies", "2026-07-01"),
])
def test_live_environment_representatives(tmp_path, monkeypatch, product, date):
    """One continuous raster, categorical raster, and vector visualization."""
    from fastapi.testclient import TestClient
    from nasa_ground_color_map import config

    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    config.get_settings.cache_clear()
    from nasa_ground_color_map.main import app
    with TestClient(app) as client:
        response = client.get(f"/v1/environment/{product}", params={"bbox": "-122,37,-121,38", "date": date, "rows": 2, "cols": 2})
        assert response.status_code == 200
        assert response.json()["product"] == product
    config.get_settings.cache_clear()
