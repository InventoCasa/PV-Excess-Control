"""Hybrid sensor balance and forecast-independent Battery First regressions."""

from dataclasses import replace
from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from tests.test_init import _make_coordinator, _make_config_entry, MockState
from tests.test_optimizer import (
    _make_appliance,
    _make_state,
    _make_power,
    _empty_plan,
    _make_tariff,
)


def coordinator(combined=True, pv=3841, load=875, battery=2476, grid=490, soc=92):
    data = {
        "pv_power": "sensor.pv",
        "load_power": "sensor.load",
        "battery_power": "sensor.battery",
        "battery_soc": "sensor.soc",
        "inverter_type": "hybrid",
        "battery_strategy": "battery_first",
        "battery_target_soc": 100,
    }
    data["import_export_power" if combined else "grid_export"] = "sensor.grid"
    states = {
        "sensor.pv": MockState(str(pv)),
        "sensor.load": MockState(str(load)),
        "sensor.battery": MockState(str(battery)),
        "sensor.soc": MockState(str(soc)),
        "sensor.grid": MockState(str(grid if combined else max(grid, 0))),
    }
    return _make_coordinator(entry=_make_config_entry(data=data), states=states)


@pytest.mark.parametrize("combined", [True, False])
@pytest.mark.parametrize(
    "pv,load,battery,grid,expected",
    [
        (3841, 875, 2476, 490, 2966),
        (0, 1200, -1200, 0, -1200),
        (0, 1000, 2000, -3000, -1000),
        (4500, 0, 2000, 2500, 4500),
    ],
)
def test_equivalent_hybrid_topologies(combined, pv, load, battery, grid, expected):
    coord = coordinator(combined, pv, load, battery, grid)
    assert coord._collect_power_state().excess_power == expected


def test_combined_battery_outage_does_not_fall_back_to_grid_export():
    coord = coordinator(battery="unavailable")
    assert coord._collect_power_state().excess_power is None


def test_missing_configured_charge_half_stays_unavailable():
    coord = coordinator()
    coord.config_entry.data.pop("battery_power")
    coord.config_entry.data.update(
        battery_charge_power="sensor.charge", battery_discharge_power="sensor.discharge"
    )
    coord.hass.states._states.update(
        {"sensor.charge": MockState("unavailable"), "sensor.discharge": MockState("0")}
    )
    power = coord._collect_power_state()
    assert power.battery_power is None
    assert power.excess_power is None


def test_valid_zero_load_is_not_an_outage_or_a_different_topology():
    coord = coordinator(False, pv=4500, load=0, battery=2000, grid=2500)
    assert coord._collect_power_state().excess_power == 4500
    coord.hass.states._states["sensor.load"] = MockState("unavailable")
    assert coord._collect_power_state().excess_power is None


def test_battery_first_without_forecast_preserves_raw_values_and_protects_charging():
    coord = coordinator()
    power = coord._collect_power_state()
    coord.power_history = [power] * 3
    adjusted, history = coord._power_for_optimizer(power, [], {})
    assert power.excess_power == 2966
    assert adjusted.excess_power == 490
    assert all(p.excess_power == 490 for p in history)
    assert all(p.excess_power == 2966 for p in coord.power_history)
    assert coord._battery_charge_reserve_w == 2476
    result = coord.optimizer.optimize(
        adjusted,
        [_make_appliance(nominal_power=800)],
        [_make_state()],
        _empty_plan(),
        history,
        _make_tariff(),
    )
    assert result.decisions[0].action == "idle"


def test_battery_first_cap_leaves_real_excess_for_consumers():
    coord = coordinator(pv=9000, load=1000, battery=2500, grid=5500)
    coord.config_entry.data["battery_max_charge_power_w"] = 2500
    power = coord._collect_power_state()
    coord.power_history = [power] * 3
    adjusted, _ = coord._power_for_optimizer(power, [], {})
    assert adjusted.excess_power == 5500


