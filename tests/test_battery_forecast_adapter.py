"""Known price coverage, provider acquisition age and advisory PV isolation."""
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

import pytest

from custom_components.pv_excess_control.battery_planner import BatteryGridPlan
from tests.test_battery_control_economics import economic_case  # noqa: F401


def request(case):
    return case.coord._battery_planning_request(case.tariff, None)


def solar_case(factory):
    case = factory([.1, .4, .5], [0, 4, 0], [0, 0, 2000], forecast_provider='solcast',
                   battery_pv_forecast_factor=.75, min_battery_soc=0)
    rows = case.coord._forecast_data.hourly_breakdown
    case.reading('sensor.forecast', 4, detailedHourly=[
        {'period_start':r.start.isoformat(), 'period_end':r.end.isoformat(),
         'pv_estimate':r.expected_watts/1000, 'pv_estimate10':0.}
        for r in rows])
    return case


def test_adapter_uses_all_known_prices_beyond_twenty_four_hours(economic_case):
    case = economic_case([.1]*29+[.5], [0]*30, [500]*24)
    reason, value = request(case)
    assert reason is None
    assert value.config.horizon_hours == 48
    plan = value.build()
    assert plan.valid
    assert plan.slots[-1].end == case.clock['now'] + timedelta(hours=30)


def test_known_horizon_stops_at_forecast_coverage(economic_case):
    case = economic_case([.1]*30, [0]*30, [500]*24)
    case.coord._forecast_data.hourly_breakdown = case.coord._forecast_data.hourly_breakdown[:26]
    reason, value = request(case)
    assert reason is None
    assert value.build().slots[-1].end == case.clock['now'] + timedelta(hours=26)


def test_source_acquisition_older_than_report_is_rejected(economic_case):
    case = economic_case([.1, .4], [0, 0], [0, 2000],
                         battery_forecast_updated_entity='sensor.forecast_fetch')
    case.reading('sensor.forecast_fetch', (case.clock['now']-timedelta(hours=25)).isoformat())
    reason, value = request(case)
    assert reason == 'forecast_source_stale'
    assert value is None


def test_normal_overnight_acquisition_remains_valid(economic_case):
    case = economic_case([.1, .4], [0, 0], [0, 2000],
                         battery_forecast_updated_entity='sensor.forecast_fetch')
    stamp = case.clock['now']-timedelta(hours=15)
    case.reading('sensor.forecast_fetch', stamp.isoformat())
    reason, value = request(case)
    assert reason is None and value is not None
    meta = case.coord.battery_control_diagnostics()
    assert meta['battery_forecast_source_time'] == stamp.isoformat()
    assert meta['battery_forecast_source_age_seconds'] == 15*3600


@pytest.mark.parametrize('state', ['unavailable', 'unknown', 'bad', '2030-01-01'])
def test_configured_acquisition_requires_valid_aware_timestamp(economic_case, state):
    case = economic_case([.1, .4], [0, 0], [0, 2000],
                         battery_forecast_updated_entity='sensor.forecast_fetch')
    case.reading('sensor.forecast_fetch', state)
    reason, value = request(case)
    assert reason == 'forecast_source_unavailable'
    assert value is None


def test_future_acquisition_is_not_fresh(economic_case):
    case = economic_case([.1, .4], [0, 0], [0, 2000],
                         battery_forecast_updated_entity='sensor.forecast_fetch')
    case.reading('sensor.forecast_fetch', (case.clock['now']+timedelta(minutes=2)).isoformat())
    assert request(case)[0] == 'forecast_source_unavailable'


@pytest.mark.parametrize('hours', [True, 0, 73, float('nan'), float('inf')])
def test_invalid_source_age_budget_fails_closed(economic_case, hours):
    case = economic_case([.1, .4], [0, 0], [0, 2000],
                         battery_forecast_updated_entity='sensor.forecast_fetch',
                         battery_forecast_source_max_age_hours=hours)
    case.reading('sensor.forecast_fetch', case.clock['now'].isoformat())
    assert request(case)[0] == 'invalid_forecast_source_age'


def test_unmapped_source_age_stays_unknown(economic_case):
    case = economic_case([.1, .4], [0, 0], [0, 2000])
    assert request(case)[0] is None
    meta = case.coord.battery_control_diagnostics()
    assert meta['battery_forecast_source_time'] is None
    assert meta['battery_forecast_source_age_seconds'] is None


