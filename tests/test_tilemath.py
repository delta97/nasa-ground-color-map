import math

import pytest

from nasa_ground_color_map.gibs.tilemath import (
    BBox,
    lonlat_to_tile,
    matrix_size,
    pixel_deg,
    plan_tiles,
    select_zoom,
    tile_span_deg,
)


class TestGridBasics:
    def test_tile_span(self):
        # Spans derived from the live GetCapabilities TileMatrixSet definitions
        assert tile_span_deg(0) == 288.0
        assert tile_span_deg(3) == 36.0
        assert tile_span_deg(6) == 4.5
        assert tile_span_deg(8) == 1.125

    def test_matrix_sizes_match_capabilities(self):
        # Verbatim MatrixWidth x MatrixHeight from GetCapabilities (250m set)
        assert matrix_size(0) == (2, 1)
        assert matrix_size(1) == (3, 2)
        assert matrix_size(2) == (5, 3)
        assert matrix_size(3) == (10, 5)
        assert matrix_size(6) == (80, 40)
        assert matrix_size(8) == (320, 160)

    def test_pixel_deg(self):
        assert pixel_deg(6) == pytest.approx(4.5 / 512)
        assert pixel_deg(8) == pytest.approx(1.125 / 512)  # ~244m, the "250m" resolution

    def test_known_good_example_tile(self):
        """The GIBS docs example tile 250m/6/13/36 shows the Canary Islands
        (verified visually against the live tile): lon [-18, -13.5], lat [27, 31.5]."""
        assert lonlat_to_tile(-15.75, 29.25, 6) == (36, 13)  # tile center
        assert lonlat_to_tile(-18.0, 31.5, 6) == (36, 13)  # NW corner stays in-tile
        # Tenerife
        assert lonlat_to_tile(-16.6, 28.3, 6) == (36, 13)

    def test_world_corners_clamp(self):
        assert lonlat_to_tile(-180.0, 90.0, 3) == (0, 0)
        # far SE corner clamps into the padded 10x5 matrix at z=3
        assert lonlat_to_tile(180.0, -90.0, 3) == (9, 4)

    def test_grid_shape(self):
        # zoom 0: 2 cols x 1 row; col 0 spans lon [-180, 108)
        assert lonlat_to_tile(-90.0, 0.0, 0) == (0, 0)
        assert lonlat_to_tile(150.0, 0.0, 0) == (1, 0)


class TestSelectZoom:
    def test_small_box_wants_max_zoom(self):
        box = BBox(-117.3, 32.6, -117.0, 32.9)  # San Diego-ish
        assert select_zoom(box, 64, 64, max_zoom=8) == 8

    def test_large_box_coarse(self):
        box = BBox(-180, -90, 180, 90)
        z = select_zoom(box, 4, 4, max_zoom=8)
        assert z <= 2

    def test_monotonic_in_grid_size(self):
        box = BBox(-10, -10, 10, 10)
        zooms = [select_zoom(box, n, n, max_zoom=8) for n in (1, 4, 16, 64, 256)]
        assert zooms == sorted(zooms)


class TestPlanTiles:
    def test_small_box_small_plan(self):
        # Small box within tile z=6 col=36 row=13; whatever zoom is chosen,
        # the tile range must cover the bbox and stay small.
        box = BBox(-78.0, 51.0, -76.5, 52.5)
        plan = plan_tiles(box, 8, 8, max_zoom=6, max_tiles=64)
        assert not plan.degraded
        assert plan.tile_count <= 4
        col_lo, row_lo = lonlat_to_tile(box.min_lon, box.max_lat, plan.zoom)
        col_hi, row_hi = lonlat_to_tile(box.max_lon, box.min_lat, plan.zoom)
        assert plan.col_min <= col_lo and plan.col_max >= col_hi
        assert plan.row_min <= row_lo and plan.row_max >= row_hi

    def test_quality_floor_small_bbox(self):
        # A city-sized bbox with a 1x1 grid must still sample fine imagery:
        # the smaller axis should span >= 64 source pixels (or hit max zoom).
        box = BBox(-117.3, 32.6, -117.0, 32.9)
        plan = plan_tiles(box, 1, 1, max_zoom=8, max_tiles=64)
        px = pixel_deg(plan.zoom)
        assert min(box.width, box.height) / px >= 64 or plan.zoom == 8

    def test_crop_rect_matches_bbox(self):
        box = BBox(-78.0, 51.0, -76.5, 52.5)
        plan = plan_tiles(box, 8, 8, max_zoom=6, max_tiles=64)
        px = pixel_deg(plan.zoom)
        span = tile_span_deg(plan.zoom)
        west = plan.col_min * span - 180.0
        north = 90.0 - plan.row_min * span
        assert plan.crop_left == math.floor((box.min_lon - west) / px)
        assert plan.crop_top == math.floor((north - box.max_lat) / px)
        assert plan.crop_right == math.ceil((box.max_lon - west) / px)
        assert plan.crop_bottom == math.ceil((north - box.min_lat) / px)
        assert 0 <= plan.crop_left < plan.crop_right <= plan.n_cols * 512
        assert 0 <= plan.crop_top < plan.crop_bottom <= plan.n_rows * 512

    def test_budget_degrades_zoom(self):
        box = BBox(-125.0, 25.0, -66.0, 49.0)  # continental US
        plan_generous = plan_tiles(box, 256, 256, max_zoom=8, max_tiles=10_000)
        plan_capped = plan_tiles(box, 256, 256, max_zoom=8, max_tiles=1)
        assert plan_capped.tile_count == 1
        assert plan_capped.zoom < plan_generous.zoom
        assert plan_capped.degraded
        assert not plan_generous.degraded

    def test_boundary_aligned_bbox_does_not_grab_extra_tile(self):
        # bbox exactly one tile at z=6: [-78.75, -75.9375] x [50.625, 53.4375]
        box = BBox(-78.75, 50.625, -75.9375, 53.4375)
        plan = plan_tiles(box, 4, 4, max_zoom=6, max_tiles=64)
        if plan.zoom == 6:
            assert plan.tile_count == 1

    def test_whole_world_fits_budget(self):
        box = BBox(-180, -90, 180, 90)
        plan = plan_tiles(box, 16, 16, max_zoom=8, max_tiles=64)
        assert plan.tile_count <= 64
        assert plan.crop_left == 0 and plan.crop_top == 0

    def test_tiny_box_nonempty_crop(self):
        box = BBox(0.0, 0.0, 1e-6, 1e-6)
        plan = plan_tiles(box, 1, 1, max_zoom=8, max_tiles=64)
        assert plan.crop_right > plan.crop_left
        assert plan.crop_bottom > plan.crop_top

    def test_tiles_iterator_order(self):
        box = BBox(-10, -10, 10, 10)
        plan = plan_tiles(box, 64, 64, max_zoom=4, max_tiles=64)
        coords = list(plan.tiles())
        assert len(coords) == plan.tile_count
        assert coords[0] == (plan.row_min, plan.col_min)
        assert coords[-1] == (plan.row_max, plan.col_max)
