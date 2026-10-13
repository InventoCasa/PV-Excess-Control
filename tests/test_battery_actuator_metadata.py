"""The battery planner receives the same numeric limits used for dispatch."""
from unittest.mock import patch

import pytest

from custom_components.pv_excess_control.battery_planner import build_battery_grid_plan
from tests.test_battery_control_economics import economic_case  # noqa: F401


def captured_constraints(case):
    with patch(
        "custom_components.pv_excess_control.battery_planner.build_battery_grid_plan",
        wraps=build_battery_grid_plan,
    ) as build:
        assert case.coord._refresh_battery_grid_plan(case.tariff, None) is None
    config = build.call_args.args[5]
    return tuple(getattr(config, key, None) for key in (
        "forced_charge_min_power_w", "forced_charge_max_power_w",
        "forced_charge_power_step_w",
    ))


@pytest.mark.parametrize("unit, minimum, maximum, step", [
    ("W", 55, 5055, 100), ("kW", .055, 5.055, .1),
    (None, 55, 5055, 100), ("", 55, 5055, 100),
])
async def test_planner_and_dispatch_share_watt_constraints(economic_case, unit, minimum, maximum, step):
    case = economic_case([.1, .4], [0, 0], [0, 2000])
    case.reading("number.charge_power", 1, min=minimum, max=maximum, step=step,
                 unit_of_measurement=unit)
    assert captured_constraints(case) == pytest.approx((55, 5055, 100))
    assert case.coord._feasible_battery_power(430) == pytest.approx(355)


@pytest.mark.parametrize("attributes", [{}, {"unit_of_measurement": "kW"}])
async def test_missing_optional_metadata_uses_zero_minimum_and_one_watt_step(economic_case, attributes):
    case = economic_case([.1, .4], [0, 0], [0, 2000])
    case.reading("number.charge_power", 1, **attributes)
    assert captured_constraints(case) == (0, None, 1)
    assert case.coord._feasible_battery_power(430.75) == 430


@pytest.mark.parametrize("invalid", [
    {"min": True}, {"max": True}, {"step": True},
    {"min": -1}, {"max": 0}, {"max": -1}, {"step": 0}, {"step": -1},
    {"min": 6000}, {"min": float("nan")}, {"max": float("inf")},
    {"step": "unknown"}, {"unit_of_measurement": "MW"},
])
async def test_invalid_metadata_blocks_planning_and_dispatch(economic_case, invalid):
    case = economic_case([.1, .4], [0, 0], [0, 2000])
    attributes = {"min": 0, "max": 5000, "step": 10, "unit_of_measurement": "W"}
    attributes.update(invalid)
    case.reading("number.charge_power", 1, **attributes)
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        case.controller.engage.assert_not_awaited()
        assert case.coord._battery_control_reason == "invalid_charge_power_constraints"
        assert case.coord._battery_grid_plan is None
        assert case.coord._feasible_battery_power(400) is None
    finally:
        await case.coord.async_stop_battery_controls()


@pytest.mark.parametrize("state", [None, "unknown", "unavailable"])
async def test_unavailable_configured_actuator_does_not_publish_an_executable_plan(economic_case, state):
    case = economic_case([.1, .4], [0, 0], [0, 2000])
    if state is None:
        case.states.pop("number.charge_power")
    else:
        case.reading("number.charge_power", state)
    assert case.coord._refresh_battery_grid_plan(case.tariff, None) == "invalid_charge_power_constraints"
    assert case.coord._battery_grid_plan is None


@pytest.mark.parametrize("changed", [{"min": 20}, {"max": 2000}, {"step": 20}])
async def test_actuator_metadata_changes_invalidate_cached_plan(economic_case, changed):
    case = economic_case([.1, .4], [0, 0], [0, 2000])
    attributes = {"min": 0, "max": 5000, "step": 10, "unit_of_measurement": "W"}
    case.reading("number.charge_power", 1, **attributes)
    with patch(
        "custom_components.pv_excess_control.battery_planner.build_battery_grid_plan",
        wraps=build_battery_grid_plan,
    ) as build:
        assert case.coord._refresh_battery_grid_plan(case.tariff, None) is None
        assert case.coord._refresh_battery_grid_plan(case.tariff, None) is None
        assert build.call_count == 1
        attributes.update(changed)
        case.reading("number.charge_power", 1, **attributes)
        assert case.coord._refresh_battery_grid_plan(case.tariff, None) is None
        assert build.call_count == 2


async def test_missing_mapping_keeps_direct_pure_planning_compatible(economic_case):
    case = economic_case([.1, .4], [0, 0], [0, 2000])
    del case.coord.config_entry.data["inverter_force_charge_power_entity"]
    assert captured_constraints(case) == (None, None, None)
    await case.coord._run_grid_charge_state_machine(case.tariff, None)
    case.controller.engage.assert_not_awaited()
    assert case.coord._battery_control_reason == "adjustable_power_control_required"


async def test_realizable_low_power_plan_reaches_controller_unchanged(economic_case):
    case = economic_case([.1, .4], [0, 0], [0, 340], battery_grid_charge_power_w=5000)
    case.reading("number.charge_power", 5000, min=0, max=5000, step=10)
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.coord._current_battery_slot().battery_charge_power_w == pytest.approx(400)
        case.controller.engage.assert_awaited_once_with(400)
    finally:
        await case.coord.async_stop_battery_controls()


async def test_unrealizable_residual_does_not_publish_a_rejected_charge_slot(economic_case):
    # 7.65 Wh of future useful demand would require a 9 W one-hour charge;
    # the actuator's first positive setpoint is 10 W.
    case = economic_case([.1, .4], [0, 0], [0, 7.65])
    case.reading("number.charge_power", 5000, min=0, max=5000, step=10)
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        case.controller.engage.assert_not_awaited()
        assert case.coord._battery_grid_plan.valid
        assert case.coord._battery_grid_plan.grid_energy_kwh == 0
        assert case.coord._battery_control_state == "self_consumption"
    finally:
        await case.coord.async_stop_battery_controls()


async def test_new_invalid_metadata_stops_existing_charge_and_clears_plan(economic_case):
    case = economic_case([.1, .4], [0, 0], [0, 2000])
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.coord._grid_charge_engaged
        case.reading("number.charge_power", "unavailable")
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.coord._battery_grid_plan is None
        assert not case.coord._grid_charge_engaged
        assert case.coord._battery_control_reason == "invalid_charge_power_constraints"
        case.controller.disengage.assert_awaited_once()
    finally:
        await case.coord.async_stop_battery_controls()