def test_battery_first_cap_can_reclaim_power_from_running_loads():
    coord = coordinator(pv=2000, load=2000, battery=0, grid=0)
    coord.config_entry.data["battery_max_charge_power_w"] = 2500
    power = coord._collect_power_state()
    coord.power_history = [power] * 3
    app = _make_appliance(nominal_power=1000)
    state = _make_state(is_on=True, current_power=1000)
    adjusted, history = coord._power_for_optimizer(power, [app], {app.id: state})
    assert adjusted.excess_power == -1000
    result = coord.optimizer.optimize(
        adjusted, [app], [state], _empty_plan(), history, _make_tariff()
    )
    assert result.decisions[0].action == "off"


def test_target_reached_releases_reservation_and_old_soc_does_not_hold_it():
    coord = coordinator(soc=100)
    power = coord._collect_power_state()
    coord.power_history = [replace(power, battery_soc=80)] * 3
    adjusted, history = coord._power_for_optimizer(power, [], {})
    assert adjusted.excess_power == 2966
    assert all(p.excess_power == 2966 for p in history)


@pytest.mark.parametrize("strategy", ["balanced", "appliance_first"])
def test_other_battery_strategies_are_unchanged(strategy):
    coord = coordinator()
    coord.battery_strategy = strategy
    coord.config_entry.data["battery_strategy"] = strategy
    power = coord._collect_power_state()
    coord.power_history = [power] * 3
    adjusted, history = coord._power_for_optimizer(power, [], {})
    assert adjusted is power
    assert history == coord.power_history


@pytest.mark.parametrize("missing", ["soc", "cap"])
def test_unknown_required_battery_policy_sensor_prevents_using_stale_history(missing):
    coord = coordinator()
    power = coord._collect_power_state()
    coord.power_history = [power] * 3
    if missing == "soc":
        power = replace(power, battery_soc=None)
    else:
        coord.config_entry.data.update(
            battery_max_charge_power_w=2500,
            inverter_battery_max_charge_power_entity="number.missing",
        )
    adjusted, history = coord._power_for_optimizer(power, [], {})
    assert adjusted.excess_power == 0
    assert all(p.excess_power == 0 for p in history)


def test_actual_charge_limit_cap_is_respected():
    coord = coordinator()
    coord.config_entry.data.update(
        battery_max_charge_power_w=2500, inverter_battery_max_charge_power_entity="number.limit"
    )
    coord.hass.states._states["number.limit"] = MockState("100")
    power = coord._collect_power_state()
    coord.power_history = [power] * 3
    adjusted, _ = coord._power_for_optimizer(power, [], {})
    assert adjusted.excess_power == 2866


def test_missing_current_excess_never_uses_old_positive_samples():
    coord = coordinator()
    power = coord._collect_power_state()
    coord.power_history = [power] * 3
    adjusted, history = coord._power_for_optimizer(replace(power, excess_power=None), [], {})
    assert all(p.excess_power is None for p in history)


def test_excess_sensor_exposes_raw_and_available_budget_separately():
    from custom_components.pv_excess_control.sensor import PvExcessPowerSensor

    coord = coordinator()
    power = coord._collect_power_state()
    coord.power_history = [power] * 3
    coord._power_for_optimizer(power, [], {})
    coord.data = coord._build_coordinator_data()
    sensor = PvExcessPowerSensor(coord)
    assert sensor.native_value == 2966
    attrs = sensor.extra_state_attributes
    assert attrs["available_excess_power_w"] == 490
    assert attrs["battery_charge_reserve_w"] == 2476
    assert attrs["battery_priority_status"] == "measured_charging"


