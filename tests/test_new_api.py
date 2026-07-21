from io import BytesIO
from zipfile import ZipFile

from PIL import Image

from tests.conftest import make_tile_bytes
from tests.test_api import BASE, api, mock_truecolor_tiles


def test_history_best_and_composite(api, red_tile):
    client, router = api; route = router.get(url__regex=rf"{BASE}/\w+_CorrectedReflectance_TrueColor/default/.*\.jpg")
    route.respond(200, content=red_tile, headers={"content-type": "image/jpeg"})
    common = {"bbox": "-117.3,32.6,-117.0,32.9", "end": "2026-07-03"}
    history = client.get("/v1/history", params={**common, "start": "2026-07-01", "metrics": "color"})
    assert history.status_code == 200 and len(history.json()["observations"]) == 3
    best = client.get("/v1/best", params={**common, "metric": "color", "lookback_days": 3})
    assert best.status_code == 200 and best.json()["selected"]["date"] == "2026-07-03"
    composite = client.get("/v1/composite", params={**common, "days": 3, "rows": 2, "cols": 2})
    assert composite.status_code == 200
    assert composite.json()["matrix"][0][0][0] > 250
    assert composite.json()["observation_counts"] == [[3, 3], [3, 3]]
    calls = route.call_count
    assert client.get("/v1/composite", params={**common, "days": 3, "rows": 2, "cols": 2}).status_code == 200
    assert route.call_count == calls


def test_temporal_range_limits(api):
    client, _ = api
    response = client.get("/v1/history", params={"bbox": "0,0,1,1", "start": "2026-01-01", "end": "2026-02-01"})
    assert response.status_code == 400 and "31" in response.json()["detail"]
    response = client.get("/v1/composite", params={"bbox": "0,0,1,1", "days": 15})
    assert response.status_code == 400 and "14" in response.json()["detail"]


def test_geometry_normalization_sampling_and_png_export(api, red_tile):
    client, router = api; mock_truecolor_tiles(router, red_tile)
    geometry = {"type": "Polygon", "coordinates": [[[-117.3, 32.6], [-117.15, 32.6], [-117.15, 32.9], [-117.3, 32.9], [-117.3, 32.6]]]}
    normalized = client.post("/v1/areas/normalize", json=geometry)
    assert normalized.status_code == 200 and normalized.json()["wraps_antimeridian"] is False
    body = {"geometry": geometry, "rows": 2, "cols": 2, "observation": {"mode": "single", "date": "2026-07-01"}}
    sampled = client.post("/v1/areas/sample", json=body)
    assert sampled.status_code == 200 and sampled.json()["rgb"][0] > 250
    exported = client.post("/v1/exports", json={**body, "format": "png-bundle"})
    assert exported.status_code == 200
    bundle = ZipFile(BytesIO(exported.content))
    assert set(bundle.namelist()) == {"color.png", "quality.png", "metadata.json"}


def test_low_zoom_xyz_and_derived_cache(api, red_tile):
    client, router = api; mock_truecolor_tiles(router, red_tile)
    first = client.get("/v1/tiles/0/0/0.png", params={"date": "2026-07-01"})
    assert first.status_code == 200 and first.headers["x-derived-cache"] == "miss"
    assert Image.open(BytesIO(first.content)).size == (256, 256)
    second = client.get("/v1/tiles/0/0/0.png", params={"date": "2026-07-01"})
    assert second.status_code == 200 and second.headers["x-derived-cache"] == "hit"


def test_environment_ndvi_decodes_pinned_value(api):
    client, router = api
    # An exact official MODIS_NDVI colormap color for the first visible bin.
    png = make_tile_bytes((241, 236, 236), fmt="PNG")
    router.get(url__regex=rf"{BASE}/VIIRS_SNPP_NDVI_8Day/default/.*\.png").respond(200, content=png, headers={"content-type": "image/png"})
    response = client.get("/v1/environment/ndvi", params={"bbox": "-117.3,32.6,-117.0,32.9", "date": "2026-07-01", "rows": 2, "cols": 2})
    assert response.status_code == 200
    assert response.json()["valid_fraction"] == 1
    assert 0 <= response.json()["mean"] < .01
