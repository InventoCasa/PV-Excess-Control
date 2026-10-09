"""Rounded tariff windows must agree with full-precision prices within one half step."""
from dataclasses import replace
from datetime import timedelta

import pytest

from tests.test_battery_control_economics import economic_case  # noqa: F401


@pytest.mark.parametrize(("current", "window"), [
    (.123456, .1235),
    (.12345, .1235),
    (.12355, .1235),
])
async def test_four_decimal_window_rounding_allows_economic_charge(economic_case, current, window):
    case = economic_case([window, .4], [0, 0], [0, 2000])
    case.tariff.current_price = current
    case.reading("sensor.price", current)
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.coord._battery_grid_plan is not None
        assert case.coord._battery_grid_plan.valid
        assert case.coord._battery_control_state == "charging"
        case.controller.engage.assert_awaited_once()
    finally:
        await case.coord.async_stop_battery_controls()


@pytest.mark.parametrize("difference", [.000050001, -.000050001, .001])
async def test_difference_beyond_half_step_stops_owned_charge(economic_case, difference):
    case = economic_case([.1235, .4], [0, 0], [0, 2000])
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.coord._grid_charge_engaged
        current = .1235 + difference
        case.tariff.current_price = current
        case.reading("sensor.price", current)
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.coord._battery_control_reason == "price_window_mismatch"
        assert case.coord._battery_grid_plan is None
        assert not case.coord._grid_charge_engaged
        case.controller.disengage.assert_awaited_once()
    finally:
        await case.coord.async_stop_battery_controls()


@pytest.mark.parametrize("kind", ["duplicate", "current_gap", "future_gap"])
async def test_rounding_tolerance_does_not_hide_window_coverage_errors(economic_case, kind):
    case = economic_case([.1235, .4], [0, 0], [0, 2000])
    case.tariff.current_price = .123456
    case.reading("sensor.price", .123456)
    if kind == "duplicate":
        case.tariff.windows.append(replace(case.tariff.windows[0]))
    elif kind == "current_gap":
        del case.tariff.windows[0]
    else:
        row = case.tariff.windows[1]
        case.tariff.windows[1] = replace(row, start=row.start + timedelta(minutes=30))
    await case.coord._run_grid_charge_state_machine(case.tariff, None)
    expected = "tariff_gap_or_overlap" if kind == "future_gap" else "price_window_mismatch"
    assert case.coord._battery_control_reason == expected
    assert not case.coord._grid_charge_engaged
    case.controller.engage.assert_not_awaited()


async def test_rounding_tolerance_does_not_relax_current_sensor_snapshot(economic_case):
    case = economic_case([.1235, .4], [0, 0], [0, 2000])
    case.tariff.current_price = .123456
    # The live source still says .1235: unlike window rounding, that is a
    # changed measurement and the captured tariff snapshot must be refreshed.
    await case.coord._run_grid_charge_state_machine(case.tariff, None)
    assert case.coord._battery_control_reason == "price_snapshot_changed"
    case.controller.engage.assert_not_awaited()


async def test_purchase_ceiling_uses_full_current_price_despite_rounded_window(economic_case):
    case = economic_case([.2, .4], [0, 0], [0, 2000])
    case.tariff.current_price = .20002
    case.reading("sensor.price", .20002)
    await case.coord._run_grid_charge_state_machine(case.tariff, None)
    assert case.coord._battery_grid_plan is not None
    assert case.coord._battery_grid_plan.valid
    assert case.coord._battery_grid_plan.slots[0].action == "charge"
    assert not case.coord._grid_charge_engaged
    case.controller.engage.assert_not_awaited()