async def test_coordinator_applies_battery_budget_without_forecast():
    coord = coordinator()
    sub = MagicMock()
    sub.data = {
        "appliance_entity": "switch.app_1",
        "appliance_name": "Heater",
        "nominal_power": 800,
    }
    coord.config_entry.subentries = {"app_1": sub}
    coord.hass.states._states["switch.app_1"] = MockState("off")
    coord._startup_time -= timedelta(minutes=5)
    coord.power_history = [coord._collect_power_state()] * 3
    data = await coord._async_update_data()
    assert data["power_state"].excess_power == 2966
    assert data["available_excess_power_w"] == 490
    assert data["control_decisions"][0].action == "idle"
    assert not coord.hass.services.calls


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("standard", 1000),
        ("dynamic", 2300),
        ("disconnected", 0),
        ("measured_zero", 0),
        ("missing", 0),
    ],
)
def test_coordinator_distinguishes_unmetered_idle_and_unavailable_power(kind, expected):
    coord = coordinator()
    app = _make_appliance(nominal_power=1000)
    coord.hass.states._states["switch.app_1"] = MockState("on")
    if kind in ("dynamic", "disconnected"):
        app.dynamic_current = True
        app.current_entity = "number.amps"
        app.ev_connected_entity = "binary_sensor.connected"
        coord.hass.states._states["number.amps"] = MockState("10")
        coord.hass.states._states["binary_sensor.connected"] = MockState(
            "off" if kind == "disconnected" else "on"
        )
    elif kind in ("measured_zero", "missing"):
        app.actual_power_entity = "sensor.device_power"
        if kind == "measured_zero":
            coord.hass.states._states["sensor.device_power"] = MockState("0")
    states = coord._get_appliance_states([app])
    assert states[app.id].current_power == expected
    assert states[app.id].current_power_available is (kind != "missing")
    states = coord._get_appliance_states([app])
    assert states[app.id].energy_today == pytest.approx(expected * 30 / 3600000)


def test_unavailable_appliance_power_sensor_does_not_display_a_real_zero():
    from custom_components.pv_excess_control.sensor import PvAppliancePowerSensor

    coord = coordinator()
    state = _make_state(is_on=True)
    state.current_power_available = False
    coord.data = {"appliance_states": {"app_1": state}}
    assert PvAppliancePowerSensor(coord, "app_1", "Heater").native_value is None


@pytest.mark.parametrize("missing", ["soc", "cap", "charging"])
def test_unknown_policy_reading_does_not_hide_a_measured_deficit(missing):
    coord = coordinator(False, pv=0, load=1000, battery=0, grid=0)
    power = coord._collect_power_state()
    coord.power_history = [_make_power(3000)] * 3
    if missing == "soc":
        power = replace(power, battery_soc=None)
    elif missing == "cap":
        coord.config_entry.data.update(
            battery_max_charge_power_w=2500,
            inverter_battery_max_charge_power_entity="number.missing",
        )
    else:
        power = replace(power, battery_power=None)
    adjusted, history = coord._power_for_optimizer(power, [], {})
    assert adjusted.excess_power == -1000
    assert all(p.excess_power is not None and p.excess_power <= 0 for p in history)
    result = coord.optimizer.optimize(
        adjusted,
        [_make_appliance()],
        [_make_state(is_on=True, current_power=1000)],
        _empty_plan(),
        history,
        _make_tariff(),
    )
    assert result.decisions[0].action == "off"


async def test_idle_or_unavailable_on_load_does_not_record_phantom_analytics():
    coord = coordinator()
    coord.battery_strategy = "appliance_first"
    coord.config_entry.data["battery_strategy"] = "appliance_first"
    sub = MagicMock()
    sub.data = {
        "appliance_entity": "switch.app_1",
        "appliance_name": "Heater",
        "nominal_power": 1000,
        "actual_power_entity": "sensor.device_power",
    }
    coord.config_entry.subentries = {"app_1": sub}
    coord.hass.states._states["switch.app_1"] = MockState("on")
    coord._startup_time -= timedelta(minutes=5)
    coord.power_history = [coord._collect_power_state()] * 3
    for reading in ["0", "unavailable"]:
        coord.hass.states._states["sensor.device_power"] = MockState(reading)
        await coord._async_update_data()
    assert coord.analytics.get_appliance_stats("app_1").energy_today_kwh == 0
    assert coord.analytics.savings_today == 0


@pytest.mark.parametrize("unit,expected", [("W", 2866), ("kW", 1966), ("A", 0)])
def test_live_charge_limit_uses_power_units_not_current(unit, expected):
    coord = coordinator()
    coord.config_entry.data.update(
        battery_max_charge_power_w=2500, inverter_battery_max_charge_power_entity="number.limit"
    )
    value = "1" if unit == "kW" else "100"
    coord.hass.states._states["number.limit"] = MockState(value, {"unit_of_measurement": unit})
    power = coord._collect_power_state()
    coord.power_history = [power] * 3
    adjusted, _ = coord._power_for_optimizer(power, [], {})
    assert adjusted.excess_power == expected
