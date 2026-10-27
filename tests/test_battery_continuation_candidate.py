"""Fresh replanning keeps an executable charging candidate across confirmation."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.pv_excess_control.models import (
    ForecastData, HourlyForecast, InverterGridChargeConfig, TariffWindow,
)
from tests.test_battery_control_economics import economic_case  # noqa: F401


@pytest.fixture
def continuation_case(economic_case):
    prices = [.15, .25, .15, .4, .15, .5, .4, .25, .3, .35, .3, .5]
    case = economic_case(prices, [0.] * 12, [800., 200., 800.],
        battery_capacity=2., min_battery_soc=0., battery_grid_target_soc=100.,
        battery_grid_charge_power_w=1000., battery_max_charge_power_w=1000.,
        battery_max_discharge_default=2000., battery_roundtrip_efficiency=.85,
        battery_wear_cost_per_kwh=.03, battery_charge_price_threshold=.8,
        battery_grid_charge_minimum_minutes=5., grid_charge_engage_min_duration_minutes=5.)
    origin = datetime(2030, 1, 1, tzinfo=timezone.utc)
    case.clock['now'] = origin + timedelta(minutes=12, seconds=19)
    for entity, state in list(case.states.items()):
        case.reading(entity, state.state, **state.attributes)
    case.reading('sensor.soc', 30.)
    case.reading('number.charge_power', 1000., min=0., max=1000., step=10., unit_of_measurement='W')
    case.controller.config = InverterGridChargeConfig(
        'switch.charge', 'on', 'off', mode_entity_id='select.mode',
        mode_engage_value='Forced', mode_disengage_value='Self',
        power_entity_id='number.charge_power', timeout_seconds=15.)
    case.coord._battery_hold_ctl = MagicMock(confirmation_level='physical',
        engage=AsyncMock(), disengage=AsyncMock(), verify_engaged=AsyncMock())
    case.tariff.windows = [TariffWindow(origin + timedelta(minutes=15*i),
        origin + timedelta(minutes=15*(i+1)), price, False) for i, price in enumerate(prices)]
    case.coord._forecast_data = ForecastData(0., [HourlyForecast(row.start, row.end, 0., 0.)
        for row in case.tariff.windows])
    case.coord._battery_charge_prepared = True
    assert case.coord._refresh_battery_grid_plan(case.tariff, None) is None
    case.prepared = case.coord._battery_grid_plan
    assert case.prepared.slots[0].action == 'charge'
    case.coord._grid_charge_engaged = True
    case.coord._grid_charge_cleanup_pending = True
    case.coord._battery_charge_stop_confirmed = False
    case.coord._battery_charge_prepared = False
    case.coord._battery_charge_minimum_until = case.clock['now'] + timedelta(minutes=5)
    return case


def test_confirmation_replan_retains_a_cheaper_feasible_charging_trajectory(continuation_case):
    case = continuation_case
    assert case.coord._refresh_battery_grid_plan(case.tariff, None) is None
    active = case.coord._battery_grid_plan
    assert active.slots[0].action == 'charge'
    assert active.estimated_savings >= case.prepared.estimated_savings - 1e-9


def replay(case, *, config_changes=None, previous=None):
    from dataclasses import replace
    from math import sqrt
    from custom_components.pv_excess_control import battery_planner as bp
    from custom_components.pv_excess_control.battery_continuation import replay_continuation

    reason, request = case.coord._battery_planning_request(case.tariff, None)
    assert reason is None
    config = replace(request.config, **(config_changes or {}))
    protected = (config.grid_charge_minimum_until,) if config.grid_charge_minimum_until else ()
    timeline = bp._timeline(request.now, request.tariffs, request.forecast, request.profile,
        config.horizon_hours, request.commitments, protected_instants=protected)
    initial = bp._Node(request.soc / 100 * config.capacity_kwh, 0., 0., None, None,
        charge_active=config.grid_charge_active, charge_grid_delivered=config.grid_charge_active,
        charge_minimum_until=config.grid_charge_minimum_until)
    return replay_continuation(timeline, config, sqrt(config.roundtrip_efficiency),
        initial, previous or case.prepared)


@pytest.mark.parametrize('limits', [
    {'max_charge_power_w': 500.},
    {'max_pv_charge_power_w': 500.},
    {'forced_charge_max_power_w': 500.},
    {'forced_charge_power_step_w': 300.},
    {'forced_charge_min_power_w': 500.},
    {'grid_target_soc': 30.},
    {'charge_price_limit': .1},
    {'grid_restart_delay_minutes': 30.},
    {'hold_supported': False},
    {'grid_charge_active': False},
])
def test_current_limits_can_exclude_old_charging_trajectory(continuation_case, limits):
    assert replay(continuation_case, config_changes=limits) is None


def test_new_minimum_deadline_cannot_end_at_old_earlier_boundary(continuation_case):
    case = continuation_case
    assert replay(case, config_changes={
        'grid_charge_minimum_until': case.clock['now'] + timedelta(minutes=40)}) is None


def test_fresh_soc_prevents_rebuying_unneeded_energy(continuation_case):
    case = continuation_case
    case.reading('sensor.soc', 100.)
    assert replay(case) is None


def test_updated_solar_cannot_be_displaced_by_previous_purchase(continuation_case):
    case = continuation_case
    row = case.coord._forecast_data.hourly_breakdown[3]
    row.expected_kwh = 2.
    row.expected_watts = 8000.
    assert replay(case) is None


def test_reduced_future_demand_rejects_extra_terminal_inventory(continuation_case):
    case = continuation_case
    case.coord.config_entry.data['battery_load_profile_w'] = [800.] + [0.] * 23
    assert replay(case) is None


def test_unchanged_candidate_preserves_immutable_source_and_fresh_balances(continuation_case):
    from copy import deepcopy
    from custom_components.pv_excess_control import battery_planner as bp
    case = continuation_case
    previous = deepcopy(case.prepared)
    result = replay(case)
    assert result is not None
    assert case.prepared == previous
    assert result.energy == pytest.approx(0., abs=1e-9)
    slots = bp._slots(result)
    assert slots[0].start == case.clock['now']
    assert case.coord._battery_charge_minimum_until in {row.end for row in slots}
    assert slots[0].battery_charge_power_w == pytest.approx(1000.)


def test_price_reversal_within_limit_preserves_confirmed_minimum(continuation_case):
    from dataclasses import replace
    case = continuation_case
    case.reading('sensor.price', .7)
    case.tariff.current_price = .7
    case.tariff.windows[0] = replace(case.tariff.windows[0], price=.7)
    assert case.coord._refresh_battery_grid_plan(case.tariff, None) is None
    plan = case.coord._battery_grid_plan
    deadline = case.coord._battery_charge_minimum_until
    assert all(row.action == 'charge' for row in plan.slots if row.start < deadline)
    assert deadline in {row.end for row in plan.slots}


@pytest.mark.parametrize('change', ['disabled', 'missing_soc', 'missing_load', 'price_limit', 'forecast_gap'])
async def test_candidate_never_blocks_immediate_runtime_stop(continuation_case, change):
    from custom_components.pv_excess_control import battery_planner as bp
    case = continuation_case
    episode = bp.charge_episode_at(case.prepared, case.clock['now'])
    case.coord._battery_charge_deadline = episode.end
    case.coord._battery_charge_target_soc = episode.target_soc
    case.coord._battery_charge_power = episode.power_w
    if change == 'disabled':
        case.coord.enabled = False
    elif change == 'missing_soc':
        case.reading('sensor.soc', 'unavailable')
    elif change == 'missing_load':
        case.reading('sensor.load_power', 'unavailable')
    elif change == 'price_limit':
        case.coord.config_entry.data['battery_charge_price_threshold'] = .1
    else:
        del case.coord._forecast_data.hourly_breakdown[1]
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert not case.coord._grid_charge_engaged
        case.controller.disengage.assert_awaited_once()
    finally:
        await case.coord.async_stop_battery_controls()


def test_later_confirmation_preserves_deadline_and_uses_only_remaining_energy(continuation_case):
    case = continuation_case
    deadline = case.coord._battery_charge_minimum_until
    case.clock['now'] += timedelta(seconds=30)
    for entity, state in list(case.states.items()):
        case.reading(entity, state.state, **state.attributes)
    assert case.coord._refresh_battery_grid_plan(case.tariff, None) is None
    first = case.coord._battery_grid_plan.slots[0]
    assert first.action == 'charge'
    assert first.start == case.clock['now']
    assert first.grid_charge_kwh == pytest.approx((first.end - first.start).total_seconds() / 3600)
    assert case.coord._battery_charge_minimum_until == deadline
