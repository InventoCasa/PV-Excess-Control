"""Status reasons distinguish native consumption from a completed charge."""

import pytest

from tests.test_battery_control_economics import economic_case


@pytest.mark.parametrize("soc", [0, 30, 100])
async def test_native_slot_does_not_claim_charge_target_reached(economic_case, soc):
    case = economic_case([.4, .4], [0, 0], [500, 500], min_battery_soc=0)
    case.reading("sensor.soc", soc)
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.coord._current_battery_slot().action == "self_consumption"
        assert case.coord._battery_control_state == "self_consumption"
        assert case.coord._battery_control_reason == "self_consumption"
        case.controller.engage.assert_not_awaited()
    finally:
        await case.coord.async_stop_battery_controls()


async def test_actual_grid_charge_target_keeps_completion_reason(economic_case):
    case = economic_case([.1, .4], [0, 0], [0, 2500], battery_grid_target_soc=30)
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.coord._grid_charge_engaged
        case.reading("sensor.soc", 30)
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert not case.coord._grid_charge_engaged
        assert case.coord._battery_control_reason == "target_reached"
        case.controller.disengage.assert_awaited_once()
    finally:
        await case.coord.async_stop_battery_controls()
