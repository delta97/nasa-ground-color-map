"""Resolve the most recent available date per layer from GIBS GetCapabilities."""

import logging
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

import httpx

logger = logging.getLogger(__name__)

_NS = {
    "wmts": "http://www.opengis.net/wmts/1.0",
    "ows": "http://www.opengis.net/ows/1.1",
}


def parse_latest_dates(xml_text: str, layer_ids: set[str]) -> dict[str, str]:
    """Parse WMTSCapabilities.xml; return {layer_id: latest YYYY-MM-DD}.

    Time <Dimension> values look like "2012-05-08/2026-07-19/P1D" (possibly
    several comma/space-separated ranges) or single dates.
    """
    latest: dict[str, str] = {}
    root = ET.fromstring(xml_text)
    for layer_el in root.iterfind(".//wmts:Contents/wmts:Layer", _NS):
        ident = layer_el.findtext("ows:Identifier", default="", namespaces=_NS)
        if ident not in layer_ids:
            continue
        best: str | None = None
        for dim in layer_el.iterfind("wmts:Dimension", _NS):
            dim_id = dim.findtext("ows:Identifier", default="", namespaces=_NS)
            if dim_id.lower() != "time":
                continue
            for value_el in dim.iterfind("wmts:Value", _NS):
                for chunk in (value_el.text or "").replace(",", " ").split():
                    # range "start/end/period" or bare date
                    end = chunk.split("/")[1] if "/" in chunk else chunk
                    end = end[:10]  # trim any time-of-day suffix
                    try:
                        datetime.strptime(end, "%Y-%m-%d")
                    except ValueError:
                        continue
                    if best is None or end > best:
                        best = end
        if best:
            latest[ident] = best
    return latest


class LatestDates:
    """Holds the latest available date per layer, refreshed from GetCapabilities."""

    def __init__(self, capabilities_url: str, layer_ids: set[str]):
        self.capabilities_url = capabilities_url
        self.layer_ids = layer_ids
        self._dates: dict[str, str] = {}

    async def refresh(self, http: httpx.AsyncClient) -> None:
        try:
            resp = await http.get(self.capabilities_url)
            resp.raise_for_status()
            self._dates = parse_latest_dates(resp.text, self.layer_ids)
            logger.info("capabilities refreshed: %s", self._dates)
        except Exception as exc:
            logger.warning("capabilities refresh failed (keeping previous data): %s", exc)

    def latest_for(self, layer_id: str) -> str:
        found = self._dates.get(layer_id)
        if found:
            return found
        # Fallback if capabilities were never fetched: yesterday UTC.
        return (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
