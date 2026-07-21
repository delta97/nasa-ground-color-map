from shapely.geometry import shape

from nasa_ground_color_map.processing.geometry import aggregate_rgb, mask_grid, normalize_geometry


def test_polygon_mask_uses_cell_centers():
    polygon = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 2], [0, 2], [0, 0]]]}
    matrix = [[[1, 2, 3], [4, 5, 6]], [[7, 8, 9], [10, 11, 12]]]
    masked = mask_grid(matrix, polygon, [0, 0, 2, 2])
    assert masked == [[[1, 2, 3], None], [[7, 8, 9], None]]
    assert aggregate_rgb(masked) == [4, 5, 6]


def test_antimeridian_polygon_is_split():
    raw = {"type": "Polygon", "coordinates": [[[179, 10], [-179, 10], [-179, 12], [179, 12], [179, 10]]]}
    normalized, wraps = normalize_geometry(raw)
    assert wraps is True
    assert shape(normalized).geom_type == "MultiPolygon"
    assert len(shape(normalized).geoms) == 2
