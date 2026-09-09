"""Forecast interval, missing-source and multi-array regressions."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.pv_excess_control import forecast as f

NOW = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)


def state(total=5, **attrs):
    return {"state": str(total), "attributes": attrs}


def sol(slots, total=5, key="detailedForecast"):
    return state(total, **{key: slots})


def slot(start, kw, **extra):
    return {"period_start": start, "pv_estimate": kw, **extra}


@pytest.mark.parametrize("value", ["unavailable", "unknown", "nan", "inf", -1, None])
def test_missing_forecast_is_not_zero(value):
    with pytest.raises(ValueError, match="sensor.pv"):
        f.GenericForecastProvider("sensor.pv").get_forecast({"sensor.pv": state(value)})


def test_hourly_solcast_is_not_halved():
    data = f.SolcastProvider("sensor.pv").get_forecast(
        {"sensor.pv": sol([slot(NOW, 4)], key="detailedHourly")}
    )
    assert sum(h.expected_kwh for h in data.hourly_breakdown) == 4


def test_explicit_duration_and_duplicate_do_not_invent_energy():
    row = slot(NOW, 4, period="PT15M")
    data = f.SolcastProvider("sensor.pv").get_forecast({"sensor.pv": sol([row, row])})
    assert sum(h.expected_kwh for h in data.hourly_breakdown) == 1
    assert data.hourly_breakdown[-1].end == NOW + timedelta(minutes=15)


def test_partial_interval_is_not_extended_to_calendar_hour():
    row = slot(NOW + timedelta(minutes=15), 2)
    data = f.SolcastProvider("sensor.pv").get_forecast({"sensor.pv": sol([row])})
    assert data.hourly_breakdown[0].start == NOW + timedelta(minutes=15)
    assert data.hourly_breakdown[-1].end == NOW + timedelta(minutes=45)


def test_multiple_sources_sum_overlap_and_deduplicate_entity_ids():
    provider = f.AggregatingForecastProvider(
        "solcast", ["sensor.a", "sensor.a", "sensor.b"]
    )
    data = provider.get_forecast(
        {
            "sensor.a": sol([slot(NOW, 2)], 2),
            "sensor.b": sol([slot(NOW + timedelta(minutes=15), 4)], 3),
        },
        now=NOW,
    )
    assert data.remaining_today_kwh == 5
    assert [h.expected_watts for h in data.hourly_breakdown] == [2000, 6000, 4000]
    assert sum(h.expected_kwh for h in data.hourly_breakdown) == 3


def test_missing_one_of_multiple_sources_invalidates_complete_forecast():
    provider = f.AggregatingForecastProvider("generic", ["sensor.a", "sensor.b"])
    with pytest.raises(ValueError, match="sensor.b"):
        provider.get_forecast({"sensor.a": state(3)}, now=NOW)


def test_explicit_tomorrow_replaces_primary_tomorrow_details():
    tomorrow = NOW + timedelta(days=1)
    provider = f.AggregatingForecastProvider(
        "solcast", ["sensor.a"], ["sensor.tomorrow"]
    )
    data = provider.get_forecast(
        {
            "sensor.a": sol([slot(NOW, 2), slot(tomorrow, 9)], 3),
            "sensor.tomorrow": sol([slot(tomorrow, 4)], 7),
        },
        now=NOW,
    )
    assert data.remaining_today_kwh == 3 and data.tomorrow_total_kwh == 7
    assert [h.expected_watts for h in data.hourly_breakdown] == [2000, 4000]


def test_dst_repeated_hour_preserves_absolute_duration():
    berlin = ZoneInfo("Europe/Berlin")
    a = datetime(2026, 10, 25, 2, 0, tzinfo=berlin, fold=0)
    b = datetime(2026, 10, 25, 2, 0, tzinfo=berlin, fold=1)
    data = f.SolcastProvider("sensor.pv").get_forecast(
        {"sensor.pv": sol([slot(a, 2), slot(b, 4)], key="detailedHourly")}
    )
    assert sum(h.expected_kwh for h in data.hourly_breakdown) == 6
    assert data.hourly_breakdown[0].start < data.hourly_breakdown[-1].start


def test_generic_sources_remain_totals_only():
    data = f.AggregatingForecastProvider(
        "generic", ["sensor.a", "sensor.b"]
    ).get_forecast({"sensor.a": state(3), "sensor.b": state(4)}, now=NOW)
    assert data.remaining_today_kwh == 7 and not data.hourly_breakdown


def test_forecast_solar_halfhour_durations():
    data = f.ForecastSolarProvider("sensor.pv").get_forecast(
        {
            "sensor.pv": state(
                3,
                watts={
                    NOW: 2000,
                    NOW + timedelta(minutes=30): 4000,
                    NOW + timedelta(hours=1): 0,
                },
            )
        }
    )
    assert data.hourly_breakdown[0].expected_kwh == 1


def test_forecast_solar_uses_ha_local_tomorrow(monkeypatch):
    from datetime import date

    class ProcessDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 8)

    monkeypatch.setattr(f, "date", ProcessDate)
    local = datetime(2026, 9, 9, 0, 30, tzinfo=ZoneInfo("Europe/Berlin"))
    data = f.AggregatingForecastProvider("forecast_solar", ["sensor.pv"]).get_forecast(
        {"sensor.pv": state(3, wh_days={"2026-09-09": 9000, "2026-09-10": 10000})},
        now=local,
    )
    assert data.tomorrow_total_kwh == 10


def test_forecast_solar_last_interval_uses_known_cadence():
    data = f.ForecastSolarProvider("sensor.pv").get_forecast(
        {"sensor.pv": state(3, watts={NOW: 2000, NOW + timedelta(minutes=30): 4000})}
    )
    assert sum(h.expected_kwh for h in data.hourly_breakdown) == 3
