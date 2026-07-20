import json

import pytest
import respx

from nasa_ground_color_map import cli
from tests.conftest import make_snow_tile_bytes, make_tile_bytes

BASE = "https://gibs.earthdata.nasa.gov/wmts/epsg4326/best"


class TestRendering:
    def test_swatch_contains_bg_escape(self):
        lines = cli.swatch_lines((10, 20, 30), width=4, height=2)
        assert len(lines) == 2
        assert all("\x1b[48;2;10;20;30m" in line and line.endswith(cli.RESET) for line in lines)

    def test_render_matrix_one_line_per_row(self):
        matrix = [[(1, 2, 3), (4, 5, 6)], [(7, 8, 9), (10, 11, 12)]]
        lines = cli.render_matrix(matrix)
        assert len(lines) == 2
        assert lines[0].count("\x1b[48;2;") == 2
        assert "\x1b[48;2;4;5;6m" in lines[0]

    def test_render_matrix_cell_height_repeats_lines(self):
        lines = cli.render_matrix([[(1, 2, 3)]], cell_width=3, cell_height=2)
        assert len(lines) == 2
        assert lines[0] == lines[1]

    def test_fit_cell_size(self):
        # narrow grid gets fat cells (capped at 6 wide), no half-block
        assert cli.fit_cell_size(4, 80) == (6, 3, False)
        # very wide grid falls back to half-block mode
        assert cli.fit_cell_size(200, 80) == (1, 1, True)

    def test_frame_matrix_labels_and_border(self):
        from nasa_ground_color_map.gibs.tilemath import BBox
        box = BBox(-117.3, 32.6, -117.0, 32.9)
        framed = cli.frame_matrix(["XXXX"], box, grid_width=4, color=False)
        assert framed[0].strip().startswith("-117.300°")
        assert "32.900°" in framed[1] and "N↑" in framed[1]
        assert framed[2] == "        │XXXX│"
        assert "32.600°" in framed[-1] and "╰" in framed[-1]

    def test_render_matrix_half_packs_two_rows(self):
        matrix = [[(1, 1, 1)], [(2, 2, 2)], [(3, 3, 3)]]
        lines = cli.render_matrix_half(matrix)
        assert len(lines) == 2  # rows 0+1 packed, row 2 alone
        assert "▀" in lines[0]
        assert "\x1b[38;2;1;1;1m" in lines[0] and "\x1b[48;2;2;2;2m" in lines[0]
        assert "\x1b[48;2;" not in lines[1]  # no bottom row for the odd tail

    def test_hex_matrix(self):
        assert cli.render_hex_matrix([[(255, 0, 0), (0, 0, 255)]]) == ["#ff0000 #0000ff"]

    def test_snow_ramp_endpoints(self):
        assert cli.snow_fraction_color(0.0) == (110, 84, 60)
        assert cli.snow_fraction_color(1.0) == (255, 255, 255)

    def test_snow_matrix_none_renders_dots(self):
        lines = cli.render_snow_matrix([[None, 1.0]], color=True)
        assert "··" in lines[0]
        assert "\x1b[48;2;255;255;255m" in lines[0]


@pytest.fixture
def cli_env(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=rf"{BASE}/\w+_CorrectedReflectance_TrueColor/default/.*\.jpg").respond(
            200, content=make_tile_bytes((255, 0, 0)), headers={"content-type": "image/jpeg"}
        )
        router.get(url__regex=rf"{BASE}/MODIS_Terra_NDSI_Snow_Cover/default/.*\.png").respond(
            200, content=make_snow_tile_bytes(60), headers={"content-type": "image/png"}
        )
        router.get("https://api.zippopotam.us/us/00000").respond(404)
        router.get(url__regex=r"https://api\.zippopotam\.us/us/\d{5}").respond(
            200,
            json={
                "post code": "92037",
                "places": [{"place name": "La Jolla", "state abbreviation": "CA",
                            "latitude": "32.8328", "longitude": "-117.2713"}],
            },
        )
        yield capsys


def test_color_command_swatch(cli_env):
    cli.main(["color", "-117.3,32.6,-117.0,32.9", "--date", "2026-07-01", "--color", "always"])
    out = cli_env.readouterr().out
    assert "\x1b[48;2;" in out  # a real colored tile
    assert "hex  #" in out
    assert "2026-07-01" in out


