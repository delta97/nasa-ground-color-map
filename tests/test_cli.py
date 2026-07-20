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
    assert len(color_rows) == 3
    assert color_rows[0].count("\x1b[48;2;") == 4
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


def test_layers_offline(cli_env):
    cli.main(["layers", "--offline"])
    out = cli_env.readouterr().out
    assert "VIIRS_SNPP_CorrectedReflectance_TrueColor" in out
    assert "(default)" in out
