"""Executable charge timing uses one validated configuration and actuator budget."""
from dataclasses import replace
from datetime import timedelta

import pytest

from custom_components.pv_excess_control.models import InverterGridChargeConfig
from tests.test_battery_control_economics import economic_case  # noqa: F401


def window_case(factory, **options):
    case = factory([.1, .4], [0, 0], [0, 2000], **options)
    case.coord.config_entry.data.pop('battery_grid_charge_minimum_minutes', None)
    case.coord.config_entry.data.update(options)
    case.controller.config = InverterGridChargeConfig(
        'switch.charge', 'on', 'off',
        mode_entity_id='select.mode', mode_engage_value='Forced',
        mode_disengage_value='Self', power_entity_id='number.charge_power',
    )
    return case


def request(case):
    return case.coord._battery_planning_request(case.tariff, None)


def test_default_minimum_is_five_minutes_plus_all_confirmation_channels(economic_case):
    case = window_case(economic_case)
    reason, value = request(case)
    assert reason is None
    assert value.config.grid_charge_minimum_minutes == 5
    assert value.config.grid_charge_startup_seconds == 30
    assert value.config.grid_charge_enable_seconds == 15
    assert value.config.grid_charge_prepared is False
    assert 'battery_grid_charge_minimum_minutes' not in case.coord.config_entry.data


def test_prepared_state_changes_first_start_without_erasing_future_startup_budget(economic_case):
    case = window_case(economic_case)
    _, before = request(case)
    case.coord._battery_charge_prepared = True
    reason, prepared = request(case)
    assert reason is None
    assert prepared.config.grid_charge_prepared is True
    assert prepared.config.grid_charge_startup_seconds == 30
    assert prepared.config.grid_charge_enable_seconds == 15
    assert prepared.key != before.key


def test_minimum_change_invalidates_planning_inputs(economic_case):
    case = window_case(economic_case)
    _, before = request(case)
    case.coord.config_entry.data['battery_grid_charge_minimum_minutes'] = 8
    reason, after = request(case)
    assert reason is None
    assert after.config.grid_charge_minimum_minutes == 8
    assert after.key != before.key


@pytest.mark.parametrize('mode,power,seconds', [(False, False, 0), (False, True, 15), (True, True, 30)])
def test_startup_budget_matches_configured_sequential_confirmation_steps(economic_case, mode, power, seconds):
    case = window_case(economic_case)
    case.controller.config = replace(case.controller.config,
        mode_entity_id='select.mode' if mode else None,
        power_entity_id='number.charge_power' if power else None,
    )
    assert case.coord._battery_charge_startup_seconds() == seconds
    assert case.coord._battery_charge_enable_seconds() == 15


@pytest.mark.parametrize('minimum', [True, float('nan'), float('inf'), -1, 61])
def test_invalid_minimum_fails_closed(economic_case, minimum):
    case = window_case(economic_case, battery_grid_charge_minimum_minutes=minimum)
    reason, value = request(case)
    assert reason == 'invalid_charge_minimum_duration'
    assert value is None


@pytest.mark.parametrize('timeout', [float('nan'), float('inf'), 0, -1])
def test_invalid_actuator_time_budget_fails_closed(economic_case, timeout):
    case = window_case(economic_case)
    case.controller.config = replace(case.controller.config, timeout_seconds=timeout)
    reason, value = request(case)
    assert reason == 'invalid_charge_startup_budget'
    assert value is None


def test_running_episode_keeps_one_fixed_minimum_deadline(economic_case):
    case = window_case(economic_case)
    case.coord._grid_charge_engaged = True
    until = case.clock['now'] + timedelta(minutes=5)
    case.coord._battery_charge_minimum_until = until
    reason, before = request(case)
    assert reason is None
    assert before.config.grid_charge_minimum_until == until
    case.clock['now'] += timedelta(seconds=30)
    reason, later = request(case)
    assert reason is None
    assert later.config.grid_charge_minimum_until == until
    case.clock['now'] = until + timedelta(seconds=1)
    for entity, old in list(case.states.items()):
        case.reading(entity, old.state, **old.attributes)
    reason, expired = request(case)
    assert reason is None
    assert expired.config.grid_charge_minimum_until is None
    assert expired.key != before.key


def test_inactive_control_does_not_inherit_an_old_minimum_deadline(economic_case):
    case = window_case(economic_case)
    case.coord._battery_charge_minimum_until = case.clock['now'] + timedelta(minutes=5)
    reason, value = request(case)
    assert reason is None
    assert value.config.grid_charge_minimum_until is None
