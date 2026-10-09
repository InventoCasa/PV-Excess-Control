"""Grid charging can conservatively calibrate PV without changing solar planning."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

import pytest

from custom_components.pv_excess_control.battery_planner import build_battery_grid_plan
from tests.test_battery_control_economics import economic_case  # noqa: F401


@pytest.mark.parametrize("factor", [.1, .75, .9, 1.])
async def test_reduced_pv_buys_only_additional_useful_energy(economic_case, factor):
    case = economic_case([.1, .3, .4], [0, 1, 0], [0, 0, 2000],
                         battery_pv_forecast_factor=factor)
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.coord._battery_grid_plan.grid_energy_kwh == pytest.approx(2 / .85 - factor)
        assert case.controller.engage.await_args.args[0] == pytest.approx((2 / .85 - factor) * 1000)
    finally:
        await case.coord.async_stop_battery_controls()


async def test_missing_factor_preserves_existing_grid_plan(economic_case):
    default = economic_case([.1, .3, .4], [0, 1, 0], [0, 0, 2000])
    explicit = economic_case([.1, .3, .4], [0, 1, 0], [0, 0, 2000],
                             battery_pv_forecast_factor=1.)
    assert default.coord._refresh_battery_grid_plan(default.tariff, None) is None
    assert explicit.coord._refresh_battery_grid_plan(explicit.tariff, None) is None
    assert default.coord._battery_grid_plan == explicit.coord._battery_grid_plan


@pytest.mark.parametrize("factor", [None, True, False, "bad", float("nan"),
                                    float("inf"), -float("inf"), -1, 0, .09, 1.01])
async def test_invalid_factor_blocks_automatic_charge(economic_case, factor):
    case = economic_case([.1, .4], [0, 0], [0, 2000],
                         battery_pv_forecast_factor=factor)
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.coord._battery_control_reason == "invalid_pv_forecast_factor"
        assert case.coord._battery_grid_plan is None
        case.controller.engage.assert_not_awaited()
    finally:
        await case.coord.async_stop_battery_controls()


async def test_invalid_factor_stops_owned_charge_and_discards_cached_plan(economic_case):
    case = economic_case([.1, .4], [0, 0], [0, 2000])
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.coord._grid_charge_engaged
        case.coord.config_entry.data["battery_pv_forecast_factor"] = float("nan")
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.coord._battery_control_reason == "invalid_pv_forecast_factor"
        assert case.coord._battery_grid_plan is None
        assert not case.coord._grid_charge_engaged
        case.controller.disengage.assert_awaited_once()
    finally:
        await case.coord.async_stop_battery_controls()


async def test_factor_copies_energy_and_power_without_changing_shared_forecast_or_demand(economic_case):
    case = economic_case([.1, .3, .4], [0, 1, 0], [0, 0, 2000],
                         battery_pv_forecast_factor=.9)
    forecast = case.coord._forecast_data
    original = deepcopy(forecast)
    profile = case.coord.config_entry.data["battery_load_profile_w"]
    profile_before = profile.copy()
    with patch("custom_components.pv_excess_control.battery_planner.build_battery_grid_plan",
               wraps=build_battery_grid_plan) as planner:
        assert case.coord._refresh_battery_grid_plan(case.tariff, None) is None
    scaled = planner.call_args.args[3]
    assert scaled is not forecast.hourly_breakdown
    for source, result in zip(forecast.hourly_breakdown, scaled):
        assert result is not source
        assert (result.start, result.end) == (source.start, source.end)
        assert result.expected_kwh == pytest.approx(source.expected_kwh * .9)
        assert result.expected_watts == pytest.approx(source.expected_watts * .9)
    assert case.coord._forecast_data is forecast
    assert forecast == original
    assert profile == profile_before
    assert case.coord.config_entry.data["battery_target_soc"] == 100


async def test_factor_change_replans_without_compounding_shared_forecast(economic_case):
    case = economic_case([.1, .3, .4], [0, 1, 0], [0, 0, 2000],
                         battery_pv_forecast_factor=.9)
    assert case.coord._refresh_battery_grid_plan(case.tariff, None) is None
    original_plan = case.coord._battery_grid_plan
    case.coord.config_entry.data["battery_pv_forecast_factor"] = .75
    assert case.coord._refresh_battery_grid_plan(case.tariff, None) is None
    assert case.coord._battery_grid_plan is not original_plan
    assert case.coord._battery_grid_plan.grid_energy_kwh == pytest.approx(2 / .85 - .75)
    assert case.coord._forecast_data.hourly_breakdown[1].expected_kwh == 1


async def test_scaled_forecast_preserves_calendar_coverage_across_midnight(economic_case):
    case = economic_case([.1, .3, .4], [0, 1, 0], [0, 0, 2000],
                         battery_pv_forecast_factor=.9)
    offset = timedelta(hours=23)
    case.clock["now"] += offset
    case.tariff.windows = [replace(row, start=row.start + offset, end=row.end + offset)
                           for row in case.tariff.windows]
    case.coord._forecast_data.hourly_breakdown = [
        replace(row, start=row.start + offset, end=row.end + offset)
        for row in case.coord._forecast_data.hourly_breakdown
    ]
    case.coord.config_entry.data["battery_load_profile_w"] = [0, 2000] + [0] * 22
    for entity, state in list(case.states.items()):
        case.reading(entity, state.state, **state.attributes)
    assert case.coord._refresh_battery_grid_plan(case.tariff, None) is None
    plan = case.coord._battery_grid_plan
    assert plan.grid_energy_kwh == pytest.approx(2 / .85 - .9)
    assert [(slot.start, slot.end) for slot in plan.slots] == [
        (row.start, row.end) for row in case.tariff.windows
    ]
