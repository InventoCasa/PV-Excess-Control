"""The runtime state machine preserves confirmed minima, never safety overrides."""
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.pv_excess_control.models import InverterGridChargeConfig
from tests.test_battery_control_economics import economic_case  # noqa: F401


@pytest.fixture
def confirmed_case(economic_case):
    """Represent an already physically confirmed, owned automatic episode."""
    case = economic_case([.4, .1], [0., 0.], [500., 500.],
        battery_capacity=2., min_battery_soc=0., battery_grid_target_soc=100.,
        battery_grid_charge_power_w=1000., battery_max_charge_power_w=1000.,
        battery_max_discharge_default=1000., battery_roundtrip_efficiency=.85,
        battery_wear_cost_per_kwh=.03, battery_charge_price_threshold=.8,
        battery_grid_charge_minimum_minutes=5., grid_charge_engage_min_duration_minutes=5.)
    case.controller.config = InverterGridChargeConfig(
        'switch.charge', 'on', 'off', mode_entity_id='select.mode',
        mode_engage_value='Forced', mode_disengage_value='Self',
        power_entity_id='number.charge_power', timeout_seconds=15.)
    case.reading('number.charge_power', 500., min=0., max=1000., step=10., unit_of_measurement='W')
    case.coord._battery_hold_ctl = MagicMock(confirmation_level='physical',
        engage=AsyncMock(), disengage=AsyncMock(), verify_engaged=AsyncMock())
    case.coord._grid_charge_cleanup_pending = True
    case.coord._battery_charge_owner = case.controller
    case.coord._battery_charge_stop_confirmed = False
    case.coord._mark_battery_charge_engaged(500., manual=False, minimum_minutes=5.)
    case.coord._battery_charge_deadline = case.clock['now'] + timedelta(minutes=15)
    case.original_minimum = case.coord._battery_charge_minimum_until
    case.original_start = case.coord._battery_charge_started_at
    return case


def fresh(case):
    for entity, state in list(case.states.items()):
        case.reading(entity, state.state, **state.attributes)


async def test_negative_economic_replan_continues_owned_charge_without_new_start(confirmed_case):
    case = confirmed_case
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        plan = case.coord._battery_grid_plan
        assert plan.valid and plan.estimated_savings < 0
        assert plan.reason == 'confirmed_charge_minimum'
        assert case.coord._grid_charge_engaged
        assert case.coord._battery_control_state == 'charging'
        assert case.coord._battery_control_reason == 'minimum_charge_continuation'
        assert case.coord._battery_charge_minimum_until == case.original_minimum
        assert case.coord._battery_charge_started_at == case.original_start
        case.controller.verify_engaged.assert_awaited()
        case.controller.disengage.assert_not_awaited()
        case.controller.engage.assert_not_awaited()
        case.controller.enable_prepared.assert_not_awaited()
    finally:
        await case.coord.async_stop_battery_controls()


async def test_changing_soc_power_and_partial_tariff_boundary_preserve_original_deadline(confirmed_case):
    case = confirmed_case
    origin = case.clock['now']
    # The price changes during the remaining physical minimum, below its cap.
    first = case.tariff.windows[0]
    edge = origin + timedelta(minutes=2)
    case.tariff.windows[:1] = [replace(first, end=edge), replace(first, start=edge, price=.45)]
    try:
        for seconds, soc, cap in [(0, 10., 1000.), (60, 11.1, 800.), (120, 12.2, 600.), (240, 13.3, 500.)]:
            case.clock['now'] = origin + timedelta(seconds=seconds)
            fresh(case)
            case.reading('sensor.soc', soc)
            price = .4 if seconds < 120 else .45
            case.tariff.current_price = price
            case.reading('sensor.price', price)
            case.coord.config_entry.data['battery_grid_charge_power_w'] = cap
            case.coord._battery_plan_key = None
            await case.coord._run_grid_charge_state_machine(case.tariff, None)
            assert case.coord._grid_charge_engaged
            assert case.coord._battery_charge_power <= cap
            assert case.coord._battery_charge_minimum_until == case.original_minimum
            assert case.coord._battery_charge_started_at == case.original_start
        case.controller.disengage.assert_not_awaited()
        case.controller.set_power.assert_awaited()
    finally:
        await case.coord.async_stop_battery_controls()


@pytest.mark.parametrize('change', ['disabled', 'automation_off', 'allow_off', 'soc_missing',
                                  'load_missing', 'over_price_cap', 'configured_target',
                                  'physical_full', 'feedback_failure'])
async def test_confirmed_minimum_never_prevents_runtime_safety_or_policy_stop(confirmed_case, change):
    case = confirmed_case
    if change == 'disabled':
        case.coord.enabled = False
    elif change == 'automation_off':
        case.coord.config_entry.data['auto_battery_grid_charge'] = False
    elif change == 'allow_off':
        case.coord.config_entry.data['allow_grid_charging'] = False
    elif change == 'soc_missing':
        case.reading('sensor.soc', 'unavailable')
    elif change == 'load_missing':
        case.reading('sensor.load_power', 'unavailable')
    elif change == 'over_price_cap':
        case.coord.config_entry.data['battery_charge_price_threshold'] = .3
    elif change == 'configured_target':
        case.coord.config_entry.data['battery_grid_target_soc'] = 10.
    elif change == 'physical_full':
        case.reading('sensor.soc', 100.)
    else:
        case.controller.verify_engaged.side_effect = RuntimeError('physical confirmation lost')
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert not case.coord._grid_charge_engaged
        assert case.coord._battery_charge_minimum_until is None
        case.controller.disengage.assert_awaited_once()
    finally:
        await case.coord.async_stop_battery_controls()


async def test_manual_stop_clears_confirmed_minimum_immediately(confirmed_case):
    case = confirmed_case
    await case.coord.async_stop_battery_controls('manual_cancelled')
    assert not case.coord._grid_charge_engaged
    assert case.coord._battery_charge_minimum_until is None
    case.controller.disengage.assert_awaited_once()


async def test_expiry_allows_economic_stop_without_a_new_five_minute_minimum(confirmed_case):
    case = confirmed_case
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.coord._grid_charge_engaged
        case.clock['now'] = case.original_minimum
        fresh(case)
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert not case.coord._grid_charge_engaged
        assert case.coord._battery_charge_minimum_until is None
        case.controller.disengage.assert_awaited_once()
    finally:
        await case.coord.async_stop_battery_controls()


async def test_startup_recovery_releases_owned_charge_and_does_not_invent_fresh_minimum(confirmed_case):
    case = confirmed_case
    case.coord._battery_recovery = True
    # Durable cleanup ownership survives a restart; confirmation time does not.
    case.coord._battery_charge_started_at = None
    case.coord._battery_charge_minimum_until = None
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert not case.coord._grid_charge_engaged
        assert case.coord._battery_charge_minimum_until is None
        case.controller.disengage.assert_awaited_once()
        case.controller.engage.assert_not_awaited()
        case.controller.enable_prepared.assert_not_awaited()
    finally:
        await case.coord.async_stop_battery_controls()
