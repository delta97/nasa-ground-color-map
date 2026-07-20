"""Terminal client for the ground-color pipeline.

Runs the same tile-fetch/color pipeline as the API, in-process (no server
needed), and renders the results as actual colors in the terminal using
24-bit ANSI escapes: a swatch for single colors, a colored grid for
matrices, and a brown-to-white ramp for snow cover.
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from . import __version__
from .config import Settings
from .gibs import layers as layer_registry
from .gibs.cache import TileCache
from .gibs.capabilities import parse_latest_dates
from .gibs.client import GibsClient
from .gibs.tilemath import parse_bbox_string, pixel_deg, plan_tiles
from .processing import colors, mosaic
from .processing import snow as snow_mod

RESET = "\x1b[0m"
DIM = "\x1b[2m"


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


def render_matrix(matrix, cell_width: int = 2) -> list[str]:
    """One terminal line per matrix row; each cell is cell_width bg-colored spaces."""
    return ["".join(_bg(cell) + " " * cell_width for cell in row) + RESET for row in matrix]


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
    return latest_date_cached(settings, layer_id), "latest"


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
        print(f"warning: could not resolve latest date ({exc}); using yesterday", file=sys.stderr)
    return (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()


async def sample(settings: Settings, layer, date: str, box, rows: int, cols: int, mode: str):
    """Fetch, stitch and crop — the same pipeline the API routes run."""
    plan = plan_tiles(box, rows, cols, layer.max_zoom, settings.max_tiles_per_request, layer.tile_px)
    cache = TileCache(settings.cache_dir, settings.cache_max_bytes, settings.cache_eviction_check_interval)
    async with httpx.AsyncClient(
        timeout=settings.request_timeout_seconds,
        headers={"User-Agent": settings.user_agent},
        follow_redirects=True,
    ) as http:
        client = GibsClient(settings, cache, http)
        tiles = await client.fetch_plan(layer, date, plan)
    if all(v is None for v in tiles.values()):
        raise SystemExit(
            f"error: GIBS returned no imagery for {layer.id} on {date} "
            "(date outside coverage, or GIBS unavailable)"
        )
    cropped, missing = mosaic.stitch_and_crop(tiles, plan, mode=mode)
    return cropped, plan, missing


def parse_bbox_arg(raw: str, settings: Settings):
    try:
        return parse_bbox_string(raw, settings.max_bbox_deg)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}")


def resolve_truecolor_layer(layer_id: str | None):
    if layer_id is None:
        return layer_registry.DEFAULT_TRUECOLOR
    layer = layer_registry.get_layer(layer_id)
    if layer is None or layer.kind != "truecolor":
        valid = ", ".join(l.id for l in layer_registry.TRUECOLOR_LAYERS)
        raise SystemExit(f"error: unknown layer '{layer_id}'; valid: {valid}")
    return layer


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
    box = parse_bbox_arg(args.bbox, settings)
    layer = resolve_truecolor_layer(args.layer)
    date, resolved_from = resolve_date(args.date, layer.id, settings)
    cropped, plan, missing = asyncio.run(sample(settings, layer, date, box, 1, 1, "RGB"))
    rgb = colors.average(cropped)
    hex_code = colors.rgb_to_hex(rgb)
    if args.json:
        print(json.dumps({
            "bbox": [box.min_lon, box.min_lat, box.max_lon, box.max_lat],
            "date": date, "date_resolved_from": resolved_from, "layer": layer.id,
            "rgb": list(rgb), "hex": hex_code, "source": source_info(plan, missing, layer),
        }))
        return
    color = use_color(args.color)
    info = [
        f"rgb  {rgb[0]:>3} {rgb[1]:>3} {rgb[2]:>3}",
        f"hex  {hex_code}",
        f"date {date}" + (" (latest)" if resolved_from == "latest" else ""),
    ]
    if color:
        for line, text in zip(swatch_lines(rgb), info):
            print(f"{line}  {text}")
    else:
        for text in info:
            print(text)
    if missing:
        print(f"note: {missing}/{plan.tile_count} tiles missing (filled black)", file=sys.stderr)


def cmd_matrix(args) -> None:
    settings = build_settings()
    box = parse_bbox_arg(args.bbox, settings)
    layer = resolve_truecolor_layer(args.layer)
    date, resolved_from = resolve_date(args.date, layer.id, settings)
    cropped, plan, missing = asyncio.run(sample(settings, layer, date, box, args.rows, args.cols, "RGB"))
    matrix = colors.to_grid(cropped, args.rows, args.cols)
    if args.json:
        print(json.dumps({
            "bbox": [box.min_lon, box.min_lat, box.max_lon, box.max_lat],
            "date": date, "date_resolved_from": resolved_from, "layer": layer.id,
            "rows": args.rows, "cols": args.cols, "origin": "northwest",
            "source": source_info(plan, missing, layer), "matrix": matrix,
        }))
        return
    color = use_color(args.color)
    if not color or args.hex:
        for line in render_hex_matrix(matrix):
            print(line)
    if color:
        wide = args.cols * 2 > terminal_width()
        for line in (render_matrix_half(matrix) if wide else render_matrix(matrix)):
            print(line)
    print(f"{args.rows}x{args.cols} cells, north at top | {date}"
          + (" (latest)" if resolved_from == "latest" else "")
          + f" | zoom {plan.zoom}, {plan.tile_count} tiles"
          + (f", {missing} missing" if missing else ""))


def cmd_snow(args) -> None:
    settings = build_settings()
    box = parse_bbox_arg(args.bbox, settings)
    layer = layer_registry.SNOW_LAYER
    date, resolved_from = resolve_date(args.date, layer.id, settings)
    cropped, plan, missing = asyncio.run(sample(settings, layer, date, box, args.rows, args.cols, "P"))
    stats = snow_mod.analyze(cropped)
    grid = snow_mod.analyze_grid(cropped, args.rows, args.cols) if args.rows * args.cols > 1 else None
    if args.json:
        print(json.dumps({
            "bbox": [box.min_lon, box.min_lat, box.max_lon, box.max_lat],
            "date": date, "date_resolved_from": resolved_from, "layer": layer.id,
            "snow_fraction": stats.snow_fraction, "valid_fraction": stats.valid_fraction,
            "cloud_fraction": stats.cloud_fraction, "water_fraction": stats.water_fraction,
            "matrix": grid, "source": source_info(plan, missing, layer),
        }))
        return
    color = use_color(args.color)
    frac = "n/a" if stats.snow_fraction is None else f"{stats.snow_fraction:.1%}"
    if color and stats.snow_fraction is not None:
        tile = _bg(snow_fraction_color(stats.snow_fraction)) + "    " + RESET + " "
    else:
        tile = ""
    print(f"{tile}snow {frac}  (valid {stats.valid_fraction:.1%}, cloud {stats.cloud_fraction:.1%}, "
          f"water {stats.water_fraction:.1%})  {date}"
          + (" (latest)" if resolved_from == "latest" else ""))
    if grid:
        for line in render_snow_matrix(grid, color=color):
            print(line)
        if color:
            legend = "".join(_bg(snow_fraction_color(f / 4)) + "  " for f in range(5))
            print(f"legend: bare {legend}{RESET} snow, {DIM}··{RESET} = no data (cloud/night/water)")


def cmd_layers(args) -> None:
    settings = build_settings()
    rows = []
    for layer in layer_registry.all_layers():
        latest = latest_date_cached(settings, layer.id) if not args.offline else "?"
        rows.append((layer.id, layer.kind, f"{layer.tile_matrix_set}/{layer.ext}", latest))
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
        p.add_argument("bbox", help="minLon,minLat,maxLon,maxLat (WGS84 degrees)")
        p.add_argument("--date", help="YYYY-MM-DD or 'latest' (default: latest available)")
        p.add_argument("--json", action="store_true", help="print JSON (same shape as the API)")
        p.add_argument("--color", choices=["auto", "always", "never"], default="auto",
                       help="terminal color output (default: auto-detect)")
        if grid_defaults:
            rows, cols = grid_defaults
            p.add_argument("--rows", type=int, default=rows, metavar="N", help=f"grid rows (default {rows})")
            p.add_argument("--cols", type=int, default=cols, metavar="N", help=f"grid cols (default {cols})")

    p_color = sub.add_parser("color", help="composite color of a bbox, with a color swatch")
    common(p_color)
    p_color.add_argument("--layer", help="true-color layer id (see 'layers')")
    p_color.set_defaults(func=cmd_color)

    p_matrix = sub.add_parser("matrix", help="grid of colors rendered as terminal cells")
    common(p_matrix, grid_defaults=(16, 16))
    p_matrix.add_argument("--layer", help="true-color layer id (see 'layers')")
    p_matrix.add_argument("--hex", action="store_true", help="also print the hex values grid")
    p_matrix.set_defaults(func=cmd_matrix)

    p_snow = sub.add_parser("snow", help="snow cover stats, optionally as a colored grid")
    common(p_snow, grid_defaults=(1, 1))
    p_snow.set_defaults(func=cmd_snow)

    p_layers = sub.add_parser("layers", help="list available imagery layers")
    p_layers.add_argument("--json", action="store_true")
    p_layers.add_argument("--offline", action="store_true", help="skip latest-date lookup")
    p_layers.set_defaults(func=cmd_layers)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    for name in ("rows", "cols"):
        value = getattr(args, name, None)
        if value is not None and not (1 <= value <= 256):
            raise SystemExit(f"error: --{name} must be in [1, 256]")
    args.func(args)


if __name__ == "__main__":
    main()
