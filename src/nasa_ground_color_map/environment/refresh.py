"""Deliberate maintainer-only refresh of pinned official GIBS colormaps."""

from __future__ import annotations

from datetime import date
from defusedxml import ElementTree
import json
import math
from pathlib import Path
import re

import httpx


SOURCES = {
    "ndvi": "MODIS_NDVI",
    "land-surface-temperature": "MODIS_Land_Surface_Temp",
    "flood": "MODIS_Flood",
    "aerosol-index": "OMPS_Aerosol_Index",
    "soil-moisture": "SMAP_Soil_Moisture",
}
BASE = "https://gibs.earthdata.nasa.gov/colormaps/v1.3"


def _representative(raw: str | None) -> float | None:
    if not raw: return None
    numbers = [float(x) for x in re.findall(r"[-+]?\d+(?:\.\d+)?", raw)]
    if not numbers: return None
    return sum(numbers[:2]) / min(2, len(numbers))


def build_tables(timeout: float = 30) -> dict:
    tables = {}
    with httpx.Client(timeout=timeout, headers={"User-Agent": "nasa-ground-color-map metadata-maintenance"}) as client:
        for slug, filename in SOURCES.items():
            url = f"{BASE}/{filename}.xml"
            response = client.get(url); response.raise_for_status()
            root = ElementTree.fromstring(response.content)
            legend = {entry.attrib.get("id"): entry.attrib.get("tooltip", "") for entry in root.findall(".//LegendEntry")}
            entries = []
            for node in root.findall(".//ColorMapEntry"):
                rgb = [int(x) for x in node.attrib["rgb"].split(",")]
                value = _representative(node.attrib.get("value") or node.attrib.get("sourceValue"))
                item = {"rgb": rgb}
                if value is not None: item["value"] = value
                item["nodata"] = node.attrib.get("nodata") == "true" or node.attrib.get("transparent") == "true"
                if slug == "land-surface-temperature" and value is not None: item["value"] = value - 273.15
                if slug == "flood":
                    tooltip = legend.get(node.attrib.get("ref"), "").lower()
                    if tooltip == "no water": item.update(label="land", nodata=False, value=0)
                    elif tooltip == "surface water": item.update(label="surface-water", nodata=False, value=1)
                    elif tooltip in {"recurring flood", "flood"}: item.update(label="flooded", nodata=False, value=2)
                    else: item.update(label="no-data", nodata=True)
                entries.append(item)
            tables[slug] = {"source": url, "pinned_on": date.today().isoformat(), "entries": entries}
    return tables


def refresh(output: str | Path) -> Path:
    path = Path(output); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(build_tables(), indent=2) + "\n")
    return path
