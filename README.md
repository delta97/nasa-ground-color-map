# nasa-ground-color-map

An HTTP API that samples [NASA GIBS](https://nasa-gibs.github.io/gibs-api-docs/) daily
satellite imagery (the imagery behind NASA Worldview) and returns **ground-color data
for any lat/lon bounding box**: a pixel color matrix, a single composite color, and a
snow-cover estimate — all date-addressable.

Why? Flight simulators and other terrain renderers often use basemap imagery that is
years out of date and unrealistically green. This API gives downstream tools a current
"ground truth" color reference for any region: what color *is* the ground there right
now — brown scrub, fall foliage, fresh snow, or bare cold ground.

## Quick start

```bash
docker compose up --build
# then:
curl "http://localhost:8000/v1/color?bbox=2,26,6,29"
```

Interactive OpenAPI docs: http://localhost:8000/docs

Without Docker:

```bash
pip install -e ".[dev]"
CACHE_DIR=./tile-cache uvicorn nasa_ground_color_map.main:app
```

## CLI

Installing the package also installs a `ground-color` command that runs the same
pipeline in-process (no server needed) and **renders the colors right in your
terminal** — a swatch for single colors, a colored cell grid for matrices, and a
bare-ground-to-snow ramp for snow cover (24-bit ANSI; auto-disables when piped,
force with `--color always|never`).

```bash
pip install .

ground-color color 2,26,6,29                       # Sahara: tan swatch + rgb/hex
ground-color matrix -117.35,32.5,-116.8,33.15 --rows 12 --cols 12
ground-color matrix 92037 --rows 12 --cols 12      # US ZIP codes work anywhere a bbox does
ground-color snow -106.9,39.0,-106.0,39.7 --date 2026-01-15 --rows 8 --cols 8
ground-color zip 80435                             # ZIP -> place, centroid, bbox
ground-color layers                                # available layers + latest dates
ground-color history 2,26,6,29 --days 7 --json
ground-color best 2,26,6,29 --metric color --lookback-days 7
ground-color composite 2,26,6,29 --days 7 --rows 16 --cols 16
ground-color area route.gpx --corridor-km 2
ground-color export 2,26,6,29 --format geotiff --output sahara.zip
```

- **ZIP codes**: any command accepts a 5-digit US ZIP in place of a bbox — it is
  resolved (via the free Zippopotam.us API, cached on disk) to a box extending
  `--radius-km` (default 5) around the ZIP's centroid. `ground-color zip <code>`
  prints the resolved place and bbox by itself.
- **Progress**: while tiles are fetched (or the ~5 MB GIBS catalog is downloaded),
  a live progress bar with tile counts and cache hits is shown on stderr; it only
  appears on a terminal and is cleared before results print, so piped/JSON output
  stays clean.
- `matrix` renders a map-style frame: lat/lon edge labels, a north indicator,
  and cells auto-scaled to fill your terminal width. Very large grids switch to
  a denser half-block rendering.
- `--date YYYY-MM-DD` samples that exact day. Omit it for the previous completed
  UTC day; use `--date latest` to explicitly request GIBS's advertised latest day.
  If capabilities lag the completed day, the response reports
  `latest_available_fallback` rather than silently substituting another date.
- `--json` prints the same JSON shapes as the API, for scripting.
- `matrix --hex` also prints the hex values grid; without a color terminal it
  falls back to hex automatically.
- A pure-black result triggers a stderr hint (satellite swath gap or night
  imagery — try an adjacent date or another layer).
- Tiles share the same disk cache as the server (`CACHE_DIR`, default
  `~/.cache/nasa-ground-color-map` for the CLI).

## Endpoints

All endpoints share:

| Param | Meaning |
|---|---|
| `bbox` | `minLon,minLat,maxLon,maxLat` (WGS84 degrees) |
| `date` | `YYYY-MM-DD` for an exact day, `latest` for GIBS's advertised latest day; omit for the previous completed UTC day |
| `layer` | true-color layer id (see `/v1/layers`); default VIIRS SNPP |

### `GET /v1/color-matrix` — grid of ground colors

`rows` × `cols` grid (default 16×16, max 256×256) of area-averaged `[r, g, b]`
values. Row 0 is the northernmost row; cell `[0][0]` is the northwest corner.

```bash
curl "http://localhost:8000/v1/color-matrix?bbox=-117.25,32.55,-116.9,33.1&rows=8&cols=8"
```

```json
{
  "bbox": [-117.25, 32.55, -116.9, 33.1],
  "date": "2026-07-19",
  "date_resolved_from": "previous_completed_day",
  "layer": "VIIRS_SNPP_CorrectedReflectance_TrueColor",
  "rows": 8, "cols": 8,
  "origin": "northwest",
  "cell_size_deg": [0.04375, 0.06875],
  "source": {"zoom": 7, "zoom_degraded": false, "tiles_fetched": 2, "tiles_missing": 0, "native_pixel_deg": 0.00439},
  "observation_quality": {"status": "usable", "missing_tile_fraction": 0, "near_black_pixel_fraction": 0, "reasons": ["Source tile coverage and near-black-pixel checks are within advisory thresholds."]},
  "matrix": [[[124, 122, 113], "..."], "..."]
}
```

### `GET /v1/color` — single composite color

Area-weighted mean color of the box, as both `rgb` and `hex`.

```bash
curl "http://localhost:8000/v1/color?bbox=2,26,6,29"
# → {"rgb": [173, 153, 134], "hex": "#ad9986", ...}   (the Sahara is tan)
```

### `GET /v1/snow` — snow-cover statistics

From the MODIS NDSI Snow Cover layer (500m). `snow_fraction` is the mean NDSI
(0..1) over observable land pixels. Always check `valid_fraction` and
`cloud_fraction` — a mostly-cloudy day yields little usable signal. Optional
`rows`/`cols` return a per-cell matrix (cells with no observable land are `null`).

```bash
curl "http://localhost:8000/v1/snow?bbox=-106.9,39.0,-106.0,39.7&date=2026-01-15"
# → {"snow_fraction": 0.403, "valid_fraction": 0.988, "cloud_fraction": 0.003, ...}
```

### `GET /v1/layers` — available layers and their latest available dates

### Temporal observations

- `GET /v1/history` returns independent daily color/snow observations for up to
  31 inclusive days. One failed date does not fail the range.
- `GET /v1/best` ranks up to 14 recent color or snow observations using quality,
  coverage, and recency.
- `GET /v1/composite` creates a bounded channel-wise median color grid. Cells
  with fewer than `MIN_COMPOSITE_OBSERVATIONS` valid days are `null`.

```bash
curl "http://localhost:8000/v1/history?bbox=2,26,6,29&start=2026-07-01&end=2026-07-07"
curl "http://localhost:8000/v1/best?bbox=2,26,6,29&metric=color&lookback_days=7"
curl "http://localhost:8000/v1/composite?bbox=2,26,6,29&days=7&rows=32&cols=32"
```

### Geometry, exports, and map renderers

`POST /v1/areas/normalize`, `/v1/areas/upload`, and `/v1/areas/sample` accept
polygonal GeoJSON and uploaded GeoJSON/KML/GPX. GPX routes and tracks become
corridors. Geometry sampling masks cells by center point. Uploads are limited to
5 MB and XML is parsed with hardened defaults.

`POST /v1/exports` accepts a bbox or geometry plus a `single`, `best`, or
`composite` observation and returns a PNG, GeoTIFF, or COG ZIP bundle. Invalid
cells are transparent. `/v1/tiles/{z}/{x}/{y}.png` and `/v1/tilejson.json`
provide a derived XYZ renderer for zooms 0–12.

### Environmental products

`GET /v1/environment/products` describes the stable product catalog. A request
to `/v1/environment/{product}` returns normalized values for `ndvi`,
`land-surface-temperature`, `flood`, `thermal-anomalies`, `aerosol-index`, or
`soil-moisture`. Decoder tables are pinned with the application; they never
change in response to runtime metadata. These results interpret GIBS
visualizations and do not replace source science datasets.

Maintainers can deliberately refresh the checked-in official GIBS colormap
snapshot with `ground-color refresh-environment-metadata`; review decoder fixture
changes before committing them. The service never runs this command itself.

### Persistent monitoring

Monitoring remains off unless `MONITORING_ENABLED=true` and
`MONITORING_ADMIN_TOKEN` is set. All `/v1/regions`, `/v1/monitors`,
`/v1/monitor-events`, and `/v1/monitoring/status` operations require
`Authorization: Bearer <token>`. SQLite uses WAL and versioned migrations at
`DATABASE_PATH`. Daily rules emit transition-only trigger/recovery events;
signed HTTPS webhooks are retried three times and private destinations are
rejected by default.

When enabled, the web console exposes a token-gated monitoring panel for saving
the current target as a region, creating a typed daily rule, running monitors
manually, and reviewing observation/event history and pending deliveries. The
admin token is held only in the page session and sent in the bearer header.

### Optional interpretation

`GET /v1/features` reports whether interpretation is enabled. It remains disabled
until `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, and `INTERPRETATION_ACCESS_TOKEN`
are all configured. When enabled, `POST /v1/interpret` requires the access token
in `X-Interpretation-Token`; missing configuration returns `503` and a missing or
invalid token returns `401`.

The console’s **Interpret this result** action uses the exact resolved date of the
displayed observation and prompts for the token per request. The server reruns the
observation and shares only derived metrics with OpenRouter—never imagery,
screenshots, color matrices, keys, or user history. Its validated response contains
`summary`, `observations`, `confidence`, `limitations`, and
`recommended_next_checks` alongside the evidence used.

### `GET /healthz` — liveness (never touches GIBS)

## The cloud caveat

Single-day true-color satellite imagery frequently contains clouds — they show up
as white/grey and will skew colors toward bright neutral tones. Every color and
snow response includes `observation_quality` (`usable`, `suspect`, or `unusable`)
and evidence such as missing tiles, near-black pixels, observable land, cloud, and
water fractions. Quality is advisory: it never changes sampled pixels or silently
chooses a different date. If a result is suspect or unusable, try `date=latest`,
select an exact date, or wait for the next completed day.
Use `/v1/composite?days=N` to median-composite several days per cell and reduce
transient cloud and missing-swath effects.

## How it works

1. The bbox plus requested grid size selects a WMTS zoom level (enough resolution
   for ≥4 source pixels per output cell, and at least 64 pixels across the box).
2. The needed tiles are fetched from GIBS (`epsg4326/best`, no API key required),
   at most `MAX_TILES_PER_REQUEST` per request — oversized requests are served at
   a coarser zoom instead of erroring (`source.zoom_degraded: true`).
3. Tiles are cached on disk by layer/date/tile — a published tile for a past date
   never changes, so cached entries live until size-cap eviction.
4. The stitched image is cropped to the bbox and area-averaged (Pillow BOX
   resampling) into the requested grid.
5. Omitted dates resolve to the previous completed UTC calendar day (one-day lag by
   default), bounded by the layer's advertised GIBS date from GetCapabilities.
   Explicit `date=latest` still uses that advertised date. Capabilities refresh every 6 h.

Tile-grid math follows the GetCapabilities TileMatrixSet definitions (level-0 tile
span 288°, anchored at (-180, 90), 512px tiles) and was verified against live
imagery. The snow decoder reads GIBS palette indices per the layer's colormap
(0–100 = NDSI percent; 104/105 water; 106 cloud), also verified against live tiles.

## Configuration

See [.env.example](.env.example). Everything is env-overridable; defaults are
sensible for a single small instance. Run one uvicorn worker per container
(the outbound-concurrency cap is per-process); scale horizontally behind a load
balancer if needed.

## Limitations

- Antimeridian-crossing bboxes remain rejected for backward compatibility.
  Geometry normalization detects and splits antimeridian polygons and reports
  `wraps_antimeridian`; submit the returned per-hemisphere polygons for sampling.
- Colors are as-seen-from-space (atmosphere included), not surface reflectance.
- The snow layer lags ~1 day and has no data at night or under heavy cloud.

## Tests

```bash
pytest                                        # unit tests, no network
LIVE_GIBS_TESTS=1 pytest tests/test_live_integration.py   # hits real GIBS
```

## Attribution

We acknowledge the use of imagery provided by services from NASA's Global Imagery
Browse Services (GIBS), part of NASA's Earth Observing System Data and Information
System (EOSDIS).
