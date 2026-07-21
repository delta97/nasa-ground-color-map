from datetime import date

from nasa_ground_color_map.api import deps
from nasa_ground_color_map.config import Settings


class LatestDates:
    def __init__(self, value):
        self.value = value

    def latest_for(self, _layer):
        return self.value


def test_omitted_date_uses_previous_completed_utc_day(monkeypatch):
    monkeypatch.setattr(deps, "utc_today", lambda: date(2026, 7, 20))
    assert deps.resolve_date(None, "layer", LatestDates("2026-07-20"), Settings()) == (
        "2026-07-19", "previous_completed_day"
    )


def test_capability_lag_is_reported_as_fallback(monkeypatch):
    monkeypatch.setattr(deps, "utc_today", lambda: date(2026, 7, 20))
    assert deps.resolve_date(None, "layer", LatestDates("2026-07-17"), Settings()) == (
        "2026-07-17", "latest_available_fallback"
    )


def test_latest_and_exact_dates_remain_explicit(monkeypatch):
    monkeypatch.setattr(deps, "utc_today", lambda: date(2026, 7, 20))
    latest = LatestDates("2026-07-18")
    assert deps.resolve_date("latest", "layer", latest, Settings()) == ("2026-07-18", "latest")
    assert deps.resolve_date("2026-07-01", "layer", latest, Settings()) == ("2026-07-01", "request")