def test_downside_is_advisory_and_does_not_change_primary_plan(economic_case):
    case = solar_case(economic_case)
    reason, value = request(case)
    assert reason is None
    plan = value.build()
    assert plan.low_pv_plan is not None and plan.low_pv_plan.valid
    assert plan.low_pv_plan.grid_energy_kwh > plan.grid_energy_kwh
    nominal = replace(value, low_forecast=None).build()
    assert plan.slots == nominal.slots
    assert plan.grid_energy_kwh == nominal.grid_energy_kwh
    assert case.coord._forecast_data.hourly_breakdown[1].expected_kwh == 4


def test_changed_low_forecast_invalidates_only_advisory_cache(economic_case):
    case = solar_case(economic_case)
    _, before = request(case)
    raw = [dict(x) for x in case.states['sensor.forecast'].attributes['detailedHourly']]
    raw[1]['pv_estimate10'] = .5
    case.reading('sensor.forecast', 4, detailedHourly=raw)
    _, after = request(case)
    assert before.key == after.key
    assert before.risk_key != after.risk_key


async def test_advisory_grid_purchase_never_becomes_a_hardware_request(economic_case):
    case = solar_case(economic_case)
    case.reading('sensor.soc', 0)
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        case.controller.engage.assert_not_awaited()
        plan = case.coord._battery_grid_plan
        assert plan.low_pv_plan.grid_energy_kwh > 0
        assert plan.grid_energy_kwh == 0
        meta = case.coord.battery_control_diagnostics()
        assert meta['battery_pv_risk_status'] == 'available'
        assert meta['battery_pv_risk_grid_energy_kwh'] > 0
    finally:
        await case.coord.async_stop_battery_controls()


def test_optional_risk_failure_keeps_valid_primary(economic_case):
    case = solar_case(economic_case)
    _, value = request(case)
    primary = BatteryGridPlan((), 0., 0., 'no_economic_benefit', 10.)
    with patch('custom_components.pv_excess_control.battery_planner.build_battery_grid_plan',
               side_effect=[primary, ValueError('bad scenario')]):
        result = value.build()
    assert result.valid and result.slots == primary.slots
    assert result.low_pv_plan is not None and not result.low_pv_plan.valid


async def test_old_provider_acquisition_stops_owned_charge_despite_fresh_reports(economic_case):
    case = economic_case([.1, .4], [0, 0], [0, 2000],
                         battery_forecast_updated_entity='sensor.forecast_fetch')
    case.reading('sensor.forecast_fetch', case.clock['now'].isoformat())
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.coord._grid_charge_engaged
        case.reading('sensor.forecast_fetch', (case.clock['now']-timedelta(hours=25)).isoformat())
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert not case.coord._grid_charge_engaged
        assert case.coord._battery_grid_plan is None
        assert case.coord._battery_control_reason == 'forecast_source_stale'
        case.controller.disengage.assert_awaited_once()
    finally:
        await case.coord.async_stop_battery_controls()


def test_valid_provider_acquisition_updates_metadata_without_retargeting(economic_case):
    case = economic_case([.1, .4], [0, 0], [0, 2000],
                         battery_forecast_updated_entity='sensor.forecast_fetch')
    case.reading('sensor.forecast_fetch', (case.clock['now']-timedelta(hours=2)).isoformat())
    _, old = request(case)
    case.reading('sensor.forecast_fetch', case.clock['now'].isoformat())
    _, new = request(case)
    assert old.key == new.key
    assert case.coord.battery_control_diagnostics()['battery_forecast_source_age_seconds'] == 0


async def test_manual_request_is_independent_of_optional_forecast_acquisition(economic_case):
    case = economic_case([.1, .4], [0, 0], [0, 2000],
                         battery_forecast_updated_entity='sensor.forecast_fetch')
    case.reading('sensor.forecast_fetch', 'unavailable')
    case.coord.force_charge = True
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.coord._grid_charge_engaged
    finally:
        await case.coord.async_stop_battery_controls()


async def test_stop_interrupts_wait_for_advisory_cpu_search(economic_case, monkeypatch):
    import asyncio
    import threading
    from custom_components.pv_excess_control import battery_planner

    case = solar_case(economic_case)
    entered, release = threading.Event(), threading.Event()
    original = battery_planner.build_battery_grid_plan
    calls = []
    main_thread = threading.get_ident()

    def search(*args, **kwargs):
        calls.append(threading.get_ident())
        if len(calls) == 2:
            entered.set()
            if not release.wait(3):
                raise RuntimeError('worker was not released')
        return original(*args, **kwargs)

    async def executor(function, *args):
        return await asyncio.to_thread(function, *args)

    case.coord.hass.async_add_executor_job = executor
    monkeypatch.setattr(battery_planner, 'build_battery_grid_plan', search)
    task = asyncio.create_task(case.coord._async_refresh_battery_grid_plan(case.tariff, None))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        assert not task.done()
        case.coord._battery_unloading = True
        await asyncio.wait_for(case.coord.async_stop_battery_controls('unload'), .5)
        assert await asyncio.wait_for(task, .5) == 'planning_interrupted'
        assert case.coord._battery_grid_plan is None
        assert all(thread != main_thread for thread in calls)
        case.controller.engage.assert_not_awaited()
    finally:
        release.set()
        await task
        case.coord._cancel_battery_watchdog()


