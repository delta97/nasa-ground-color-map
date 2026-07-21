"""Terminal client for the ground-color pipeline.

Runs the same tile-fetch/color pipeline as the API, in-process (no server
needed), and renders the results as actual colors in the terminal using
24-bit ANSI escapes: a swatch for single colors, a colored grid for
matrices, and a brown-to-white ramp for snow cover.

Anywhere a bbox is accepted, a 5-digit US ZIP code works too — it is
resolved to a bounding box around the ZIP's centroid (see --radius-km).
While working, transient status lines (tile-fetch progress, capability
downloads, ZIP lookups) are shown on stderr when it is a terminal.
"""

import argparse
import asyncio
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from itertools import zip_longest
from pathlib import Path

import httpx

from . import __version__
from .config import Settings
from .gibs import layers as layer_registry
from .gibs.cache import TileCache
from .gibs.capabilities import parse_latest_dates
from .gibs.client import GibsClient
from .gibs.tilemath import BBox, parse_bbox_string, pixel_deg, plan_tiles
from .processing import colors, mosaic
from .processing import snow as snow_mod
from .processing import quality
from .processing.temporal import color_rank_key, composite_rgb, inclusive_dates, snow_rank_key

RESET = "\x1b[0m"
DIM = "\x1b[2m"

ZIP_RE = re.compile(r"^\d{5}(?:-\d{4})?$")
ZIP_API = "https://api.zippopotam.us/us"


# ---------------------------------------------------------------- progress

class Progress:
    """Transient single-line status on stderr; no-op unless stderr is a TTY.

    Lines are overwritten in place with \\r and fully cleared before the
    command prints its results, so piped/JSON output is never polluted.
    """

    def __init__(self):
        self.enabled = sys.stderr.isatty()
        self._width = 0

    def update(self, message: str) -> None:
        if not self.enabled:
            return
        pad = " " * max(0, self._width - len(message))
        sys.stderr.write(f"\r{message}{pad}")
        sys.stderr.flush()
        self._width = len(message)

    def clear(self) -> None:
        if not self.enabled or not self._width:
            return
        sys.stderr.write("\r" + " " * self._width + "\r")
        sys.stderr.flush()
        self._width = 0


PROGRESS = Progress()


def progress_bar(done: int, total: int, width: int = 22) -> str:
    filled = round(width * done / total) if total else width
    return "█" * filled + "░" * (width - filled)


# ---------------------------------------------------------------- rendering

def _fg(rgb) -> str:
    return f"\x1b[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"


def _bg(rgb) -> str:
    return f"\x1b[48;2;{rgb[0]};{rgb[1]};{rgb[2]}m"


def use_color(mode: str) -> bool:
    if mode == "always":
        return True
    if mode == "never":
        return False
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"


def swatch_lines(rgb, width: int = 10, height: int = 3) -> list[str]:
    """A solid color tile rendered as background-colored spaces."""
    return [_bg(rgb) + " " * width + RESET for _ in range(height)]