def test_color_command_json(cli_env):
    cli.main(["color", "-117.3,32.6,-117.0,32.9", "--date", "2026-07-01", "--json"])
    body = json.loads(cli_env.readouterr().out)
    r, g, b = body["rgb"]
    assert r > 250 and g < 6 and b < 6
    assert body["date_resolved_from"] == "request"


def test_matrix_command_grid(cli_env):
    cli.main(["matrix", "-117.3,32.6,-117.0,32.9", "--date", "2026-07-01",
              "--rows", "3", "--cols", "4", "--color", "always"])
    out = cli_env.readouterr().out
    color_rows = [l for l in out.splitlines() if "\x1b[48;2;" in l]
    # cells are scaled up to fill the terminal: a whole number of lines per matrix row
    assert color_rows and len(color_rows) % 3 == 0
    assert all(l.count("\x1b[48;2;") == 4 for l in color_rows)
    # the grid is wrapped in a labeled map frame
    assert "╭" in out and "╰" in out and "N↑" in out
    assert "-117.300°" in out and "32.900°" in out
    assert "3x4 cells" in out


def test_matrix_no_color_falls_back_to_hex(cli_env):
    cli.main(["matrix", "-117.3,32.6,-117.0,32.9", "--date", "2026-07-01",
              "--rows", "2", "--cols", "2", "--color", "never"])
    out = cli_env.readouterr().out
    assert "\x1b[" not in out
    assert out.count("#") == 4


def test_snow_command(cli_env):
    cli.main(["snow", "-106.9,39.0,-106.0,39.7", "--date", "2026-01-15",
              "--rows", "2", "--cols", "2", "--color", "always"])
    out = cli_env.readouterr().out
    assert "snow 60.0%" in out
    assert "legend" in out


def test_invalid_bbox_exits_with_message(cli_env):
    with pytest.raises(SystemExit) as exc:
        cli.main(["color", "10,0,-10,10", "--date", "2026-07-01"])
    assert "antimeridian" in str(exc.value)


def test_bad_grid_size_rejected(cli_env):
    with pytest.raises(SystemExit) as exc:
        cli.main(["matrix", "0,0,1,1", "--date", "2026-07-01", "--rows", "0"])
    assert "--rows" in str(exc.value)


def test_zip_command(cli_env):
    cli.main(["zip", "92037"])
    out = cli_env.readouterr().out
    assert "92037  La Jolla, CA" in out
    assert "center  32.8328, -117.2713" in out
    assert "bbox" in out


def test_zip_command_json_radius(cli_env):
    cli.main(["zip", "92037", "--radius-km", "10", "--json"])
    body = json.loads(cli_env.readouterr().out)
    assert body["place"] == "La Jolla"
    min_lon, min_lat, max_lon, max_lat = body["bbox"]
    assert max_lat - min_lat == pytest.approx(20 / 110.574, rel=1e-3)
    assert min_lat < 32.8328 < max_lat and min_lon < -117.2713 < max_lon


def test_color_accepts_zip_in_place_of_bbox(cli_env):
    cli.main(["color", "92037", "--date", "2026-07-01", "--json"])
    body = json.loads(cli_env.readouterr().out)
    assert body["zip"]["place"] == "La Jolla"
    min_lon, min_lat, max_lon, max_lat = body["bbox"]
    assert min_lat < 32.8328 < max_lat and min_lon < -117.2713 < max_lon
    assert body["rgb"][0] > 250  # imagery pipeline actually ran


def test_zip_lookup_is_cached_on_disk(cli_env):
    import os
    from pathlib import Path
    cli.main(["zip", "92037"])
    cli_env.readouterr()
    data = json.loads((Path(os.environ["CACHE_DIR"]) / "zip_cache.json").read_text())
    assert data["92037"]["place"] == "La Jolla"


def test_unknown_zip_exits_with_message(cli_env):
    with pytest.raises(SystemExit) as exc:
        cli.main(["zip", "00000"])
    assert "unknown US ZIP" in str(exc.value)


def test_bad_zip_format_rejected(cli_env):
    with pytest.raises(SystemExit) as exc:
        cli.main(["zip", "1234"])
    assert "5 digits" in str(exc.value)


def test_bad_radius_rejected(cli_env):
    with pytest.raises(SystemExit) as exc:
        cli.main(["color", "92037", "--radius-km", "0"])
    assert "--radius-km" in str(exc.value)


def test_layers_offline(cli_env):
    cli.main(["layers", "--offline"])
    out = cli_env.readouterr().out
    assert "VIIRS_SNPP_CorrectedReflectance_TrueColor" in out
    assert "(default)" in out
