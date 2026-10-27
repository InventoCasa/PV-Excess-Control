"""Economic hold release is confirmed across time, including shared controls."""
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.pv_excess_control.battery_control import BatteryControlMixin
from custom_components.pv_excess_control.models import BatteryDischargeAction
from tests.test_battery_control_economics import economic_case
from tests.test_battery_numeric_hold import numeric_case


def fresh(case, seconds=30):
    case.clock['now'] += timedelta(seconds=seconds)
    for e, value in [('sensor.soc',30),('sensor.load_power',1000),
                     ('sensor.pv_power',0),('sensor.price',.2),('sensor.forecast',0)]:
        case.reading(e,value)


async def start_hold(case):
    case.coord.config_entry.data['battery_charge_price_threshold'] = 0
    case.coord._latest_tariff = case.tariff
    case.reading('sensor.soc',30)
    await case.coord._run_grid_charge_state_machine(case.tariff,None)
    return case.coord._battery_grid_plan


def native_proposal(case, old):
    first = old.slots[0]
    case.coord._battery_grid_plan = replace(old, slots=(replace(first,action='self_consumption'),*old.slots[1:]))
    case.coord._async_refresh_battery_grid_plan = AsyncMock(return_value=None)


async def test_transient_native_proposal_does_not_release_confirmed_hold(economic_case):
    case=economic_case([.2,.5],[0,0],[1000,3000])
    hold=MagicMock(confirmation_level='physical')
    hold.engage=AsyncMock();hold.disengage=AsyncMock();hold.verify_engaged=AsyncMock()
    case.coord._battery_hold_ctl=hold
    try:
        old=await start_hold(case)
        assert case.coord._battery_hold_engaged
        fresh(case)
        native_proposal(case,old)
        await case.coord._run_grid_charge_state_machine(case.tariff,None)
        assert case.coord._battery_hold_engaged
        hold.disengage.assert_not_awaited()
        assert case.coord._battery_control_reason=='hold_release_pending'
        # The published economic proposal is not rewritten with invented energy.
        assert case.coord._battery_grid_plan.slots[0].action=='self_consumption'
        deadline=case.coord.battery_control_diagnostics()['battery_hold_release_pending_until']
        fresh(case)
        await case.coord._run_grid_charge_state_machine(case.tariff,None)
        assert case.coord.battery_control_diagnostics()['battery_hold_release_pending_until']==deadline
        fresh(case)
        await case.coord._run_grid_charge_state_machine(case.tariff,None)
        hold.disengage.assert_awaited_once()
        assert not case.coord._battery_hold_engaged
    finally:
        await case.coord.async_stop_battery_controls()


@pytest.mark.parametrize('interruption',['disabled','missing_soc','charge','forecast_change'])
async def test_pending_hold_does_not_delay_hard_changes(economic_case,interruption):
    case=economic_case([.2,.5],[0,0],[1000,3000])
    hold=MagicMock(confirmation_level='physical')
    hold.engage=AsyncMock();hold.disengage=AsyncMock();hold.verify_engaged=AsyncMock()
    case.coord._battery_hold_ctl=hold
    try:
        old=await start_hold(case)
        fresh(case)
        native_proposal(case,old)
        await case.coord._run_grid_charge_state_machine(case.tariff,None)
        assert case.coord._battery_hold_engaged
        if interruption=='disabled':case.coord.enabled=False
        elif interruption=='missing_soc':case.reading('sensor.soc','unavailable')
        elif interruption=='forecast_change':
            case.coord._forecast_data.hourly_breakdown[0]=replace(case.coord._forecast_data.hourly_breakdown[0],expected_kwh=.1)
        else:
            case.coord.force_charge=True
        await case.coord._run_grid_charge_state_machine(case.tariff,None)
        hold.disengage.assert_awaited_once()
        assert case.coord.battery_control_diagnostics()['battery_hold_release_pending_until'] is None
    finally:
        await case.coord.async_stop_battery_controls()


async def test_numeric_hold_revalidation_preserves_same_pending_release(numeric_case):
    case=numeric_case
    case.coord._battery_action_after_wait=BatteryControlMixin._battery_action_after_wait.__get__(case.coord)
    try:
        old=await start_hold(case)
        assert case.coord._battery_numeric_hold_requested, (case.coord._battery_control_reason, old)
        assert await case.coord._numeric_hold_still_requested(), (case.coord._battery_control_reason, case.coord._battery_grid_plan)
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(False))
        assert case.writes==[10]
        fresh(case)
        native_proposal(case,old)
        await case.coord._run_grid_charge_state_machine(case.tariff,None)
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(False))
        assert case.writes==[10]
        assert case.coord._battery_grid_plan is not None
        assert case.coord._battery_hold_engaged
        fresh(case,60)
        await case.coord._run_grid_charge_state_machine(case.tariff,None)
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(False))
        assert case.writes==[10,5000]
    finally:
        await case.coord.async_stop_battery_controls()


async def test_shared_expiry_without_main_loop_clears_pending_diagnostics(numeric_case):
    case = numeric_case
    case.coord._battery_action_after_wait = BatteryControlMixin._battery_action_after_wait.__get__(case.coord)
    try:
        old = await start_hold(case)
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(False))
        fresh(case)
        native_proposal(case, old)
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(False))
        assert case.coord._battery_hold_release.pending_until is not None
        fresh(case, 60)
        # The expiry/shared-control path must not depend on another main tick.
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(False))
        assert case.writes == [10, 5000]
        assert not case.coord._battery_hold_engaged
        assert case.coord.battery_control_diagnostics()['battery_hold_release_pending_until'] is None
        assert case.coord._battery_hold_release.authorized_until is None
    finally:
        await case.coord.async_stop_battery_controls()
