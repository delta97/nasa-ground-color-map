import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from nasa_ground_color_map import config
from tests.conftest import make_snow_tile_bytes, make_tile_bytes

BASE = "https://gibs.earthdata.nasa.gov/wmts/epsg4326/best"
OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"

CAPABILITIES_XML = """<Capabilities xmlns="http://www.opengis.net/wmts/1.0" xmlns:ows="http://www.opengis.net/ows/1.1"><Contents>
<Layer><ows:Identifier>VIIRS_SNPP_CorrectedReflectance_TrueColor</ows:Identifier><Dimension><ows:Identifier>Time</ows:Identifier><Value>2012-05-08/2026-07-18/P1D</Value></Dimension></Layer>
<Layer><ows:Identifier>MODIS_Terra_NDSI_Snow_Cover</ows:Identifier><Dimension><ows:Identifier>Time</ows:Identifier><Value>2000-02-24/2026-07-17/P1D</Value></Dimension></Layer>
</Contents></Capabilities>"""


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    config.get_settings.cache_clear()
    from nasa_ground_color_map.main import app

    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BASE}/1.0.0/WMTSCapabilities.xml").respond(200, text=CAPABILITIES_XML)
        with TestClient(app) as client:
            yield client, router
    config.get_settings.cache_clear()


def enable_interpretation(client):
    settings = client.app.state.gibs_client.settings
    settings.openrouter_api_key = "test-key"
    settings.openrouter_model = "test/model"
    settings.interpretation_access_token = "test-token"


def mock_observation_tiles(router):
    router.get(url__regex=rf"{BASE}/\w+_CorrectedReflectance_TrueColor/default/.*\.jpg").respond(
        200, content=make_tile_bytes((30, 90, 40)), headers={"content-type": "image/jpeg"}
    )
    router.get(url__regex=rf"{BASE}/MODIS_Terra_NDSI_Snow_Cover/default/.*\.png").respond(
        200, content=make_snow_tile_bytes(60), headers={"content-type": "image/png"}
    )


def test_interpretation_disabled_by_default(api):
    client, _ = api
    assert client.get("/v1/features").json() == {"interpretation": False}
    response = client.post("/v1/interpret", json={"bbox": "-117.3,32.6,-117.0,32.9"})
    assert response.status_code == 503


def test_interpretation_enforces_token(api):
    client, _ = api
    enable_interpretation(client)
    response = client.post("/v1/interpret", json={"bbox": "-117.3,32.6,-117.0,32.9"})
    assert response.status_code == 401


def test_interpretation_sends_only_derived_evidence(api):
    client, router = api
    enable_interpretation(client)
    mock_observation_tiles(router)
    received = {}

    def provider(request):
        received.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "summary": "The color observation is greenish.", "observations": ["RGB is green-dominant."],
            "confidence": "medium", "limitations": ["One observation date."],
            "recommended_next_checks": ["Compare an exact adjacent date."],
        })}}]})

    router.post(OPENROUTER).mock(side_effect=provider)
    response = client.post(
        "/v1/interpret", headers={"X-Interpretation-Token": "test-token"},
        json={"bbox": "-117.3,32.6,-117.0,32.9", "date_mode": "request", "date": "2026-07-01", "rows": 2, "cols": 2},
    )
    assert response.status_code == 200
    serialized = json.dumps(received)
    assert "matrix" not in serialized and "test-key" not in serialized
    assert response.json()["interpretation"]["confidence"] == "medium"


def test_interpretation_rejects_malformed_provider_response(api):
    client, router = api
    enable_interpretation(client)
    mock_observation_tiles(router)
    router.post(OPENROUTER).respond(200, json={"choices": [{"message": {"content": "not json"}}]})
    response = client.post(
        "/v1/interpret", headers={"X-Interpretation-Token": "test-token"},
        json={"bbox": "-117.3,32.6,-117.0,32.9", "date_mode": "request", "date": "2026-07-01"},
    )
    assert response.status_code == 502
