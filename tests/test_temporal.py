from nasa_ground_color_map.processing.temporal import color_rank_key, composite_rgb, snow_rank_key


def q(status="usable", **extra):
    return {"status": status, "missing_tile_fraction": 0, **extra}


def test_color_ranking_uses_quality_coverage_black_then_recency():
    items = [
        {"date": "2026-01-03", "observation_quality": q("suspect", near_black_pixel_fraction=0)},
        {"date": "2026-01-01", "observation_quality": q(near_black_pixel_fraction=.2)},
        {"date": "2026-01-02", "observation_quality": q(near_black_pixel_fraction=.1)},
    ]
    assert sorted(items, key=color_rank_key)[0]["date"] == "2026-01-02"


def test_snow_ranking_uses_observable_then_cloud_then_recency():
    items = [
        {"date": "2026-01-03", "observation_quality": q(observable_fraction=.5, cloud_fraction=.3)},
        {"date": "2026-01-02", "observation_quality": q(observable_fraction=.8, cloud_fraction=.1)},
    ]
    assert sorted(items, key=snow_rank_key)[0]["date"] == "2026-01-02"


def test_composite_median_nulls_and_near_black_exclusion():
    grids = [[[[0, 0, 0], [10, 20, 30]]], [[[100, 100, 100], [30, 40, 50]]], [[[200, 200, 200], [50, 60, 70]]]]
    matrix, counts, rgb = composite_rgb(grids, minimum_observations=2)
    assert matrix == [[[150, 150, 150], [30, 40, 50]]]
    assert counts == [[2, 3]]
    assert rgb == [90, 95, 100]


def test_composite_cell_with_one_observation_is_null():
    matrix, counts, rgb = composite_rgb([[[[20, 20, 20]]]], minimum_observations=2)
    assert matrix == [[None]] and counts == [[1]] and rgb is None


def test_thermal_feature_summary():
    from nasa_ground_color_map.environment.decoders import summarize_thermal_features
    features = [{"properties": {"FRP": 12.5, "CONFIDENCE": "h"}}, {"properties": {"FRP": "2.5", "CONFIDENCE": "nominal"}}]
    result = summarize_thermal_features(features)
    assert result["detection_count"] == 2
    assert result["confidence_counts"] == {"high": 1, "nominal": 1}
    assert result["maximum_fire_radiative_power"] == 12.5
    assert result["total_fire_radiative_power"] == 15
