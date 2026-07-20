from datetime import datetime, timedelta, timezone

from nasa_ground_color_map.gibs.capabilities import LatestDates, parse_latest_dates

XML = """<?xml version="1.0" encoding="UTF-8"?>
<Capabilities xmlns="http://www.opengis.net/wmts/1.0" xmlns:ows="http://www.opengis.net/ows/1.1">
  <Contents>
    <Layer>
      <ows:Identifier>LayerA</ows:Identifier>
      <Dimension>
        <ows:Identifier>Time</ows:Identifier>
        <Value>2012-05-08/2020-12-31/P1D</Value>
        <Value>2021-01-01/2026-07-18/P1D</Value>
      </Dimension>
    </Layer>
    <Layer>
      <ows:Identifier>LayerB</ows:Identifier>
      <Dimension>
        <ows:Identifier>Time</ows:Identifier>
        <Value>2024-03-01</Value>
      </Dimension>
    </Layer>
    <Layer>
      <ows:Identifier>Unregistered</ows:Identifier>
    </Layer>
  </Contents>
</Capabilities>
"""


def test_parse_ranges_and_singles():
    result = parse_latest_dates(XML, {"LayerA", "LayerB"})
    assert result == {"LayerA": "2026-07-18", "LayerB": "2024-03-01"}


def test_unregistered_layers_ignored():
    result = parse_latest_dates(XML, {"LayerA"})
    assert "Unregistered" not in result


def test_fallback_is_yesterday_utc():
    latest = LatestDates("http://example.invalid/caps.xml", {"LayerA"})
    expected = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    assert latest.latest_for("LayerA") == expected
