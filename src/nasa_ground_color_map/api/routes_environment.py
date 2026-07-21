"""Curated, typed environmental visualization endpoints."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Query
import numpy as np
from PIL import Image

from ..environment.catalog import PRODUCTS, get_product
from ..environment.decoders import decode_raster, decode_thermal_tiles, numeric_summary, summarize_thermal_features
from ..gibs.client import GibsClient
from ..gibs.layers import Layer
from ..gibs.tilemath import plan_tiles
from ..processing import mosaic
from . import deps

router = APIRouter(prefix="/v1/environment", tags=["environment"])


@router.get("/products", summary="Curated environmental product catalog")
async def products(latest_dates=Depends(deps.get_latest_dates)):
    result = []
    for product in PRODUCTS:
        item = asdict(product); item["current_availability"] = latest_dates.latest_for(product.layer_id)
        item["scientific_use_note"] = "Decoded from a GIBS visualization; consult the source science dataset for scientific use."
        result.append(item)
    return {"products": result, "attribution": "NASA GIBS, NASA/GSFC/ESDIS"}


@router.get("/{product}", summary="Sample a normalized environmental visualization")
async def environment_product(product: str, bbox: str = Query(...), date: str | None = None,
                              rows: int = Query(16, ge=1, le=256), cols: int = Query(16, ge=1, le=256),
                              client: GibsClient = Depends(deps.get_client), latest_dates=Depends(deps.get_latest_dates)):
    spec = get_product(product)
    if spec is None: raise HTTPException(404, "unknown environmental product")
    box = deps.parse_bbox(bbox, client.settings); deps.validate_grid(rows, cols, client.settings)
    concrete, resolved = deps.resolve_date(date, spec.layer_id, latest_dates, client.settings)
    if spec.decoder == "vector":
        layer = Layer(spec.layer_id, spec.tile_matrix_set, spec.ext, spec.max_zoom, kind="environment-vector")
        plan = plan_tiles(box, rows, cols, layer.max_zoom, client.settings.max_tiles_per_request, layer.tile_px)
        tiles = await client.fetch_plan(layer, concrete, plan)
        bbox_list = [box.min_lon, box.min_lat, box.max_lon, box.max_lat]
        features = decode_thermal_tiles(tiles, plan, bbox_list)
        missing = sum(value is None for value in tiles.values())
        return {"product": product, "layer": spec.layer_id, "date": concrete, "date_resolved_from": resolved,
                "bbox": bbox_list, "units": spec.units, **summarize_thermal_features(features),
                "quality": "usable" if missing < len(tiles) else "unusable",
                "source": {"tiles_fetched": len(tiles), "tiles_missing": missing,
                           "vector_metadata": "https://gibs.earthdata.nasa.gov/vector-metadata/v1.0/FIRMS_VIIRS_Thermal_Anomalies.json",
                           "attribution": "NASA GIBS, NASA/GSFC/ESDIS"},
                "scientific_use_note": "Features are interpreted from a GIBS visualization, not a replacement for source science data."}
    layer = Layer(spec.layer_id, spec.tile_matrix_set, spec.ext, spec.max_zoom, kind="environment")
    plan = plan_tiles(box, rows, cols, layer.max_zoom, client.settings.max_tiles_per_request, layer.tile_px)
    tiles = await client.fetch_plan(layer, concrete, plan)
    image, missing = mosaic.stitch_and_crop(tiles, plan, "RGB")
    values, categories, table = decode_raster(image, product)
    common = {"product": product, "layer": spec.layer_id, "date": concrete, "date_resolved_from": resolved,
              "bbox": [box.min_lon, box.min_lat, box.max_lon, box.max_lat], "rows": rows, "cols": cols,
              "units": spec.units, "source": {"tiles_fetched": plan.tile_count, "tiles_missing": missing,
              "decoder_table": table["source"], "attribution": "NASA GIBS, NASA/GSFC/ESDIS"},
              "scientific_use_note": "Values are interpretations of rendered GIBS visualizations, not a replacement for source science data."}
    if product != "flood":
        common.update(numeric_summary(values, rows, cols, "temperature_matrix" if product == "land-surface-temperature" else ("ndvi_matrix" if product == "ndvi" else "matrix")))
        return common
    labels = np.asarray([entry.get("label", "no-data") for entry in table["entries"]])[categories]
    total = labels.size or 1
    common.update(flooded_fraction=float(np.sum(labels == "flooded") / total),
                  surface_water_fraction=float(np.sum(labels == "surface-water") / total),
                  cloud_fraction=float(np.sum(labels == "cloud") / total), no_data_fraction=float(np.sum(labels == "no-data") / total))
    small = Image.fromarray(categories.astype(np.uint8)).resize((cols, rows), Image.Resampling.NEAREST)
    label_table = [entry.get("label", "no-data") for entry in table["entries"]]
    common["categorical_matrix"] = [[label_table[int(v)] for v in row] for row in np.asarray(small)]
    return common