@pytest.mark.parametrize('mapped,expected', [(True, None), (False, 'forecast_stale')])
def test_mapped_acquisition_is_authoritative_for_valid_overnight_forecast(economic_case, mapped, expected):
    from homeassistant.core import State
    case = economic_case([.1, .4], [0, 0], [0, 2000])
    old = case.clock['now']-timedelta(hours=15)
    current = case.states['sensor.forecast']
    case.states['sensor.forecast'] = State('sensor.forecast', current.state, current.attributes,
                                         last_updated=old, last_reported=old)
    if mapped:
        case.coord.config_entry.data['battery_forecast_updated_entity'] = 'sensor.forecast_fetch'
        case.reading('sensor.forecast_fetch', old.isoformat())
    assert request(case)[0] == expected


async def test_risk_only_update_keeps_cached_operational_plan(economic_case):
    from unittest.mock import AsyncMock
    case = solar_case(economic_case)
    assert case.coord._refresh_battery_grid_plan(case.tariff, None) is None
    before = case.coord._battery_grid_plan
    assert before.low_pv_plan is not None
    raw = [dict(x) for x in case.states['sensor.forecast'].attributes['detailedHourly']]
    raw[1]['pv_estimate10'] = .5
    case.reading('sensor.forecast', 4, detailedHourly=raw)
    case.coord._build_battery_plan_async = AsyncMock(side_effect=AssertionError('No primary replan for risk only'))
    assert await case.coord._async_refresh_battery_grid_plan(case.tariff, None) is None
    assert case.coord._battery_grid_plan.slots is before.slots
    assert case.coord._battery_grid_plan.low_pv_plan is None
    assert case.coord.battery_control_diagnostics()['battery_pv_risk_status'] == 'refresh_pending'
    case.coord._build_battery_plan_async.assert_not_awaited()


async def test_risk_change_during_worker_keeps_valid_nominal_result(economic_case):
    import asyncio
    case = solar_case(economic_case)
    builds = 0
    async def executor(function, *args):
        nonlocal builds
        result = await asyncio.to_thread(function, *args)
        builds += 1
        raw = [dict(x) for x in case.states['sensor.forecast'].attributes['detailedHourly']]
        raw[1]['pv_estimate10'] = builds*.1
        case.reading('sensor.forecast', 4, detailedHourly=raw)
        return result
    case.coord.hass.async_add_executor_job = executor
    assert await case.coord._async_refresh_battery_grid_plan(case.tariff, None) is None
    assert builds == 1
    assert case.coord._battery_grid_plan.valid
    assert case.coord._battery_grid_plan.low_pv_plan is None
    assert case.coord.battery_control_diagnostics()['battery_pv_risk_status'] == 'refresh_pending'


def test_energy_diagnostics_use_elapsed_utc_time_across_repeated_hour(economic_case):
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from custom_components.pv_excess_control.models import HourlyForecast
    case = economic_case([.1, .4], [0, 0], [0, 2000])
    _, value = request(case)
    plan = value.build()
    local = ZoneInfo('Europe/Berlin')
    start = datetime(2026, 10, 25, 2, tzinfo=local, fold=0)
    end = datetime(2026, 10, 25, 2, tzinfo=local, fold=1)
    value = replace(value, forecast=(HourlyForecast(start, end, 1, 1000),))
    slot = replace(plan.slots[0], start=start.astimezone(timezone.utc), end=end.astimezone(timezone.utc))
    assert case.coord._commit_battery_grid_plan(value, replace(plan, slots=(slot,))) is None
    assert case.coord.battery_control_diagnostics()['battery_pv_forecast_kwh'] == 1


def test_five_minute_refresh_uses_elapsed_time_across_clock_fallback(economic_case):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    case = economic_case([.1, .4], [0, 0], [0, 2000])
    _, value = request(case)
    local = ZoneInfo('Europe/Berlin')
    case.coord._battery_plan_created = datetime(2026, 10, 25, 2, 55, tzinfo=local, fold=0)
    case.coord._battery_plan_key = value.key
    case.coord._current_battery_slot = lambda: object()
    value = replace(value, now=datetime(2026, 10, 25, 2, 1, tzinfo=local, fold=1))
    assert case.coord._battery_plan_needs_refresh(value)
