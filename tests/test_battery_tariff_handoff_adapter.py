"""Active battery episodes acquire a coherent tariff at accounting boundaries."""
from dataclasses import replace
from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from custom_components.pv_excess_control.models import HourlyForecast
from tests.test_battery_control_economics import economic_case


def quarter_case(economic_case):
    case = economic_case([.1, .12, .5], [0, 0, 0], [1000, 3000, 5000])
    now = case.clock['now']
    ends = [now+timedelta(minutes=15), now+timedelta(minutes=30), now+timedelta(hours=3)]
    starts = [now, *ends[:-1]]
    case.tariff.windows = [replace(r, start=a, end=b) for r,a,b in zip(case.tariff.windows, starts, ends)]
    case.coord._forecast_data.hourly_breakdown = [HourlyForecast(a,b,0,0) for a,b in zip(starts,ends)]
    return case


def refresh_measurements(case, price):
    for entity, value in [('sensor.soc', 15), ('sensor.load_power',1000),
                          ('sensor.pv_power',0), ('sensor.price',price), ('sensor.forecast',0)]:
        case.reading(entity,value)


async def test_quarterhour_provider_refresh_keeps_owned_charge(economic_case):
    case = quarter_case(economic_case)
    old = case.tariff
    case.coord._latest_tariff = old
    try:
        await case.coord._run_grid_charge_state_machine(old, None)
        assert case.coord._grid_charge_engaged
        case.clock['now'] += timedelta(minutes=15)
        refresh_measurements(case,.12)
        fresh = replace(old,current_price=.12)
        case.coord._get_tariff_info = MagicMock(return_value=fresh)
        await case.coord._run_grid_charge_state_machine(old,None)
        assert case.coord._grid_charge_engaged
        case.controller.disengage.assert_not_awaited()
        case.coord._get_tariff_info.assert_called()
        assert old.current_price == .1
        assert case.coord._latest_tariff is old
        assert case.coord._battery_tariff_snapshot.current_price == .12
        assert case.coord._battery_retry_after is None
    finally:
        await case.coord.async_stop_battery_controls()


@pytest.mark.parametrize('fault', ['over_limit','contradictory','missing'])
async def test_handoff_cannot_bypass_changed_price_or_required_sensor(economic_case, fault):
    case = quarter_case(economic_case)
    old = case.tariff
    case.coord._latest_tariff = old
    try:
        await case.coord._run_grid_charge_state_machine(old,None)
        assert case.coord._grid_charge_engaged
        case.clock['now'] += timedelta(minutes=15)
        price = .8 if fault == 'over_limit' else .12
        refresh_measurements(case,price)
        windows = [replace(r,price=price) if r.start<=case.clock['now']<r.end else r for r in old.windows]
        fresh = replace(old,current_price=price,windows=windows)
        if fault == 'contradictory':fresh=replace(fresh,current_price=.11)
        if fault == 'missing':case.reading('sensor.price','unavailable')
        case.coord._get_tariff_info=MagicMock(return_value=fresh)
        await case.coord._run_grid_charge_state_machine(old,None)
        assert not case.coord._grid_charge_engaged
        case.controller.disengage.assert_awaited_once()
    finally:
        await case.coord.async_stop_battery_controls()
