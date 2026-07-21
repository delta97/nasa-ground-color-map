import hashlib
import hmac
import json

import pytest

from nasa_ground_color_map.monitoring.db import MonitoringStore
from nasa_ground_color_map.monitoring.rules import evaluate
from nasa_ground_color_map.monitoring.webhooks import encode_and_sign, validate_webhook_url


def test_threshold_transitions_and_recovery():
    first = evaluate(rule_type="above", threshold=10, value=11, previous_value=None, quality="usable", previous_quality=None)
    assert first["event"] == "triggered" and first["active"]
    repeated = evaluate(rule_type="above", threshold=10, value=12, previous_value=11, quality="usable", previous_quality="usable", was_active=True)
    assert repeated["event"] is None
    recovered = evaluate(rule_type="above", threshold=10, value=9, previous_value=12, quality="usable", previous_quality="usable", was_active=True)
    assert recovered["event"] == "recovered" and not recovered["active"]


def test_unusable_observation_not_accepted_for_scalar_rule():
    result = evaluate(rule_type="above", threshold=10, value=99, previous_value=1, quality="unusable", previous_quality="usable")
    assert result == {"accepted": False, "matches": False, "event": None, "active": False}


def test_webhook_signing_is_deterministic():
    body, signature = encode_and_sign({"b": 2, "a": 1}, "secret")
    assert body == b'{"a":1,"b":2}'
    assert signature == "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()


@pytest.mark.parametrize("url", ["http://example.com/hook", "https://127.0.0.1/hook", "https://[::1]/hook"])
def test_webhook_rejects_unsafe_urls(url):
    with pytest.raises(ValueError): validate_webhook_url(url)


@pytest.mark.asyncio
async def test_sqlite_migrations_and_unique_daily_observation(tmp_path):
    store = await MonitoringStore(str(tmp_path / "monitor.db")).open()
    try:
        region = await store.execute("INSERT INTO regions(name,geometry_json) VALUES (?,?)", ("x", json.dumps({"type":"Polygon","coordinates":[]})))
        monitor = await store.execute("INSERT INTO monitors(region_id,product,metric,rule_type,threshold,run_hour) VALUES (?,?,?,?,?,?)", (region.lastrowid,"ndvi","mean","above",.5,0))
        await store.execute("INSERT INTO observations(monitor_id,observation_date,quality,payload_json,accepted) VALUES (?,?,?,?,?)", (monitor.lastrowid,"2026-01-01","usable","{}",1))
        with pytest.raises(Exception):
            await store.execute("INSERT INTO observations(monitor_id,observation_date,quality,payload_json,accepted) VALUES (?,?,?,?,?)", (monitor.lastrowid,"2026-01-01","usable","{}",1))
        assert await store.health()
    finally: await store.close()