def fit_cell_size(cols: int, term_width: int) -> tuple[int, int, bool]:
    """Choose (cell_w, cell_h, half_block) so the grid fills the terminal
    with roughly square cells (terminal chars are ~1:2 wide:tall). Falls
    back to the dense half-block mode when even 2-char cells don't fit."""
    avail = max(16, term_width - 12)  # leave room for the frame's lat gutter
    cell_w = min(6, avail // cols)
    if cell_w >= 2:
        return cell_w, max(1, cell_w // 2), False
    return 1, 1, True


def render_matrix(matrix, cell_width: int = 2, cell_height: int = 1) -> list[str]:
    """cell_height terminal lines per matrix row; each cell is cell_width
    bg-colored spaces."""
    lines = []
    for row in matrix:
        line = "".join(_bg(cell) + " " * cell_width for cell in row) + RESET
        lines.extend([line] * cell_height)
    return lines


def render_matrix_half(matrix) -> list[str]:
    """Two matrix rows per terminal line using the upper-half-block glyph
    (fg = top cell, bg = bottom cell) — cells come out roughly square and
    the grid is 4x denser, which suits large matrices."""
    lines = []
    for i in range(0, len(matrix), 2):
        top = matrix[i]
        bottom = matrix[i + 1] if i + 1 < len(matrix) else None
        line = ""
        for j, cell in enumerate(top):
            line += _fg(cell) + (_bg(bottom[j]) if bottom else "") + "▀"
        lines.append(line + RESET)
    return lines


def frame_matrix(lines: list[str], box: BBox, grid_width: int, color: bool = True) -> list[str]:
    """Wrap rendered grid lines in a map-style border: lon labels along the
    top, lat labels at the corners, and a north indicator."""
    dim, reset = (DIM, RESET) if color else ("", "")
    lat_top, lat_bot = f"{box.max_lat:.3f}°", f"{box.min_lat:.3f}°"
    lon_l, lon_r = f"{box.min_lon:.3f}°", f"{box.max_lon:.3f}°"
    gutter = max(len(lat_top), len(lat_bot))
    gap = max(1, grid_width - len(lon_l) - len(lon_r))
    out = [f"{' ' * (gutter + 2)}{dim}{lon_l}{' ' * gap}{lon_r}{reset}"]
    out.append(f"{dim}{lat_top:>{gutter}} ╭{'─' * grid_width}╮ N↑{reset}")
    for line in lines:
        out.append(f"{' ' * gutter} {dim}│{reset}{line}{dim}│{reset}")
    out.append(f"{dim}{lat_bot:>{gutter}} ╰{'─' * grid_width}╯{reset}")
    return out


def render_hex_matrix(matrix) -> list[str]:
    return [" ".join(colors.rgb_to_hex(tuple(cell)) for cell in row) for row in matrix]


def snow_fraction_color(frac: float) -> tuple[int, int, int]:
    """Ramp from bare-ground brown (0) to snow white (1)."""
    bare, white = (110, 84, 60), (255, 255, 255)
    return tuple(round(b + (w - b) * frac) for b, w in zip(bare, white))


def render_snow_matrix(matrix, cell_width: int = 2, color: bool = True) -> list[str]:
    lines = []
    for row in matrix:
        line = ""
        for frac in row:
            if frac is None:
                line += (DIM if color else "") + "·" * cell_width + (RESET if color else "")
            elif color:
                line += _bg(snow_fraction_color(frac)) + " " * cell_width + RESET
            else:
                line += f"{frac:.2f}".ljust(cell_width + 1)
        lines.append(line)
    return lines


# ---------------------------------------------------------------- plumbing

def build_settings() -> Settings:
    if "CACHE_DIR" in os.environ:
        return Settings()
    return Settings(cache_dir=str(Path.home() / ".cache" / "nasa-ground-color-map"))


def resolve_date(date_arg: str | None, layer_id: str, settings: Settings) -> tuple[str, str]:
    """Return (concrete YYYY-MM-DD, resolved_from)."""
    if date_arg and date_arg != "latest":
        try:
            return datetime.strptime(date_arg, "%Y-%m-%d").date().isoformat(), "request"
        except ValueError:
            raise SystemExit("error: date must be YYYY-MM-DD (or 'latest')")
    advertised = latest_date_cached(settings, layer_id)
    if date_arg == "latest":
        return advertised, "latest"
    target = datetime.now(timezone.utc).date() - timedelta(days=settings.default_imagery_lag_days)
    advertised_day = datetime.strptime(advertised, "%Y-%m-%d").date()
    if advertised_day < target:
        return advertised, "latest_available_fallback"
    return target.isoformat(), "previous_completed_day"


def latest_date_cached(settings: Settings, layer_id: str) -> str:
    """Latest available date via GetCapabilities, memoized on disk for 6h
    (the capabilities document is ~5 MB; a CLI shouldn't refetch it per call)."""
    cache_file = Path(settings.cache_dir) / "latest_dates.json"
    try:
        data = json.loads(cache_file.read_text())
        if time.time() - data["fetched_at"] < settings.capabilities_refresh_seconds:
            if layer_id in data["dates"]:
                return data["dates"][layer_id]
    except (OSError, ValueError, KeyError):
        pass
    layer_ids = {l.id for l in layer_registry.all_layers()}
    try:
        PROGRESS.update("downloading GIBS layer catalog to find the latest imagery date (~5 MB, cached 6 h)…")
        resp = httpx.get(
            f"{settings.gibs_base_url}/1.0.0/WMTSCapabilities.xml",
            timeout=settings.request_timeout_seconds * 3,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
        )
        resp.raise_for_status()
        dates = parse_latest_dates(resp.text, layer_ids)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({"fetched_at": time.time(), "dates": dates}))
        if layer_id in dates:
            return dates[layer_id]
    except Exception as exc:
        PROGRESS.clear()
        print(f"warning: could not resolve latest date ({exc}); using yesterday", file=sys.stderr)
    return (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()


async def sample(settings: Settings, layer, date: str, box, rows: int, cols: int, mode: str):
    """Fetch, stitch and crop — the same pipeline the API routes run."""
    plan = plan_tiles(box, rows, cols, layer.max_zoom, settings.max_tiles_per_request, layer.tile_px)
    cache = TileCache(settings.cache_dir, settings.cache_max_bytes, settings.cache_eviction_check_interval)

    total = plan.tile_count
    done = cached = 0

    def on_tile(ok: bool, from_cache: bool) -> None:
        nonlocal done, cached
        done += 1
        cached += from_cache
        note = f", {cached} cached" if cached else ""
        PROGRESS.update(f"fetching {layer.id} {date}  {progress_bar(done, total)} "
                        f"{done}/{total} tiles (zoom {plan.zoom}{note})")

    PROGRESS.update(f"fetching {layer.id} {date}  {progress_bar(0, total)} 0/{total} tiles (zoom {plan.zoom})")
    async with httpx.AsyncClient(
        timeout=settings.request_timeout_seconds,
        headers={"User-Agent": settings.user_agent},
        follow_redirects=True,
    ) as http:
        client = GibsClient(settings, cache, http)
        tiles = await client.fetch_plan(layer, date, plan, on_tile=on_tile)
    PROGRESS.update(f"stitching {plan.n_cols}x{plan.n_rows} tiles and sampling colors…")
    cropped, missing = mosaic.stitch_and_crop(tiles, plan, mode=mode)
    PROGRESS.clear()
    return cropped, plan, missing


def parse_bbox_arg(raw: str, settings: Settings):
    try:
        return parse_bbox_string(raw, settings.max_bbox_deg)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}")


def bbox_around(lat: float, lon: float, radius_km: float) -> BBox:
    """A lat/lon box extending radius_km from a center point on each side."""
    dlat = radius_km / 110.574
    dlon = radius_km / (111.320 * max(0.01, math.cos(math.radians(lat))))
    return BBox(
        min_lon=max(-180.0, lon - dlon),
        min_lat=max(-90.0, lat - dlat),
        max_lon=min(180.0, lon + dlon),
        max_lat=min(90.0, lat + dlat),
    )


def resolve_zip(zip_arg: str, radius_km: float, settings: Settings) -> tuple[BBox, dict]:
    """US ZIP code -> (bbox around its centroid, info dict).

    Uses the free Zippopotam.us API; ZIP centroids don't move, so lookups
    are memoized on disk indefinitely."""
    code = zip_arg[:5]  # ZIP+4 collapses to the base ZIP
    cache_file = Path(settings.cache_dir) / "zip_cache.json"
    known: dict = {}
    try:
        known = json.loads(cache_file.read_text())
    except (OSError, ValueError):
        pass
    info = known.get(code)
    if info is None:
        PROGRESS.update(f"looking up ZIP {code}…")
        try:
            resp = httpx.get(
                f"{ZIP_API}/{code}",
                timeout=settings.request_timeout_seconds,
                headers={"User-Agent": settings.user_agent},
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            raise SystemExit(f"error: ZIP lookup failed ({exc})")
        if resp.status_code == 404:
            raise SystemExit(f"error: unknown US ZIP code '{code}'")
        if resp.status_code != 200:
            raise SystemExit(f"error: ZIP lookup failed (HTTP {resp.status_code})")
        place = resp.json()["places"][0]
        info = {
            "zip": code,
            "place": place["place name"],
            "state": place["state abbreviation"],
            "lat": float(place["latitude"]),
            "lon": float(place["longitude"]),
        }
        known[code] = info
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(known))
        except OSError:
            pass
    return bbox_around(info["lat"], info["lon"], radius_km), info


def parse_area_arg(raw: str, radius_km: float, settings: Settings) -> tuple[BBox, dict | None]:
    """The positional area argument accepts a bbox string or a US ZIP code."""
    if ZIP_RE.match(raw):
        return resolve_zip(raw, radius_km, settings)
    return parse_bbox_arg(raw, settings), None


def place_label(info: dict) -> str:
    return f"{info['place']}, {info['state']} (ZIP {info['zip']})"


def resolve_truecolor_layer(layer_id: str | None):
    if layer_id is None:
        return layer_registry.DEFAULT_TRUECOLOR
    layer = layer_registry.get_layer(layer_id)
    if layer is None or layer.kind != "truecolor":
        valid = ", ".join(l.id for l in layer_registry.TRUECOLOR_LAYERS)
        raise SystemExit(f"error: unknown layer '{layer_id}'; valid: {valid}")
    return layer


def quality_action(status: str) -> str:
    if status == "usable":
        return ""
    return " Suggested action: try --date latest, choose an exact date, or wait for the next completed day."


def print_quality(observation_quality) -> None:
    print(f"quality {observation_quality.status.upper()} — {observation_quality.reasons[0]}" + quality_action(observation_quality.status), file=sys.stderr)


def source_info(plan, missing, layer) -> dict:
    return {
        "zoom": plan.zoom,
        "zoom_degraded": plan.degraded,
        "tiles_fetched": plan.tile_count,
        "tiles_missing": missing,
        "native_pixel_deg": pixel_deg(plan.zoom, layer.tile_px),
    }


# ---------------------------------------------------------------- commands

def cmd_color(args) -> None:
    settings = build_settings()
    box, place = parse_area_arg(args.bbox, args.radius_km, settings)
    layer = resolve_truecolor_layer(args.layer)
    date, resolved_from = resolve_date(args.date, layer.id, settings)
    cropped, plan, missing = asyncio.run(sample(settings, layer, date, box, 1, 1, "RGB"))
    rgb = colors.average(cropped)
    hex_code = colors.rgb_to_hex(rgb)
    observation_quality = quality.color_quality(cropped, missing, plan.tile_count)
    if args.json:
        payload = {
            "bbox": [box.min_lon, box.min_lat, box.max_lon, box.max_lat],
            "date": date, "date_resolved_from": resolved_from, "layer": layer.id,
            "rgb": list(rgb), "hex": hex_code, "source": source_info(plan, missing, layer),
            "observation_quality": observation_quality.__dict__,
        }
        if place:
            payload["zip"] = place
        print(json.dumps(payload))
        return
    color = use_color(args.color)
    info = [
        f"rgb  {rgb[0]:>3} {rgb[1]:>3} {rgb[2]:>3}",
        f"hex  {hex_code}",
        f"date {date} ({resolved_from})",
        f"quality {observation_quality.status}",
    ]
    if place:
        info.append(f"area {place_label(place)}")
    if color:
        for line, text in zip_longest(swatch_lines(rgb, height=max(3, len(info))), info, fillvalue=""):
            print(f"{line}  {text}")
    else:
        for text in info:
            print(text)
    print_quality(observation_quality)


def cmd_matrix(args) -> None:
    settings = build_settings()
    box, place = parse_area_arg(args.bbox, args.radius_km, settings)
    layer = resolve_truecolor_layer(args.layer)
    date, resolved_from = resolve_date(args.date, layer.id, settings)
    cropped, plan, missing = asyncio.run(sample(settings, layer, date, box, args.rows, args.cols, "RGB"))
    matrix = colors.to_grid(cropped, args.rows, args.cols)
    observation_quality = quality.color_quality(cropped, missing, plan.tile_count)
    if args.json:
        payload = {
            "bbox": [box.min_lon, box.min_lat, box.max_lon, box.max_lat],
            "date": date, "date_resolved_from": resolved_from, "layer": layer.id,
            "rows": args.rows, "cols": args.cols, "origin": "northwest",
            "source": source_info(plan, missing, layer), "matrix": matrix,
            "observation_quality": observation_quality.__dict__,
        }
        if place:
            payload["zip"] = place
        print(json.dumps(payload))
        return
    color = use_color(args.color)
    if not color or args.hex:
        for line in render_hex_matrix(matrix):
            print(line)
    if color:
        cell_w, cell_h, half = fit_cell_size(args.cols, terminal_width())
        if half:
            body, grid_width = render_matrix_half(matrix), args.cols
        else:
            body, grid_width = render_matrix(matrix, cell_w, cell_h), args.cols * cell_w
        for line in frame_matrix(body, box, grid_width):
            print(line)
    loc = f"{place_label(place)} | " if place else ""
    print(f"{loc}{args.rows}x{args.cols} cells, north at top | {date} ({resolved_from})"
          + f" | zoom {plan.zoom}, {plan.tile_count} tiles"
          + (f", {missing} missing" if missing else ""))
    print_quality(observation_quality)


def cmd_snow(args) -> None:
    settings = build_settings()
    box, place = parse_area_arg(args.bbox, args.radius_km, settings)
    layer = layer_registry.SNOW_LAYER
    date, resolved_from = resolve_date(args.date, layer.id, settings)
    cropped, plan, missing = asyncio.run(sample(settings, layer, date, box, args.rows, args.cols, "P"))
    stats = snow_mod.analyze(cropped)
    observation_quality = quality.snow_quality(
        observable_fraction=stats.valid_fraction, cloud_fraction=stats.cloud_fraction,
        water_fraction=stats.water_fraction, tiles_missing=missing, tiles_fetched=plan.tile_count,
    )
    grid = snow_mod.analyze_grid(cropped, args.rows, args.cols) if args.rows * args.cols > 1 else None
    if args.json:
        payload = {
            "bbox": [box.min_lon, box.min_lat, box.max_lon, box.max_lat],
            "date": date, "date_resolved_from": resolved_from, "layer": layer.id,
            "snow_fraction": stats.snow_fraction, "valid_fraction": stats.valid_fraction,
            "cloud_fraction": stats.cloud_fraction, "water_fraction": stats.water_fraction,
            "matrix": grid, "source": source_info(plan, missing, layer),
            "observation_quality": observation_quality.__dict__,
        }
        if place:
            payload["zip"] = place
        print(json.dumps(payload))
        return
    color = use_color(args.color)
    if place:
        print(place_label(place))
    frac = "n/a" if stats.snow_fraction is None else f"{stats.snow_fraction:.1%}"
    if color and stats.snow_fraction is not None:
        tile = _bg(snow_fraction_color(stats.snow_fraction)) + "    " + RESET + " "
    else:
        tile = ""
    print(f"{tile}snow {frac}  (valid {stats.valid_fraction:.1%}, cloud {stats.cloud_fraction:.1%}, "
          f"water {stats.water_fraction:.1%})  {date} ({resolved_from})")
    if grid:
        for line in render_snow_matrix(grid, color=color):
            print(line)
        if color:
            legend = "".join(_bg(snow_fraction_color(f / 4)) + "  " for f in range(5))
            print(f"legend: bare {legend}{RESET} snow, {DIM}··{RESET} = no data (cloud/night/water)")
    print_quality(observation_quality)


def cmd_zip(args) -> None:
    if not ZIP_RE.match(args.zip):
        raise SystemExit("error: ZIP must be 5 digits (e.g. 92037), optionally ZIP+4")
    settings = build_settings()
    box, info = resolve_zip(args.zip, args.radius_km, settings)
    PROGRESS.clear()
    if args.json:
        print(json.dumps({
            **info, "radius_km": args.radius_km,
            "bbox": [box.min_lon, box.min_lat, box.max_lon, box.max_lat],
        }))
        return
    bbox_str = f"{box.min_lon:.4f},{box.min_lat:.4f},{box.max_lon:.4f},{box.max_lat:.4f}"
    print(f"{info['zip']}  {info['place']}, {info['state']}")
    print(f"center  {info['lat']:.4f}, {info['lon']:.4f}")
    print(f"bbox    {bbox_str}  ({args.radius_km:g} km radius)")
    print(f"try:    ground-color matrix {info['zip']}   # ZIPs work anywhere a bbox does")


def cmd_layers(args) -> None:
    settings = build_settings()
    rows = []
    for layer in layer_registry.all_layers():
        if not args.offline:
            PROGRESS.update(f"resolving latest available date for {layer.id}…")
        latest = latest_date_cached(settings, layer.id) if not args.offline else "?"
        rows.append((layer.id, layer.kind, f"{layer.tile_matrix_set}/{layer.ext}", latest))
    PROGRESS.clear()
    if args.json:
        print(json.dumps({
            "default_layer": layer_registry.DEFAULT_TRUECOLOR.id,
            "layers": [{"id": r[0], "kind": r[1], "source": r[2], "latest_available_date": r[3]} for r in rows],
        }))
        return
    width = max(len(r[0]) for r in rows)
    for r in rows:
        default = " (default)" if r[0] == layer_registry.DEFAULT_TRUECOLOR.id else ""
        print(f"{r[0]:<{width}}  {r[1]:<9} {r[2]:<9} latest {r[3]}{default}")


def _cli_date_window(end_arg: str | None, days: int, layer_id: str, settings: Settings):
    end = datetime.strptime(resolve_date(end_arg, layer_id, settings)[0], "%Y-%m-%d").date()
    return inclusive_dates(end - timedelta(days=days - 1), end)


def cmd_history(args) -> None:
    settings = build_settings(); box, place = parse_area_arg(args.bbox, args.radius_km, settings)
    layer = resolve_truecolor_layer(args.layer); dates = _cli_date_window(args.end, args.days, layer.id, settings)
    observations = []
    for day in dates:
        try:
            cropped, plan, missing = asyncio.run(sample(settings, layer, day, box, 1, 1, "RGB"))
            rgb = colors.average(cropped); q = quality.color_quality(cropped, missing, plan.tile_count)
            observations.append({"date": day, "color": {"rgb": list(rgb), "hex": colors.rgb_to_hex(rgb), "observation_quality": q.__dict__, "source": source_info(plan, missing, layer)}})
        except Exception as exc: observations.append({"date": day, "error": {"color": str(exc)}})
    payload = {"bbox": [box.min_lon, box.min_lat, box.max_lon, box.max_lat], "start": dates[0], "end": dates[-1], "metrics": ["color"], "observations": observations}
    if place: payload["zip"] = place
    if args.json: print(json.dumps(payload)); return
    for item in observations:
        if "error" in item: print(f"{item['date']}  error {item['error']['color']}")
        else:
            obs = item["color"]; print(f"{item['date']}  {obs['hex']}  {obs['observation_quality']['status']}")


def cmd_best(args) -> None:
    settings = build_settings(); box, place = parse_area_arg(args.bbox, args.radius_km, settings)
    layer = layer_registry.SNOW_LAYER if args.metric == "snow" else resolve_truecolor_layer(args.layer)
    candidates = []
    for day in _cli_date_window(args.end, args.lookback_days, layer.id, settings):
        cropped, plan, missing = asyncio.run(sample(settings, layer, day, box, 1, 1, "P" if args.metric == "snow" else "RGB"))
        if args.metric == "color":
            rgb = colors.average(cropped); q = quality.color_quality(cropped, missing, plan.tile_count)
            candidates.append({"date": day, "rgb": list(rgb), "hex": colors.rgb_to_hex(rgb), "observation_quality": q.__dict__, "source": source_info(plan, missing, layer)})
        else:
            stats = snow_mod.analyze(cropped); q = quality.snow_quality(observable_fraction=stats.valid_fraction, cloud_fraction=stats.cloud_fraction, water_fraction=stats.water_fraction, tiles_missing=missing, tiles_fetched=plan.tile_count)
            candidates.append({"date": day, "snow_fraction": stats.snow_fraction, "valid_fraction": stats.valid_fraction, "cloud_fraction": stats.cloud_fraction, "observation_quality": q.__dict__, "source": source_info(plan, missing, layer)})
    ranked = sorted(candidates, key=snow_rank_key if args.metric == "snow" else color_rank_key)
    payload = {"metric": args.metric, "selected": ranked[0], "candidates": [{"date": x["date"], "observation_quality": x["observation_quality"]} for x in ranked]}
    if args.json: print(json.dumps(payload)); return
    value = ranked[0].get("hex", f"snow {ranked[0].get('snow_fraction')}")
    print(f"selected {ranked[0]['date']}  {value}  quality {ranked[0]['observation_quality']['status']}")
    for item in ranked: print(f"  {item['date']}  {item['observation_quality']['status']}")


def cmd_composite(args) -> None:
    settings = build_settings(); box, place = parse_area_arg(args.bbox, args.radius_km, settings); layer = resolve_truecolor_layer(args.layer)
    attempted = _cli_date_window(args.end, args.days, layer.id, settings); grids, used = [], []
    for day in attempted:
        cropped, plan, missing = asyncio.run(sample(settings, layer, day, box, args.rows, args.cols, "RGB"))
        q = quality.color_quality(cropped, missing, plan.tile_count)
        if q.status != "unusable": grids.append(colors.to_grid(cropped, args.rows, args.cols)); used.append(day)
    matrix, counts, rgb = composite_rgb(grids, minimum_observations=settings.min_composite_observations)
    payload = {"bbox": [box.min_lon, box.min_lat, box.max_lon, box.max_lat], "start": attempted[0], "end": attempted[-1], "layer": layer.id,
               "rows": args.rows, "cols": args.cols, "matrix": matrix, "observation_counts": counts, "rgb": rgb,
               "hex": colors.rgb_to_hex(tuple(rgb)) if rgb else None, "dates_attempted": attempted, "dates_used": used}
    if args.json: print(json.dumps(payload)); return
    printable = [[cell or [0, 0, 0] for cell in row] for row in matrix]
    if use_color(args.color):
        for line in frame_matrix(render_matrix(printable), box, args.cols * 2): print(line)
    else:
        for row in matrix: print(" ".join(colors.rgb_to_hex(tuple(cell)) if cell else "-------" for cell in row))
    print(f"composite {payload['hex'] or 'n/a'} from {len(used)}/{len(attempted)} dates")


def cmd_area(args) -> None:
    from .api.routes_areas import _parse_gpx, _parse_kml
    from .processing.geometry import normalize_geometry
    path = Path(args.file); data = path.read_bytes(); suffix = path.suffix.lower()
    if suffix in {".json", ".geojson"}: raw = json.loads(data)
    elif suffix == ".kml": raw = _parse_kml(data, args.corridor_km)
    elif suffix == ".gpx": raw = _parse_gpx(data, args.corridor_km)
    else: raise SystemExit("error: area file must be GeoJSON, KML, or GPX")
    geometry, wraps = normalize_geometry(raw)
    payload = {"type": "Feature", "properties": {}, "geometry": geometry, "wraps_antimeridian": wraps}
    print(json.dumps(payload, indent=None if args.json else 2))


def cmd_export(args) -> None:
    from .processing.exports import raster_bundle
    from .processing.geometry import mask_grid, normalize_geometry
    settings = build_settings(); area_path = Path(args.area); geometry = None
    if area_path.is_file():
        from .api.routes_areas import _parse_gpx, _parse_kml
        data = area_path.read_bytes(); suffix = area_path.suffix.lower()
        if suffix in {".json", ".geojson"}: raw = json.loads(data)
        elif suffix == ".kml": raw = _parse_kml(data, 2.0)
        elif suffix == ".gpx": raw = _parse_gpx(data, 2.0)
        else: raise SystemExit("error: area file must be GeoJSON, KML, or GPX")
        geometry, wraps = normalize_geometry(raw)
        if wraps: raise SystemExit("error: CLI export currently requires one normalized hemisphere at a time")
        from shapely.geometry import shape
        bounds = shape(geometry).bounds; box = BBox(*bounds)
    else: box, _ = parse_area_arg(args.area, args.radius_km, settings)
    layer = resolve_truecolor_layer(args.layer)
    date, resolved = resolve_date(args.date, layer.id, settings)
    cropped, plan, missing = asyncio.run(sample(settings, layer, date, box, args.rows, args.cols, "RGB")); matrix = colors.to_grid(cropped, args.rows, args.cols)
    bbox = [box.min_lon, box.min_lat, box.max_lon, box.max_lat]; kind = {"png": "png-bundle", "geotiff": "geotiff-bundle", "cog": "cog-bundle"}[args.format]
    if geometry: matrix = mask_grid(matrix, geometry, bbox)
    metadata = {"bbox": bbox, "crs": "EPSG:4326", "date": date, "date_resolved_from": resolved, "layer": layer.id,
                "quality": quality.color_quality(cropped, missing, plan.tile_count).__dict__, "attribution": "NASA GIBS, NASA/GSFC/ESDIS"}
    Path(args.output).write_bytes(raster_bundle(matrix, bbox, metadata, kind)); print(args.output)


def cmd_refresh_environment(args) -> None:
    from .environment.refresh import refresh
    output = args.output or Path(__file__).with_name("environment") / "pinned_colormaps.json"
    print(refresh(output))


def terminal_width() -> int:
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 80


# ---------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ground-color",
        description="Sample NASA GIBS satellite imagery and show ground colors in the terminal.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    # Let a bbox that starts with a negative longitude ("-117.3,32.6,...")
    # parse as a positional instead of being mistaken for an option flag.
    bbox_matcher = re.compile(r"^-\d+.*$|^-\.\d+.*$|^-\d*\.\d+.*$")

    def common(p, grid_defaults=None):
        p._negative_number_matcher = bbox_matcher
        p.add_argument("bbox", help="minLon,minLat,maxLon,maxLat (WGS84 degrees), or a US ZIP code")
        p.add_argument("--date", help="YYYY-MM-DD or 'latest' (default: previous completed UTC day)")
        p.add_argument("--radius-km", type=float, default=5.0, metavar="KM",
                       help="box half-size when the area is given as a ZIP code (default 5)")
        p.add_argument("--json", action="store_true", help="print JSON (same shape as the API)")
        p.add_argument("--color", choices=["auto", "always", "never"], default="auto",
                       help="terminal color output (default: auto-detect)")
        if grid_defaults:
            rows, cols = grid_defaults
            p.add_argument("--rows", type=int, default=rows, metavar="N", help=f"grid rows (default {rows})")
            p.add_argument("--cols", type=int, default=cols, metavar="N", help=f"grid cols (default {cols})")

    p_color = sub.add_parser("color", help="composite color of a bbox or ZIP, with a color swatch")
    common(p_color)
    p_color.add_argument("--layer", help="true-color layer id (see 'layers')")
    p_color.set_defaults(func=cmd_color)

    p_matrix = sub.add_parser("matrix", help="grid of colors rendered as a framed terminal map")
    common(p_matrix, grid_defaults=(16, 16))
    p_matrix.add_argument("--layer", help="true-color layer id (see 'layers')")
    p_matrix.add_argument("--hex", action="store_true", help="also print the hex values grid")
    p_matrix.set_defaults(func=cmd_matrix)

    p_snow = sub.add_parser("snow", help="snow cover stats, optionally as a colored grid")
    common(p_snow, grid_defaults=(1, 1))
    p_snow.set_defaults(func=cmd_snow)

    p_zip = sub.add_parser("zip", help="resolve a US ZIP code to a bounding box")
    p_zip.add_argument("zip", help="5-digit US ZIP code (ZIP+4 accepted)")
    p_zip.add_argument("--radius-km", type=float, default=5.0, metavar="KM",
                       help="box half-size around the ZIP centroid (default 5)")
    p_zip.add_argument("--json", action="store_true")
    p_zip.set_defaults(func=cmd_zip)

    p_layers = sub.add_parser("layers", help="list available imagery layers")
    p_layers.add_argument("--json", action="store_true")
    p_layers.add_argument("--offline", action="store_true", help="skip latest-date lookup")
    p_layers.set_defaults(func=cmd_layers)

    p_history = sub.add_parser("history", help="daily quality and color trend")
    common(p_history); p_history.add_argument("--end", help="window end YYYY-MM-DD"); p_history.add_argument("--days", type=int, default=7)
    p_history.add_argument("--layer"); p_history.set_defaults(func=cmd_history)

    p_best = sub.add_parser("best", help="select the best recent observation")
    common(p_best); p_best.add_argument("--metric", choices=["color", "snow"], default="color"); p_best.add_argument("--end")
    p_best.add_argument("--lookback-days", type=int, default=7); p_best.add_argument("--layer"); p_best.set_defaults(func=cmd_best)

    p_composite = sub.add_parser("composite", help="median composite of recent observations")
    common(p_composite, grid_defaults=(16, 16)); p_composite.add_argument("--end"); p_composite.add_argument("--days", type=int, default=7)
    p_composite.add_argument("--layer"); p_composite.set_defaults(func=cmd_composite)

    p_area = sub.add_parser("area", help="normalize a GeoJSON, KML, or GPX file")
    p_area.add_argument("file"); p_area.add_argument("--corridor-km", type=float, default=2.0); p_area.add_argument("--json", action="store_true"); p_area.set_defaults(func=cmd_area)

    p_export = sub.add_parser("export", help="export a bbox or ZIP as a raster bundle")
    p_export.add_argument("area", help="bbox or ZIP"); p_export.add_argument("--format", choices=["png", "geotiff", "cog"], required=True)
    p_export.add_argument("--output", required=True); p_export.add_argument("--date"); p_export.add_argument("--layer"); p_export.add_argument("--rows", type=int, default=256); p_export.add_argument("--cols", type=int, default=256); p_export.add_argument("--radius-km", type=float, default=5.0)
    p_export.set_defaults(func=cmd_export)

    p_refresh = sub.add_parser("refresh-environment-metadata", help="deliberately refresh pinned official GIBS colormaps")
    p_refresh.add_argument("--output", help="destination JSON (default: packaged pinned_colormaps.json)")
    p_refresh.set_defaults(func=cmd_refresh_environment)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    for name in ("rows", "cols"):
        value = getattr(args, name, None)
        if value is not None and not (1 <= value <= 256):
            raise SystemExit(f"error: --{name} must be in [1, 256]")
    radius = getattr(args, "radius_km", None)
    if radius is not None and not (0 < radius <= 300):
        raise SystemExit("error: --radius-km must be in (0, 300]")
    for name, maximum in (("days", 31 if getattr(args, "command", None) == "history" else 14), ("lookback_days", 14)):
        value = getattr(args, name, None)
        if value is not None and not (1 <= value <= maximum): raise SystemExit(f"error: --{name.replace('_', '-')} must be in [1, {maximum}]")
    try:
        args.func(args)
    finally:
        PROGRESS.clear()


if __name__ == "__main__":
    main()
