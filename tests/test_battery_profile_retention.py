"""Measured hourly demand survives bounded gaps without fabricated readings."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from tests.test_battery_control_economics import economic_case


def learned(case, old_hours=(1,), age_days=8):
    now = case.clock['now']
    case.coord._battery_load_hours = [
        [500., 1800., (now-timedelta(days=age_days if h in old_hours else 1)).isoformat()]
        for h in range(24)
    ]


async def test_one_retained_hour_allows_economic_charge_without_rewriting_history(economic_case):
    case = economic_case([.1, .5], [0, 0], [0, 2500], battery_load_profile_w=None,
                         min_battery_soc=0)
    case.reading('sensor.soc', 0)
    learned(case)
    case.coord._battery_load_hours[0][0] = 0.
    case.coord._battery_load_hours[1][0] = 2500.
    original = deepcopy(case.coord._battery_load_hours)
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.coord._battery_grid_plan is not None
        assert case.coord._battery_grid_plan.grid_energy_kwh > 0
        assert case.coord._battery_control_state == 'charging'
        assert case.coord._battery_load_hours == original
        d = case.coord.battery_control_diagnostics()
        assert d['battery_load_profile_source'] == 'retained'
        assert d['battery_load_profile_retained_hours'] == [1]
        assert d['battery_load_profile_oldest_age_hours'] == 192
    finally:
        await case.coord.async_stop_battery_controls()


@pytest.mark.parametrize('age,allowed,retained', [
    (7, True, False), (7+1/86400, True, True),
    (14, True, True), (14+1/86400, False, True),
])
def test_elapsed_age_boundaries(economic_case, age, allowed, retained):
    case = economic_case([.1, .5], [0, 0], [500, 500], battery_load_profile_w=None)
    learned(case, age_days=age)
    assert (case.coord._battery_load_profile() is not None) is allowed
    if allowed:
        d = case.coord.battery_control_diagnostics()
        assert d['battery_load_profile_retained_hours'] == ([1] if retained else [])


@pytest.mark.parametrize('old_count,allowed', [(6, True), (7, False), (24, False)])
def test_requires_most_hours_to_remain_fresh(economic_case, old_count, allowed):
    case = economic_case([.1, .5], [0, 0], [500, 500], battery_load_profile_w=None)
    learned(case, old_hours=range(old_count))
    assert (case.coord._battery_load_profile() is not None) is allowed


@pytest.mark.parametrize('bad', ['missing', 'immature', 'nan', 'negative', 'infinite_seconds',
                               'bad_timestamp', 'naive', 'future'])
def test_retention_never_fills_invalid_or_unobserved_hours(economic_case, bad):
    case = economic_case([.1, .5], [0, 0], [500, 500], battery_load_profile_w=None)
    learned(case)
    row = case.coord._battery_load_hours[2]
    if bad == 'missing': row[2] = None
    elif bad == 'immature': row[1] = 1799
    elif bad == 'nan': row[0] = float('nan')
    elif bad == 'negative': row[0] = -1
    elif bad == 'infinite_seconds': row[1] = float('inf')
    elif bad == 'bad_timestamp': row[2] = 'invalid'
    elif bad == 'naive': row[2] = case.clock['now'].replace(tzinfo=None).isoformat()
    elif bad == 'future': row[2] = (case.clock['now']+timedelta(seconds=1)).isoformat()
    assert case.coord._battery_load_profile() is None


@pytest.mark.parametrize('entity', ['sensor.load_power', 'sensor.soc'])
async def test_retained_profile_cannot_bypass_required_measurements(economic_case, entity):
    case = economic_case([.1, .5], [0, 0], [500, 500], battery_load_profile_w=None)
    learned(case)
    assert case.coord._battery_load_profile() is not None
    case.reading(entity, 'unavailable')
    await case.coord._run_grid_charge_state_machine(case.tariff, None)
    case.controller.engage.assert_not_awaited()
    assert case.coord._battery_grid_plan is None


async def test_retained_profile_survives_reload_and_real_learning_restores_freshness(economic_case):
    case = economic_case([.1, .5], [0, 0], [500, 500], battery_load_profile_w=None)
    learned(case, old_hours=(0,))
    original = deepcopy(case.coord._battery_load_hours)
    case.coord._battery_journal.async_load.return_value = (None, None)
    store = MagicMock(async_load=AsyncMock(return_value={'hours':original}))
    with patch('custom_components.pv_excess_control.battery_control.Store', return_value=store):
        await case.coord.async_restore_battery_load()
    assert case.coord._battery_load_profile() == [500.] * 24
    assert case.coord._battery_load_hours == original
    case.coord._observe_battery_load()
    case.clock['now'] += timedelta(seconds=30)
    case.reading('sensor.load_power', 600, unit_of_measurement='W')
    case.coord._observe_battery_load()
    assert case.coord._battery_load_hours[0][2] == case.clock['now'].isoformat()
    assert case.coord.battery_control_diagnostics()['battery_load_profile_source'] == 'learned'


async def test_hard_expiry_invalidates_existing_plan(economic_case):
    case = economic_case([.1, .5], [0, 0], [500, 500], battery_load_profile_w=None)
    learned(case, age_days=14)
    assert case.coord._refresh_battery_grid_plan(case.tariff, None) is None
    assert case.coord._battery_grid_plan.valid
    case.clock['now'] += timedelta(seconds=1)
    await case.coord._async_refresh_battery_grid_plan(case.tariff, None)
    assert case.coord._battery_grid_plan is None


def test_explicit_profile_is_separate_from_observation_retention(economic_case):
    case = economic_case([.1, .5], [0, 0], [500, 500])
    assert case.coord.battery_control_diagnostics()['battery_load_profile_source'] == 'configured'


async def test_loss_of_fresh_hour_quorum_invalidates_cached_plan(economic_case):
    case = economic_case([.1, .5], [0, 0], [500, 500], battery_load_profile_w=None)
    learned(case, old_hours=range(6))
    case.coord._battery_load_hours[6][2] = (case.clock['now']-timedelta(days=7)).isoformat()
    assert case.coord._refresh_battery_grid_plan(case.tariff, None) is None
    case.clock['now'] += timedelta(seconds=1)
    assert await case.coord._async_refresh_battery_grid_plan(case.tariff, None) == 'household_profile_incomplete'
    assert case.coord._battery_grid_plan is None


def test_retention_age_across_fall_dst_uses_absolute_elapsed_time(economic_case):
    case = economic_case([.1, .5], [0, 0], [500, 500], battery_load_profile_w=None)
    zone = ZoneInfo('Europe/Berlin')
    case.clock['now'] = datetime(2026, 10, 25, 3, 0, tzinfo=zone)
    learned(case, old_hours=())
    # Fourteen wall-clock days include an extra hour at the autumn transition.
    case.coord._battery_load_hours[2][2] = datetime(2026,10,11,3,0,tzinfo=zone).isoformat()
    assert case.coord._battery_load_profile() is None
    case.coord._battery_load_hours[2][2] = (case.clock['now'].astimezone(timezone.utc)-timedelta(days=14)).isoformat()
    assert case.coord._battery_load_profile() is not None


def test_retention_does_not_learn_unknown_ev_share(economic_case):
    from tests.test_battery_load_context import appliance

    case = economic_case([.1, .5], [0, 0], [500, 500], battery_load_profile_w=None)
    learned(case)
    appliance(case, watts=0)
    case.reading('switch.load', 'off')
    case.coord._observe_battery_load()
    original = deepcopy(case.coord._battery_load_hours)
    case.clock['now'] += timedelta(seconds=301)
    case.reading('sensor.load_power', 6000, unit_of_measurement='W')
    case.coord._observe_battery_load()
    assert case.coord._battery_load_hours == original
    assert case.coord._battery_load_last[1] is None
