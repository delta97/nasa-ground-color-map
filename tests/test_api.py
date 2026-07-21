import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from nasa_ground_color_map import config
from tests.conftest import make_snow_tile_bytes, make_tile_bytes

CAPABILITIES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Capabilities xmlns="http://www.opengis.net/wmts/1.0" xmlns:ows="http://www.opengis.net/ows/1.1">
  <Contents>
    <Layer>
      <ows:Identifier>VIIRS_SNPP_CorrectedReflectance_TrueColor</ows:Identifier>
      <Dimension>
        <ows:Identifier>Time</ows:Identifier>
        <Value>2012-05-08/2026-07-18/P1D</Value>
      </Dimension>
    </Layer>
    <Layer>
      <ows:Identifier>MODIS_Terra_NDSI_Snow_Cover</ows:Identifier>
      <Dimension>
        <ows:Identifier>Time</ows:Identifier>
        <Value>2000-02-24/2026-07-17/P1D</Value>
      </Dimension>
    </Layer>
  </Contents>
</Capabilities>
"""

BASE = "https://gibs.earthdata.nasa.gov/wmts/epsg4326/best"


@pytest.fixture
def api(tmp_path, monkeypatch):
    """TestClient with a temp cache dir and all GIBS traffic mocked."""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    config.get_settings.cache_clear()
    from nasa_ground_color_map.main import app

    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BASE}/1.0.0/WMTSCapabilities.xml").respond(200, text=CAPABILITIES_XML)
        with TestClient(app) as client:
            yield client, router
    config.get_settings.cache_clear()


def mock_truecolor_tiles(router, tile_bytes: bytes):
    router.get(url__regex=rf"{BASE}/\w+_CorrectedReflectance_TrueColor/default/.*\.jpg").respond(
        200, content=tile_bytes, headers={"content-type": "image/jpeg"}
    )


def test_healthz(api):
    client, _ = api
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_layers(api):
    client, _ = api
    resp = client.get("/v1/layers")
    assert resp.status_code == 200
    body = resp.json()
    assert body["default_layer"] == "VIIRS_SNPP_CorrectedReflectance_TrueColor"
    ids = {l["id"] for l in body["layers"]}
    assert "MODIS_Terra_NDSI_Snow_Cover" in ids
    viirs = next(l for l in body["layers"] if l["id"] == body["default_layer"])
    assert viirs["latest_available_date"] == "2026-07-18"


def test_color_solid_red(api, red_tile):
    client, router = api
    mock_truecolor_tiles(router, red_tile)
    resp = client.get("/v1/color", params={"bbox": "-117.3,32.6,-117.0,32.9", "date": "2026-07-01"})
    assert resp.status_code == 200
    body = resp.json()
    r, g, b = body["rgb"]
    assert r > 250 and g < 6 and b < 6  # JPEG round-trip tolerance
    assert body["hex"].startswith("#")
    assert body["date"] == "2026-07-01"
    assert body["date_resolved_from"] == "request"
    assert body["source"]["tiles_missing"] == 0


def test_color_latest_date_resolution(api, red_tile):
    client, router = api
    mock_truecolor_tiles(router, red_tile)
    resp = client.get("/v1/color", params={"bbox": "-117.3,32.6,-117.0,32.9"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["date"] == "2026-07-18"  # capability lag wins
    assert body["date_resolved_from"] == "latest_available_fallback"


def test_color_explicit_latest_date_resolution(api, red_tile):
    client, router = api
    mock_truecolor_tiles(router, red_tile)
    resp = client.get("/v1/color", params={"bbox": "-117.3,32.6,-117.0,32.9", "date": "latest"})
    assert resp.status_code == 200
    assert resp.json()["date"] == "2026-07-18"
    assert resp.json()["date_resolved_from"] == "latest"


def test_color_matrix_shape_and_metadata(api, red_tile):
    client, router = api
    mock_truecolor_tiles(router, red_tile)
    resp = client.get(
        "/v1/color-matrix",
        params={"bbox": "-117.3,32.6,-117.0,32.9", "date": "2026-07-01", "rows": 3, "cols": 5},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["rows"] == 3 and body["cols"] == 5
    assert len(body["matrix"]) == 3
    assert all(len(row) == 5 for row in body["matrix"])
    assert all(len(cell) == 3 for row in body["matrix"] for cell in row)
    assert body["origin"] == "northwest"
    assert body["source"]["zoom"] <= 8
    assert body["cell_size_deg"][0] == pytest.approx(0.3 / 5)


def test_snow(api):
    client, router = api
    router.get(url__regex=rf"{BASE}/MODIS_Terra_NDSI_Snow_Cover/default/.*\.png").respond(
        200, content=make_snow_tile_bytes(60), headers={"content-type": "image/png"}
    )
    resp = client.get("/v1/snow", params={"bbox": "-106.9,39.0,-106.0,39.7", "date": "2026-01-15"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["snow_fraction"] == pytest.approx(0.6)
    assert body["valid_fraction"] == 1.0
    assert body["matrix"] is None


def test_snow_grid(api):
    client, router = api
    router.get(url__regex=rf"{BASE}/MODIS_Terra_NDSI_Snow_Cover/default/.*\.png").respond(
        200, content=make_snow_tile_bytes(106), headers={"content-type": "image/png"}
    )
    resp = client.get(
        "/v1/snow",
        params={"bbox": "-106.9,39.0,-106.0,39.7", "date": "2026-01-15", "rows": 2, "cols": 2},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["snow_fraction"] is None  # all cloud
    assert body["cloud_fraction"] == 1.0
    assert body["matrix"] == [[None, None], [None, None]]


def test_all_tiles_missing_is_an_unusable_observation(api):
    client, router = api
    router.get(url__regex=rf"{BASE}/\w+_CorrectedReflectance_TrueColor/default/.*\.jpg").respond(404)
    resp = client.get("/v1/color", params={"bbox": "-117.3,32.6,-117.0,32.9", "date": "2026-07-01"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["observation_quality"]["status"] == "unusable"
    assert body["observation_quality"]["missing_tile_fraction"] == 1.0


@pytest.mark.parametrize(
    "params,fragment",
    [
        ({"bbox": "1,2,3"}, "minLon,minLat,maxLon,maxLat"),
        ({"bbox": "a,b,c,d"}, "numbers"),
        ({"bbox": "-200,0,10,10"}, "longitudes"),
        ({"bbox": "10,0,-10,10"}, "antimeridian"),
        ({"bbox": "0,10,10,0"}, "minLat"),
        ({"bbox": "-117,32,-116,33", "date": "07/01/2026"}, "YYYY-MM-DD"),
        ({"bbox": "-117,32,-116,33", "layer": "Bogus_Layer"}, "unknown layer"),
        ({"bbox": "-100,0,0,70"}, "60"),  # exceeds MAX_BBOX_DEG
    ],
)
def test_validation_errors(api, params, fragment):
    client, _ = api
    resp = client.get("/v1/color", params=params)
    assert resp.status_code == 400
    assert fragment in resp.json()["detail"]


def test_tile_budget_respected(api, red_tile):
    client, router = api
    mock_truecolor_tiles(router, red_tile)
    resp = client.get(
        "/v1/color-matrix",
        params={"bbox": "-125,25,-66,49", "date": "2026-07-01", "rows": 256, "cols": 256},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"]["tiles_fetched"] <= 64


def test_tiles_are_cached(api, red_tile, tmp_path):
    client, router = api
    route = router.get(url__regex=rf"{BASE}/\w+_CorrectedReflectance_TrueColor/default/.*\.jpg")
    route.respond(200, content=red_tile, headers={"content-type": "image/jpeg"})
    params = {"bbox": "-117.3,32.6,-117.0,32.9", "date": "2026-07-01"}
    client.get("/v1/color", params=params)
    first_calls = route.call_count
    client.get("/v1/color", params=params)
    assert route.call_count == first_calls  # second request fully cache-served
    assert any((tmp_path / "cache").rglob("*.jpg"))
