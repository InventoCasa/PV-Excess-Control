"""External price forecasts must remain explicit, bounded and observation-only."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)


def payload():
    return {
        "schema_version": 1,
        "market": "DE-LU",
        "unit": "EUR/kWh",
        "price_basis": "wholesale",
        "generated_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(hours=6)).isoformat(),
        "model": "calendar-v1",
        "quality": {"status": "experimental", "automation_eligible": False},
        "intervals": [{
            "start": NOW.isoformat(),
            "end": (NOW + timedelta(minutes=15)).isoformat(),
            "price": 0.0,
            "lower": -0.03,
            "upper": 0.04,
        }],
    }


def parser():
    # Attribute assertion makes the first red run report the missing feature.
    package = import_module("custom_components.pv_excess_control")
    assert (module_path := Path(package.__file__).parent / "price_forecast.py").exists(), str(module_path)
    return import_module("custom_components.pv_excess_control.price_forecast")


def test_zero_and_negative_prices_are_preserved_and_input_is_not_mutated():
    source = payload()
    expected = deepcopy(source)
    snapshot = parser().parse_price_forecast(source, now=NOW)
    assert snapshot.as_dict() == expected
    assert source == expected
    source["intervals"][0]["price"] = 100
    assert snapshot.as_dict()["intervals"][0]["price"] == 0
    expected["intervals"][0]["price"] = -0.01
    assert parser().parse_price_forecast(expected, now=NOW).intervals[0].price == -0.01


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("schema_version", 1.0), ("schema_version", 3),
    ("unit", "ct/kWh"), ("market", "FR"), ("price_basis", "unknown"),
    ("model", ""), ("generated_at", "2026-11-01T12:00:00"),
    ("generated_at", (NOW + timedelta(seconds=1)).isoformat()),
    ("expires_at", NOW.isoformat()),
    ("expires_at", (NOW + timedelta(hours=7)).isoformat()),
    ("quality", {"status": "validated", "automation_eligible": True}),
    ("quality", {"status": "experimental", "automation_eligible": 0}),
    ("intervals", []),
])
def test_invalid_envelope_rejected(field, value):
    source = payload()
    source[field] = value
    with pytest.raises(ValueError):
        parser().parse_price_forecast(source, now=NOW)


@pytest.mark.parametrize("field,value", [
    ("price", None), ("price", True), ("price", "0.01"), ("price", float("nan")),
    ("lower", float("-inf")), ("upper", float("inf")),
    ("lower", 0.01), ("upper", -0.01),
    ("end", (NOW + timedelta(minutes=5)).isoformat()),
    ("start", "2026-11-01T12:00:00"),
])
def test_invalid_interval_rejected(field, value):
    source = payload()
    source["intervals"][0][field] = value
    with pytest.raises(ValueError):
        parser().parse_price_forecast(source, now=NOW)


@pytest.mark.parametrize("offset", [0, 10, 30])
def test_duplicate_overlap_and_gap_rejected(offset):
    source = payload()
    second = dict(source["intervals"][0])
    second["start"] = (NOW + timedelta(minutes=offset)).isoformat()
    second["end"] = (NOW + timedelta(minutes=offset + 15)).isoformat()
    source["intervals"].append(second)
    with pytest.raises(ValueError):
        parser().parse_price_forecast(source, now=NOW)


def test_horizon_and_interval_count_are_bounded():
    source = payload()
    source["intervals"][0]["start"] = (NOW + timedelta(days=7)).isoformat()
    source["intervals"][0]["end"] = (NOW + timedelta(days=7, minutes=15)).isoformat()
    with pytest.raises(ValueError):
        parser().parse_price_forecast(source, now=NOW)
    source = payload()
    source["intervals"] *= 673
    with pytest.raises(ValueError):
        parser().parse_price_forecast(source, now=NOW)


def test_expired_data_and_exhausted_horizon_are_distinct():
    module = parser()
    with pytest.raises(module.PriceForecastError, match="expired"):
        module.parse_price_forecast(payload(), now=NOW + timedelta(hours=6))
    with pytest.raises(module.PriceForecastError, match="no_future_intervals"):
        module.parse_price_forecast(payload(), now=NOW + timedelta(minutes=15))


def test_dst_offsets_are_compared_as_elapsed_time():
    source = payload()
    source["generated_at"] = "2026-10-25T00:00:00+00:00"
    source["expires_at"] = "2026-10-25T06:00:00+00:00"
    source["intervals"] = [
        {"start": "2026-10-25T02:00:00+02:00", "end": "2026-10-25T02:00:00+01:00", "price": 0, "lower": -0.1, "upper": 0.1},
        {"start": "2026-10-25T02:00:00+01:00", "end": "2026-10-25T03:00:00+01:00", "price": 0.2, "lower": 0.1, "upper": 0.3},
    ]
    snapshot = parser().parse_price_forecast(source, now=datetime(2026, 10, 25, tzinfo=timezone.utc))
    assert snapshot.intervals[-1].end - snapshot.intervals[0].start == timedelta(hours=2)


def test_diagnostics_do_not_change_confirmed_tariffs_or_control(coordinator_factory, mock_tariff_at):
    coordinator = coordinator_factory(config_data={"price_forecast_sensor": "sensor.forecast"})
    source = payload()
    state = SimpleNamespace(state="experimental", attributes=source)
    coordinator.hass.states.get.return_value = state
    tariff = mock_tariff_at(0.5)
    coordinator._last_tariff_info = tariff
    original_data = dict(coordinator.config_entry.data)
    assert hasattr(coordinator, "_read_price_forecast")
    result = coordinator._read_price_forecast(now=NOW)
    assert result["status"] == "available"
    assert result["snapshot"]["intervals"][0]["price"] == 0
    assert result["automation_eligible"] is False
    assert coordinator._last_tariff_info is tariff
    assert tariff.current_price == 0.5
    assert coordinator._grid_charge_engaged is False
    assert coordinator.config_entry.data == original_data
    coordinator.hass.services.async_call.assert_not_called()


@pytest.mark.parametrize("state,expected", [(None, "missing_sensor"), ("unavailable", "unavailable"), ("unknown", "unavailable")])
def test_unavailable_state_does_not_expose_old_predictions(coordinator_factory, state, expected):
    coordinator = coordinator_factory(config_data={"price_forecast_sensor": "sensor.forecast"})
    coordinator.hass.states.get.return_value = None if state is None else SimpleNamespace(state=state, attributes=payload())
    assert hasattr(coordinator, "_read_price_forecast")
    result = coordinator._read_price_forecast(now=NOW)
    assert result["status"] == expected
    assert result["snapshot"] is None


def test_absent_configuration_does_not_read_sensor(coordinator_factory):
    coordinator = coordinator_factory()
    assert hasattr(coordinator, "_read_price_forecast")
    assert coordinator._read_price_forecast(now=NOW)["status"] == "not_configured"
    coordinator.hass.states.get.assert_not_called()


@pytest.mark.parametrize("options", [False, True])
async def test_energy_flow_accepts_and_clears_optional_input(options):
    if options:
        from tests.test_options_flow import _make_options_flow
        flow = _make_options_flow()
    else:
        from tests.test_config_flow import _make_flow
        flow = _make_flow()
    from custom_components.pv_excess_control.config_flow import _energy_schema
    schema = _energy_schema("none")
    assert "price_forecast_sensor" in {str(key.schema) for key in schema.schema}
    await flow.async_step_energy({"tariff_provider": "none", "price_forecast_sensor": "sensor.forecast"})
    assert flow.data["price_forecast_sensor"] == "sensor.forecast"
    await flow.async_step_energy({"tariff_provider": "none"})
    assert "price_forecast_sensor" not in flow.data


def test_diagnostic_sensor_excludes_large_curve_from_recorder():
    sensors = import_module("custom_components.pv_excess_control.sensor")
    assert hasattr(sensors, "PvPriceForecastSensor")
    coordinator = MagicMock()
    coordinator.data = {"price_forecast": {"status": "available", "source_entity": "sensor.forecast", "automation_eligible": False, "snapshot": payload()}}
    sensor = sensors.PvPriceForecastSensor(coordinator)
    assert sensor.native_value == "available"
    assert sensor.extra_state_attributes["intervals"] == payload()["intervals"]
    assert "intervals" in sensor._unrecorded_attributes
    coordinator.data["price_forecast"] = {"status": "expired", "source_entity": "sensor.forecast", "automation_eligible": False, "snapshot": None}
    assert sensor.native_value == "expired"
    assert "intervals" not in sensor.extra_state_attributes


def test_updated_source_and_expiry_are_reflected_without_cached_snapshot(coordinator_factory, monkeypatch):
    coordinator = coordinator_factory(config_data={"price_forecast_sensor": "sensor.first"})
    coordinator.hass.states.get.return_value = SimpleNamespace(state="experimental", attributes=payload())
    monkeypatch.setattr("custom_components.pv_excess_control.coordinator.dt_util.utcnow", lambda: NOW)
    data = coordinator._build_coordinator_data()
    assert data["price_forecast"]["status"] == "available"
    coordinator.config_entry.data["price_forecast_sensor"] = "sensor.second"
    monkeypatch.setattr("custom_components.pv_excess_control.coordinator.dt_util.utcnow", lambda: NOW + timedelta(hours=6))
    data = coordinator._build_coordinator_data()
    assert data["price_forecast"]["source_entity"] == "sensor.second"
    assert data["price_forecast"]["status"] == "expired"
    assert data["price_forecast"]["snapshot"] is None
    coordinator.config_entry.data.pop("price_forecast_sensor")
    assert coordinator._build_coordinator_data()["price_forecast"]["status"] == "not_configured"


@pytest.mark.parametrize("attributes", [None, [], {}, {"schema_version": 1}])
def test_malformed_input_is_contained_in_diagnostics(coordinator_factory, attributes):
    coordinator = coordinator_factory(config_data={"price_forecast_sensor": "sensor.forecast"})
    coordinator.hass.states.get.return_value = SimpleNamespace(state="experimental", attributes=attributes)
    data = coordinator._build_coordinator_data()["price_forecast"]
    assert data["status"] != "available"
    assert data["snapshot"] is None
    coordinator.hass.services.async_call.assert_not_called()


async def test_sensor_registered_only_for_configured_input():
    from custom_components.pv_excess_control.const import DOMAIN
    from custom_components.pv_excess_control.sensor import PvPriceForecastSensor, async_setup_entry
    entry = SimpleNamespace(entry_id="entry", data={"price_forecast_sensor": "sensor.forecast"}, subentries={})
    coordinator = MagicMock(config_entry=entry)
    hass = SimpleNamespace(data={DOMAIN: {entry.entry_id: coordinator}})
    entities = []
    await async_setup_entry(hass, entry, lambda added, **kwargs: entities.extend(added))
    assert len([entity for entity in entities if isinstance(entity, PvPriceForecastSensor)]) == 1
    assert entities[-1].unique_id == "entry_price_forecast"
    entry.data.clear()
    entities.clear()
    await async_setup_entry(hass, entry, lambda added, **kwargs: entities.extend(added))
    assert not any(isinstance(entity, PvPriceForecastSensor) for entity in entities)
