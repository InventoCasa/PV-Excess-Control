"""Economic forecasts remain coherent through coordinator state transitions."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import State
from homeassistant.util import dt as dt_util

from custom_components.pv_excess_control.models import (
    ForecastData, HourlyForecast, TariffInfo, TariffWindow,
)


@pytest.fixture
def economic_case(coordinator_factory, mock_inverter_controller, monkeypatch):
    clock = {"now": datetime(2026, 10, 9, tzinfo=timezone.utc)}
    monkeypatch.setattr(dt_util, "now", lambda: clock["now"])
    monkeypatch.setattr(dt_util, "utcnow", lambda: clock["now"].astimezone(timezone.utc))

    def build(prices, solar, demand, **config):
        coord = coordinator_factory(config_data={
            "auto_battery_grid_charge": True, "allow_grid_charging": True,
            "battery_soc": "sensor.soc", "battery_capacity": 10,
            "battery_grid_charge_power_w": 2500, "battery_grid_target_soc": 80,
            "battery_max_charge_power_w": 2500,
            "battery_target_soc": 100, "price_sensor": "sensor.price",
            "battery_load_profile_w": demand + [0.] * (24 - len(demand)),
            "inverter_force_charge_enable_entity": "switch.charge",
            "inverter_force_charge_enable_engage_value": "on",
            "inverter_force_charge_enable_disengage_value": "off",
            "inverter_force_charge_power_entity": "number.charge_power", **config,
        }, inverter_ctl=mock_inverter_controller)
        states = {}

        def reading(entity, value, **attributes):
            stamp = clock["now"].astimezone(timezone.utc)
            states[entity] = State(entity, str(value), attributes, last_updated=stamp, last_reported=stamp)

        reading("sensor.soc", 10)
        reading("sensor.load_power", demand[0], unit_of_measurement="W")
        reading("sensor.pv_power", solar[0] * 1000, unit_of_measurement="W")
        reading("sensor.price", prices[0])
        reading("sensor.forecast", sum(solar))
        reading("number.charge_power", 2500, min=0, max=5000, step=.001, unit_of_measurement="W")
        coord.hass.states.get.side_effect = states.get
        mock_inverter_controller.confirmation_level = "physical"
        mock_inverter_controller.verify_engaged = AsyncMock()
        mock_inverter_controller.verify_disengaged = AsyncMock()
        coord._forecast_entities = ["sensor.forecast"]
        coord._forecast_tomorrow_entities = []
        intervals = [(clock["now"] + timedelta(hours=i), clock["now"] + timedelta(hours=i + 1)) for i in range(len(prices))]
        coord._forecast_data = ForecastData(sum(solar), [
            HourlyForecast(start, end, pv, pv * 1000) for (start, end), pv in zip(intervals, solar)
        ])
        tariff = TariffInfo(prices[0], 0, .2, .2, [
            TariffWindow(start, end, price, price <= .2) for (start, end), price in zip(intervals, prices)
        ])
        return SimpleNamespace(coord=coord, states=states, tariff=tariff, controller=mock_inverter_controller, clock=clock, reading=reading)

    return build


async def test_charge_then_verified_hold_then_native_expensive_hour(economic_case):
    case = economic_case([.2, .22, .4], [0, 0, 0], [0, 3000, 2000])
    coord = case.coord
    hold = MagicMock(confirmation_level="physical")
    hold.engage = AsyncMock()
    hold.disengage = AsyncMock()
    hold.verify_engaged = AsyncMock()
    coord._battery_hold_ctl = hold
    try:
        await coord._run_grid_charge_state_machine(case.tariff, None)
        assert coord._battery_control_state == "charging"
        charged_soc = coord._current_battery_slot().soc_end
        assert charged_soc < 40
        case.clock["now"] += timedelta(hours=1)
        for entity, value in (("sensor.soc", charged_soc), ("sensor.load_power", 3000), ("sensor.pv_power", 0), ("sensor.price", .22)):
            case.reading(entity, value)
        case.tariff.current_price = .22
        await coord._run_grid_charge_state_machine(case.tariff, None)
        assert coord._battery_control_state == "holding"
        case.controller.disengage.assert_awaited_once()
        hold.engage.assert_awaited_once_with(0)
        assert not coord._grid_charge_engaged
        case.clock["now"] += timedelta(hours=1)
        for entity, value in (("sensor.soc", charged_soc), ("sensor.load_power", 2000), ("sensor.pv_power", 0), ("sensor.price", .4)):
            case.reading(entity, value)
        case.tariff.current_price = .4
        await coord._run_grid_charge_state_machine(case.tariff, None)
        assert coord._battery_control_state == "self_consumption"
        hold.disengage.assert_awaited_once()
        assert not coord._battery_hold_engaged
    finally:
        await coord.async_stop_battery_controls()


async def test_grid_ceiling_stops_at_30_while_solar_target_stays_100(economic_case):
    case = economic_case([.1, .4], [0, 0], [0, 2500], battery_grid_target_soc=30)
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.coord._grid_charge_engaged
        assert case.coord._current_battery_slot().soc_end == pytest.approx(30)
        case.reading("sensor.soc", 30)
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert not case.coord._grid_charge_engaged
        assert case.coord.config_entry.data["battery_target_soc"] == 100
        case.controller.disengage.assert_awaited_once()
    finally:
        await case.coord.async_stop_battery_controls()


@pytest.mark.parametrize(("solar", "expected_grid"), [([0, 1, 0], (2 - .85) / .85), ([0, 2.5, 0], 0)])
async def test_sunny_and_winter_forecasts_change_actual_grid_command(economic_case, solar, expected_grid):
    case = economic_case([.1, .3, .4], solar, [0, 0, 2000])
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.coord._battery_grid_plan.grid_energy_kwh == pytest.approx(expected_grid)
        if expected_grid:
            assert case.coord._grid_charge_engaged
            assert case.controller.engage.await_args.args[0] == pytest.approx(expected_grid * 1000)
        else:
            case.controller.engage.assert_not_awaited()
    finally:
        await case.coord.async_stop_battery_controls()


async def test_forced_power_uses_pv_plus_grid_forecast_contributions(economic_case):
    case = economic_case([.1, .4], [1, 0], [0, 2000])
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        slot = case.coord._current_battery_slot()
        assert slot.pv_charge_kwh == pytest.approx(1)
        assert slot.grid_charge_kwh == pytest.approx(2 / .85 - 1)
        assert case.controller.engage.await_args.args[0] == pytest.approx(2000 / .85)
    finally:
        await case.coord.async_stop_battery_controls()


async def test_faster_native_solar_charging_avoids_unnecessary_night_purchase(economic_case):
    case = economic_case([.1, .3, .4], [0, 5, 0], [0, 0, 4000],
                         battery_max_charge_power_w=5000, battery_max_discharge_default=5000)
    await case.coord._run_grid_charge_state_machine(case.tariff, None)
    try:
        assert case.coord._battery_grid_plan.grid_energy_kwh == 0
        assert case.coord._battery_grid_plan.slots[1].pv_charge_kwh == pytest.approx(5)
        case.controller.engage.assert_not_awaited()
    finally:
        await case.coord.async_stop_battery_controls()


async def test_total_command_absorbs_pv_without_exceeding_additional_grid_limit(economic_case):
    case = economic_case([.1, .4], [1.5, 0], [0, 3000],
                         battery_max_charge_power_w=5000, battery_max_discharge_default=4000)
    await case.coord._run_grid_charge_state_machine(case.tariff, None)
    try:
        slot = case.coord._current_battery_slot()
        assert slot.grid_charge_kwh == pytest.approx(3 / .85 - 1.5)
        assert case.controller.engage.await_args.args[0] == pytest.approx(3000 / .85)
    finally:
        await case.coord.async_stop_battery_controls()


async def test_clouds_cannot_turn_forecast_solar_into_extra_grid_purchase(economic_case):
    case = economic_case([.1, .4], [1.5, 0], [0, 3000],
                         battery_max_charge_power_w=5000, battery_max_discharge_default=4000)
    case.reading("sensor.pv_power", 0, unit_of_measurement="W")
    await case.coord._run_grid_charge_state_machine(case.tariff, None)
    try:
        assert case.coord._current_battery_slot().battery_charge_power_w > 2500
        assert case.controller.engage.await_args.args[0] == pytest.approx(2500)
    finally:
        await case.coord.async_stop_battery_controls()


async def test_forecast_gap_clears_an_existing_economic_plan(economic_case):
    case = economic_case([.1, .4, .4], [0, 0, 0], [0, 2000, 2000])
    await case.coord._run_grid_charge_state_machine(case.tariff, None)
    assert case.coord._grid_charge_engaged
    del case.coord._forecast_data.hourly_breakdown[1]
    await case.coord._run_grid_charge_state_machine(case.tariff, None)
    assert not case.coord._grid_charge_engaged
    assert case.coord._battery_grid_plan is None
    case.controller.disengage.assert_awaited_once()


async def test_repeated_measurements_keep_slot_target_stable(economic_case):
    case = economic_case([.1, .4, .4], [0, 0, 0], [0, 2000, 2000])
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        initial_plan = case.coord._battery_grid_plan
        target = case.coord._current_battery_slot().soc_end
        case.clock["now"] += timedelta(seconds=30)
        case.reading("sensor.soc", 10.2)
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.coord._battery_grid_plan is initial_plan
        assert case.coord._current_battery_slot().soc_end == target
        case.controller.engage.assert_awaited_once()
        case.controller.verify_engaged.assert_awaited_once()
    finally:
        await case.coord.async_stop_battery_controls()


async def test_learned_hourly_profile_survives_save_reload_and_midnight(economic_case):
    case = economic_case([.1, .4], [0, 0], [0, 1000], battery_load_profile_w=None)
    rows = [[500. + hour, 1800., case.clock["now"].isoformat()] for hour in range(24)]
    case.coord._battery_load_hours = rows
    store = MagicMock()
    store.async_save = AsyncMock()
    case.coord._battery_load_store = store
    await case.coord.async_save_battery_load()
    saved = deepcopy(store.async_save.await_args.args[0])
    reloaded = economic_case([.1, .4], [0, 0], [0, 1000], battery_load_profile_w=None)
    reloaded.coord._battery_journal.async_load.return_value = (None, None)
    store.async_load = AsyncMock(return_value=saved)
    with patch("custom_components.pv_excess_control.battery_control.Store", return_value=store):
        await reloaded.coord.async_restore_battery_load()
    assert reloaded.coord._battery_load_profile() == [500. + hour for hour in range(24)]
    reloaded.coord.reset_daily()
    assert reloaded.coord._battery_load_profile() == [500. + hour for hour in range(24)]


async def test_missing_hour_after_spring_dst_never_becomes_zero_demand(economic_case):
    case = economic_case([.1, .4], [0, 0], [0, 1000], battery_load_profile_w=None)
    case.clock["now"] = datetime(2026, 3, 29, 4, tzinfo=ZoneInfo("Europe/Berlin"))
    case.coord._battery_load_hours = [[500., 1800., case.clock["now"].isoformat()] for _ in range(24)]
    case.coord._battery_load_hours[2] = [0., 0., None]
    assert case.coord._battery_load_profile() is None


async def test_repeated_fall_dst_hour_uses_elapsed_seconds_for_learning(economic_case):
    case = economic_case([.1, .4], [0, 0], [0, 1000], battery_load_profile_w=None)
    zone = ZoneInfo("Europe/Berlin")
    before = datetime(2026, 10, 25, 2, 59, 30, tzinfo=zone, fold=0)
    case.clock["now"] = datetime(2026, 10, 25, 2, 0, 30, tzinfo=zone, fold=1)
    case.coord._battery_load_last = (before, 1000.)
    case.reading("sensor.load_power", 1000, unit_of_measurement="W")
    case.coord._observe_battery_load()
    assert case.coord._battery_load_hours[2][0] == 1000
    assert case.coord._battery_load_hours[2][1] == 60


async def test_pv_drop_during_cap_release_cannot_raise_grid_charge_power(economic_case):
    case = economic_case([.1, .4], [1.5, 0], [0, 3000],
        battery_max_charge_power_w=5000, dynamic_battery_charge_enabled=True,
        inverter_battery_max_charge_power_entity="number.solar_cap")
    async def release(_):
        case.reading("sensor.pv_power", 0, unit_of_measurement="W")
        return True
    case.coord._write_battery_max_charge = AsyncMock(side_effect=release)
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.coord._current_battery_slot().battery_charge_power_w > 2500
        assert case.controller.engage.await_args.args[0] <= 2500
    finally:
        await case.coord.async_stop_battery_controls()
