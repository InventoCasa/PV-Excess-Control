"""Fresh plans must also use fresh dispatch caps after asynchronous work."""
import asyncio

import pytest

from tests.test_battery_control_economics import economic_case  # noqa: F401


@pytest.mark.parametrize('phase', ['search', 'ownership'])
async def test_replaced_configuration_caps_actual_grid_command_after_wait(economic_case, phase):
    case = economic_case([.1, .4], [3, 0], [0, 4000],
                         battery_grid_charge_power_w=5000,
                         battery_max_charge_power_w=6570,
                         battery_max_discharge_default=5000)
    case.reading('sensor.pv_power', 0, unit_of_measurement='W')
    calls = 0
    async def executor(function, *args):
        nonlocal calls
        result = await asyncio.to_thread(function, *args)
        calls += 1
        if phase == 'search' and calls == 1:
            case.coord.config_entry.data = {**case.coord.config_entry.data,
                                           'battery_grid_charge_power_w': 1000}
        return result
    case.coord.hass.async_add_executor_job = executor
    if phase == 'ownership':
        original = case.coord._save_battery_ownership
        changed = False
        async def save():
            nonlocal changed
            await original()
            if not changed:
                changed = True
                case.coord.config_entry.data = {**case.coord.config_entry.data,
                                               'battery_grid_charge_power_w': 1000}
        case.coord._save_battery_ownership = save
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.controller.engage.await_count == 1, (case.coord._battery_control_reason, case.coord._battery_grid_plan)
        assert 0 < case.controller.engage.await_args.args[0] <= 1000
    finally:
        await case.coord.async_stop_battery_controls()


async def test_manual_charge_also_uses_replaced_power_limit_after_ownership_wait(economic_case):
    case = economic_case([.1, .4], [0, 0], [0, 2000], battery_grid_charge_power_w=5000)
    case.coord.force_charge = True
    original = case.coord._save_battery_ownership
    changed = False
    async def save():
        nonlocal changed
        await original()
        if not changed:
            changed = True
            case.coord.config_entry.data = {**case.coord.config_entry.data,
                                           'battery_grid_charge_power_w': 1000}
    case.coord._save_battery_ownership = save
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        case.controller.engage.assert_awaited_once_with(1000)
    finally:
        await case.coord.async_stop_battery_controls()


async def test_new_configured_price_limit_beats_stale_provider_threshold(economic_case):
    case = economic_case([.1, .4], [0, 0], [0, 2000])
    calls = 0
    async def executor(function, *args):
        nonlocal calls
        plan = await asyncio.to_thread(function, *args)
        calls += 1
        if calls == 1:
            case.coord.config_entry.data = {**case.coord.config_entry.data,
                                           'battery_charge_price_threshold': .05}
        return plan
    case.coord.hass.async_add_executor_job = executor
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        case.controller.engage.assert_not_awaited()
        assert case.coord._battery_grid_plan.grid_energy_kwh == 0
    finally:
        await case.coord.async_stop_battery_controls()


async def test_dispatch_uses_new_provider_snapshot_that_made_charge_economic(economic_case):
    from dataclasses import replace
    case = economic_case([.3, .4], [0, 0], [0, 2000])
    calls = 0
    async def executor(function, *args):
        nonlocal calls
        plan = await asyncio.to_thread(function, *args)
        calls += 1
        if calls == 1:
            case.coord._latest_tariff = replace(case.tariff, current_price=.1,
                windows=[replace(case.tariff.windows[0], price=.1), case.tariff.windows[1]])
            case.reading('sensor.price', .1)
        return plan
    case.coord.hass.async_add_executor_job = executor
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        case.controller.engage.assert_awaited_once()
        assert case.coord._battery_control_state == 'charging'
    finally:
        await case.coord.async_stop_battery_controls()
