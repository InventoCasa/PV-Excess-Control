"""Tests for the forecast provider module."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from custom_components.pv_excess_control.forecast import (
    ForecastSolarProvider,
    GenericForecastProvider,
    SolcastProvider,
    create_forecast_provider,
)
from custom_components.pv_excess_control.models import ForecastData, HourlyForecast


# ---------------------------------------------------------------------------
# TestGenericForecastProvider
# ---------------------------------------------------------------------------

class TestGenericForecastProvider:
    def test_reads_remaining_kwh(self):
        """GenericForecastProvider reads remaining kWh from state."""
        provider = GenericForecastProvider("sensor.solar_forecast")
        states = {"sensor.solar_forecast": {"state": "12.5", "attributes": {}}}
        data = provider.get_forecast(states)
        assert data.remaining_today_kwh == 12.5
        assert data.hourly_breakdown == []

    def test_unavailable_raises_unavailable(self):
        provider = GenericForecastProvider("sensor.solar_forecast")
        states = {"sensor.solar_forecast": {"state": "unavailable", "attributes": {}}}
        with pytest.raises(ValueError, match="Forecast unavailable"):
            provider.get_forecast(states)

    def test_missing_sensor_raises_unavailable(self):
        provider = GenericForecastProvider("sensor.solar_forecast")
        with pytest.raises(ValueError, match="Forecast unavailable"):
            provider.get_forecast({})

    def test_unknown_state_raises_unavailable(self):
        provider = GenericForecastProvider("sensor.solar_forecast")
        states = {"sensor.solar_forecast": {"state": "unknown", "attributes": {}}}
        with pytest.raises(ValueError, match="Forecast unavailable"):
            provider.get_forecast(states)

    def test_zero_value(self):
        provider = GenericForecastProvider("sensor.solar_forecast")
        states = {"sensor.solar_forecast": {"state": "0.0", "attributes": {}}}
        data = provider.get_forecast(states)
        assert data.remaining_today_kwh == 0.0

    def test_integer_state(self):
        provider = GenericForecastProvider("sensor.solar_forecast")
        states = {"sensor.solar_forecast": {"state": "8", "attributes": {}}}
        data = provider.get_forecast(states)
        assert data.remaining_today_kwh == 8.0

    def test_tomorrow_total_kwh_is_none(self):
        """GenericForecastProvider does not provide tomorrow total."""
        provider = GenericForecastProvider("sensor.solar_forecast")
        states = {"sensor.solar_forecast": {"state": "5.0", "attributes": {}}}
        data = provider.get_forecast(states)
        assert data.tomorrow_total_kwh is None

    def test_returns_forecast_data_instance(self):
        provider = GenericForecastProvider("sensor.solar_forecast")
        states = {"sensor.solar_forecast": {"state": "3.0", "attributes": {}}}
        data = provider.get_forecast(states)
        assert isinstance(data, ForecastData)

    def test_entity_stored(self):
        provider = GenericForecastProvider("sensor.my_sensor")
        assert provider.forecast_entity == "sensor.my_sensor"


# ---------------------------------------------------------------------------
# TestSolcastProvider
# ---------------------------------------------------------------------------

class TestSolcastProvider:
    def test_reads_forecast_with_breakdown(self):
        """SolcastProvider reads remaining kWh and hourly breakdown."""
        provider = SolcastProvider("sensor.solcast_remaining")
        now = datetime(2026, 3, 22, 12, 0, 0, tzinfo=timezone.utc)
        states = {
            "sensor.solcast_remaining": {
                "state": "15.2",
                "attributes": {
                    "forecasts": [
                        {"period_start": now.isoformat(), "pv_estimate": 2.5},
                        {"period_start": now.replace(minute=30).isoformat(), "pv_estimate": 3.0},
                    ],
                    "forecast_tomorrow": 18.5,
                },
            },
        }
        data = provider.get_forecast(states)
        assert data.remaining_today_kwh == 15.2
        assert len(data.hourly_breakdown) >= 1
        assert data.tomorrow_total_kwh == 18.5

    def test_two_half_hour_slots_preserve_intervals(self):
        """Half-hour forecasts preserve their exact coverage."""
        provider = SolcastProvider("sensor.solcast_remaining")
        now = datetime(2026, 3, 22, 10, 0, 0, tzinfo=timezone.utc)
        states = {
            "sensor.solcast_remaining": {
                "state": "10.0",
                "attributes": {
                    "forecasts": [
                        {"period_start": now.isoformat(), "pv_estimate": 2.0},
                        {"period_start": now.replace(minute=30).isoformat(), "pv_estimate": 2.0},
                    ],
                    "forecast_tomorrow": None,
                },
            },
        }
        data = provider.get_forecast(states)
        # Both half-hour intervals remain independently usable.
        assert len(data.hourly_breakdown) == 2
        hf = data.hourly_breakdown[0]
        # Each 30-min slot at 2 kW (average) contributes 1 kWh -> total 2 kWh
        assert sum(h.expected_kwh for h in data.hourly_breakdown) == 2.0
        assert isinstance(hf, HourlyForecast)

    def test_no_forecasts_attribute_returns_empty_breakdown(self):
        provider = SolcastProvider("sensor.solcast_remaining")
        states = {
            "sensor.solcast_remaining": {
                "state": "5.0",
                "attributes": {},
            },
        }
        data = provider.get_forecast(states)
        assert data.remaining_today_kwh == 5.0
        assert data.hourly_breakdown == []

    def test_unavailable_raises_unavailable(self):
        provider = SolcastProvider("sensor.solcast_remaining")
        states = {"sensor.solcast_remaining": {"state": "unavailable", "attributes": {}}}
        with pytest.raises(ValueError, match="Forecast unavailable"):
            provider.get_forecast(states)

    def test_missing_sensor_raises_unavailable(self):
        provider = SolcastProvider("sensor.solcast_remaining")
        with pytest.raises(ValueError, match="Forecast unavailable"):
            provider.get_forecast({})

    def test_forecast_tomorrow_absent_gives_none(self):
        provider = SolcastProvider("sensor.solcast_remaining")
        states = {
            "sensor.solcast_remaining": {
                "state": "8.0",
                "attributes": {"forecasts": []},
            },
        }
        data = provider.get_forecast(states)
        assert data.tomorrow_total_kwh is None

    def test_hourly_forecast_fields(self):
        """HourlyForecast contains start, end, expected_kwh, expected_watts."""
        provider = SolcastProvider("sensor.solcast_remaining")
        now = datetime(2026, 3, 22, 14, 0, 0, tzinfo=timezone.utc)
        states = {
            "sensor.solcast_remaining": {
                "state": "6.0",
                "attributes": {
                    "forecasts": [
                        {"period_start": now.isoformat(), "pv_estimate": 3.0},
                        {"period_start": now.replace(minute=30).isoformat(), "pv_estimate": 3.0},
                    ],
                },
            },
        }
        data = provider.get_forecast(states)
        assert len(data.hourly_breakdown) == 2
        hf = data.hourly_breakdown[0]
        # Each interval keeps its actual half-hour duration
        assert hf.start.hour == 14
        assert hf.end.hour == 14 and hf.end.minute == 30
        # expected_watts is average kW * 1000
        assert pytest.approx(hf.expected_watts) == 3000.0

    def test_entity_stored(self):
        provider = SolcastProvider("sensor.solcast_remaining")
        assert provider.forecast_entity == "sensor.solcast_remaining"

    def test_period_start_as_datetime_objects(self):
        """HA stores period_start as datetime objects internally, not strings.

        This is the root cause of the planner producing 0 hourly_slots on prod:
        _parse_iso only accepted strings, causing TypeError on datetime objects.
        """
        from datetime import timedelta
        provider = SolcastProvider("sensor.solcast")
        tz = timezone(timedelta(hours=2))
        now = datetime(2026, 4, 5, 10, 0, tzinfo=tz)
        states = {
            "sensor.solcast": {
                "state": "30.0",
                "attributes": {
                    "detailedForecast": [
                        {"period_start": now, "pv_estimate": 2.0},
                        {"period_start": now.replace(minute=30), "pv_estimate": 3.0},
                        {"period_start": now.replace(hour=11), "pv_estimate": 4.0},
                        {"period_start": now.replace(hour=11, minute=30), "pv_estimate": 5.0},
                    ],
                },
            },
        }
        data = provider.get_forecast(states)
        assert data.remaining_today_kwh == 30.0
        assert len(data.hourly_breakdown) == 4
        # Hour 10: avg 2.5kW, each 30-min slot contributes kW*0.5
        assert pytest.approx(sum(h.expected_kwh for h in data.hourly_breakdown[:2])) == 2.0 * 0.5 + 3.0 * 0.5
        # Hour 11: avg 4.5kW
        assert pytest.approx(sum(h.expected_kwh for h in data.hourly_breakdown[2:])) == 4.0 * 0.5 + 5.0 * 0.5

    def test_detailedForecast_attribute_used(self):
        """Solcast HACS integration uses 'detailedForecast' not 'forecasts'."""
        provider = SolcastProvider("sensor.solcast")
        now = datetime(2026, 4, 5, 12, 0, 0, tzinfo=timezone.utc)
        states = {
            "sensor.solcast": {
                "state": "20.0",
                "attributes": {
                    "detailedForecast": [
                        {"period_start": now.isoformat(), "pv_estimate": 3.0},
                        {"period_start": now.replace(minute=30).isoformat(), "pv_estimate": 3.0},
                    ],
                },
            },
        }
        data = provider.get_forecast(states)
        assert len(data.hourly_breakdown) == 2
        assert data.hourly_breakdown[0].expected_watts == 3000.0

    def test_detailedHourly_attribute_fallback(self):
        """Falls back to detailedHourly if neither forecasts nor detailedForecast exist."""
        provider = SolcastProvider("sensor.solcast")
        now = datetime(2026, 4, 5, 14, 0, 0, tzinfo=timezone.utc)
        states = {
            "sensor.solcast": {
                "state": "10.0",
                "attributes": {
                    "detailedHourly": [
                        {"period_start": now.isoformat(), "pv_estimate": 2.0},
                    ],
                },
            },
        }
        data = provider.get_forecast(states)
        assert len(data.hourly_breakdown) == 1


# ---------------------------------------------------------------------------
# TestForecastSolarProvider
# ---------------------------------------------------------------------------

class TestForecastSolarProvider:
    def test_reads_watts_dict(self):
        """ForecastSolarProvider reads watts breakdown from attributes."""
        provider = ForecastSolarProvider("sensor.forecast_solar")
        now = datetime(2026, 3, 22, 12, 0, 0, tzinfo=timezone.utc)
        hour_str = now.replace(minute=0, second=0, microsecond=0).isoformat()
        states = {
            "sensor.forecast_solar": {
                "state": "10.0",
                "attributes": {
                    "watts": {
                        hour_str: 2500,
                    },
                },
            },
        }
        data = provider.get_forecast(states)
        assert data.remaining_today_kwh == 10.0
        assert len(data.hourly_breakdown) >= 1

    def test_watts_dict_converts_to_hourly_forecast(self):
        """Each watts entry becomes one HourlyForecast with correct kWh."""
        provider = ForecastSolarProvider("sensor.forecast_solar")
        now = datetime(2026, 3, 22, 10, 0, 0, tzinfo=timezone.utc)
        states = {
            "sensor.forecast_solar": {
                "state": "8.0",
                "attributes": {
                    "watts": {
                        now.isoformat(): 3000,
                    },
                },
            },
        }
        data = provider.get_forecast(states)
        assert len(data.hourly_breakdown) == 1
        hf = data.hourly_breakdown[0]
        assert hf.expected_watts == 3000.0
        assert pytest.approx(hf.expected_kwh) == 3.0  # 3000 W * 1 hour / 1000

    def test_multiple_hours(self):
        """Multiple watts entries produce multiple HourlyForecast entries."""
        provider = ForecastSolarProvider("sensor.forecast_solar")
        now = datetime(2026, 3, 22, 10, 0, 0, tzinfo=timezone.utc)
        hour2 = now.replace(hour=11)
        states = {
            "sensor.forecast_solar": {
                "state": "7.0",
                "attributes": {
                    "watts": {
                        now.isoformat(): 2000,
                        hour2.isoformat(): 5000,
                    },
                },
            },
        }
        data = provider.get_forecast(states)
        assert len(data.hourly_breakdown) == 2

    def test_no_watts_attribute_returns_empty_breakdown(self):
        provider = ForecastSolarProvider("sensor.forecast_solar")
        states = {
            "sensor.forecast_solar": {
                "state": "5.0",
                "attributes": {},
            },
        }
        data = provider.get_forecast(states)
        assert data.remaining_today_kwh == 5.0
        assert data.hourly_breakdown == []

    def test_unavailable_raises_unavailable(self):
        provider = ForecastSolarProvider("sensor.forecast_solar")
        states = {"sensor.forecast_solar": {"state": "unavailable", "attributes": {}}}
        with pytest.raises(ValueError, match="Forecast unavailable"):
            provider.get_forecast(states)

    def test_missing_sensor_raises_unavailable(self):
        provider = ForecastSolarProvider("sensor.forecast_solar")
        with pytest.raises(ValueError, match="Forecast unavailable"):
            provider.get_forecast({})

    def test_tomorrow_total_kwh_is_none(self):
        """ForecastSolarProvider does not provide tomorrow total."""
        provider = ForecastSolarProvider("sensor.forecast_solar")
        now = datetime(2026, 3, 22, 12, 0, 0, tzinfo=timezone.utc)
        states = {
            "sensor.forecast_solar": {
                "state": "4.0",
                "attributes": {"watts": {now.isoformat(): 1000}},
            },
        }
        data = provider.get_forecast(states)
        assert data.tomorrow_total_kwh is None

    def test_hourly_forecast_start_end(self):
        """HourlyForecast start and end span exactly one hour."""
        provider = ForecastSolarProvider("sensor.forecast_solar")
        now = datetime(2026, 3, 22, 9, 0, 0, tzinfo=timezone.utc)
        states = {
            "sensor.forecast_solar": {
                "state": "3.0",
                "attributes": {"watts": {now.isoformat(): 1500}},
            },
        }
        data = provider.get_forecast(states)
        hf = data.hourly_breakdown[0]
        assert hf.start == now
        assert hf.end == now.replace(hour=10)

    def test_entity_stored(self):
        provider = ForecastSolarProvider("sensor.forecast_solar")
        assert provider.forecast_entity == "sensor.forecast_solar"


# ---------------------------------------------------------------------------
# TestCreateForecastProvider
# ---------------------------------------------------------------------------

class TestCreateForecastProvider:
    def test_create_generic(self):
        provider = create_forecast_provider("generic", "sensor.solar")
        assert isinstance(provider, GenericForecastProvider)
        assert provider.forecast_entity == "sensor.solar"

    def test_create_solcast(self):
        provider = create_forecast_provider("solcast", "sensor.solcast")
        assert isinstance(provider, SolcastProvider)
        assert provider.forecast_entity == "sensor.solcast"

    def test_create_forecast_solar(self):
        provider = create_forecast_provider("forecast_solar", "sensor.forecast_solar")
        assert isinstance(provider, ForecastSolarProvider)
        assert provider.forecast_entity == "sensor.forecast_solar"

    def test_unknown_type_raises(self):
        with pytest.raises(ValueError, match="Unknown forecast provider"):
            create_forecast_provider("unknown_provider", "sensor.x")

    def test_none_type_returns_generic(self):
        """'none' provider type returns a GenericForecastProvider for safety."""
        provider = create_forecast_provider("none", "sensor.x")
        assert isinstance(provider, GenericForecastProvider)
