"""Fresh async retries retain candidates without retaining stale permissions."""
from datetime import timedelta
from math import sqrt
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.pv_excess_control import battery_planner as bp
from custom_components.pv_excess_control.models import InverterGridChargeConfig
from tests.test_battery_control_economics import economic_case  # noqa: F401


def future_case(factory):
    case = factory([.4, .1, .8], [0., 0., 0.], [0., 0., 2000.],
                   min_battery_soc=0., battery_grid_target_soc=100.)
    case.coord._battery_hold_ctl = MagicMock(confirmation_level="physical",
        engage=AsyncMock(), disengage=AsyncMock(), verify_engaged=AsyncMock())
    assert case.coord._refresh_battery_grid_plan(case.tariff, None) is None
    seed = case.coord._battery_grid_plan
    assert seed.valid and seed.grid_energy_kwh > 1.
    assert seed.slots[0].action != "charge"
    case.coord._battery_plan_key = None
    return case, seed


async def test_fresh_retry_retains_detached_candidate_while_publication_is_invalid(economic_case, monkeypatch):
    case, seed = future_case(economic_case)
    # A completed incumbent must survive even when a new bounded search has no
    # completed candidate of its own. Its balances are recalculated for new SoC.
    monkeypatch.setattr(bp, "_search_schedule", lambda *args, **kwargs: None)
    requests = []

    async def executor(function, *args):
        requests.append(function.__self__)
        if len(requests) == 2:
            assert case.coord._battery_grid_plan is None
            assert case.coord._current_battery_slot() is None
        result = function(*args)
        if len(requests) == 1:
            case.reading("sensor.soc", 10.5)
        return result

    case.coord.hass.async_add_executor_job = executor
    assert await case.coord._async_refresh_battery_grid_plan(case.tariff, None) is None
    assert len(requests) == 2
    current = case.coord._battery_grid_plan
    assert current.valid and 1. < current.grid_energy_kwh < seed.grid_energy_kwh
    assert requests[0].continuation_plan is seed
    assert requests[1].continuation_plan is seed
    assert seed.slots[0].soc_end == 10.
    assert current.slots[0].soc_end == pytest.approx(10.5)
    case.controller.engage.assert_not_awaited()


async def test_retained_retry_candidate_still_obeys_new_price_limit(economic_case, monkeypatch):
    case, seed = future_case(economic_case)
    monkeypatch.setattr(bp, "_search_schedule", lambda *args, **kwargs: None)
    requests = []

    async def executor(function, *args):
        requests.append(function.__self__)
        result = function(*args)
        if len(requests) == 1:
            case.coord.config_entry.data["battery_charge_price_threshold"] = .05
        return result

    case.coord.hass.async_add_executor_job = executor
    assert await case.coord._async_refresh_battery_grid_plan(case.tariff, None) is None
    assert len(requests) == 2 and requests[1].continuation_plan is seed
    assert case.coord._battery_grid_plan.grid_energy_kwh == 0.
    case.controller.engage.assert_not_awaited()


@pytest.mark.parametrize("stop", ["disable", "manual", "unload", "missing_soc", "missing_pv"])
async def test_retained_candidate_does_not_survive_safety_or_user_stop(economic_case, stop):
    case, _ = future_case(economic_case)
    requests = []

    async def executor(function, *args):
        requests.append(function.__self__)
        result = function(*args)
        if stop == "disable":
            case.coord.enabled = False
        elif stop == "manual":
            case.coord.force_charge = True
        elif stop == "unload":
            case.coord._battery_unloading = True
        else:
            case.reading("sensor.soc" if stop == "missing_soc" else "sensor.pv_power", "unavailable")
        return result

    case.coord.hass.async_add_executor_job = executor
    assert await case.coord._async_refresh_battery_grid_plan(case.tariff, None) is not None
    assert len(requests) == 1
    assert case.coord._battery_grid_plan is None
    assert case.coord._battery_plan_key is None
    case.controller.engage.assert_not_awaited()


async def test_repeated_input_changes_drop_candidate_after_the_bounded_retry(economic_case, monkeypatch):
    case, seed = future_case(economic_case)
    monkeypatch.setattr(bp, "_search_schedule", lambda *args, **kwargs: None)
    requests = []

    async def executor(function, *args):
        requests.append(function.__self__)
        result = function(*args)
        case.reading("sensor.soc", 10.+len(requests)*.5)
        return result

    case.coord.hass.async_add_executor_job = executor
    assert await case.coord._async_refresh_battery_grid_plan(case.tariff, None) == "planning_input_changed"
    assert len(requests) == 2 and requests[1].continuation_plan is seed
    assert case.coord._battery_grid_plan is None
    reason, next_request = case.coord._battery_planning_request(case.tariff, None)
    assert reason is None and next_request.continuation_plan is None


def phase_case(factory):
    case = factory([.5, .1, .1, .9, .8], [0.] * 5, [0., 0., 0., 1000., 1000.],
        battery_capacity=2., min_battery_soc=0., battery_grid_target_soc=100.,
        battery_grid_charge_power_w=1000., battery_max_charge_power_w=1000.,
        battery_max_discharge_default=1000., battery_roundtrip_efficiency=.85,
        battery_wear_cost_per_kwh=.03, battery_charge_price_threshold=.8,
        battery_grid_charge_minimum_minutes=5., grid_charge_engage_min_duration_minutes=5.)
    case.controller.config = InverterGridChargeConfig('switch.charge', 'on', 'off',
        mode_entity_id='select.mode', mode_engage_value='Forced', mode_disengage_value='Self',
        power_entity_id='number.charge_power', timeout_seconds=15.)
    case.reading('number.charge_power', 1000., min=0., max=1000., step=10., unit_of_measurement='W')
    case.coord._battery_hold_ctl = MagicMock(confirmation_level='physical',
        engage=AsyncMock(), disengage=AsyncMock(), verify_engaged=AsyncMock())
    assert case.coord._refresh_battery_grid_plan(case.tariff, None) is None
    previous = case.coord._battery_grid_plan
    assert previous.valid and previous.grid_target_soc > 99.
    first = next(s for s in previous.slots if s.action == 'charge')
    case.clock['now'] = first.start
    for entity, state in list(case.states.items()):
        case.reading(entity, state.state, **state.attributes)
    case.tariff.current_price = .1
    case.reading('sensor.price', .1)

    async def ready(when):
        case.clock['now'] = max(case.clock['now'], when)
        for entity, state in list(case.states.items()):
            case.reading(entity, state.state, **state.attributes)

    case.coord._wait_battery_charge_ready = ready
    return case, previous


async def test_future_plan_survives_prepare_retry_and_confirmed_enable(economic_case, monkeypatch):
    case, previous = phase_case(economic_case)
    monkeypatch.setattr(bp, '_search_schedule', lambda *args, **kwargs: None)
    requests = []
    revised = False

    async def executor(function, *args):
        nonlocal revised
        request = function.__self__
        requests.append(request)
        result = function(*args)
        if request.config.grid_charge_prepared and not revised:
            revised = True
            case.reading('sensor.soc', 10.5)
        return result

    case.coord.hass.async_add_executor_job = executor
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.coord._battery_control_state == 'charging'
        assert case.coord._grid_charge_engaged
        assert revised
        phases = {(r.config.grid_charge_prepared, r.config.grid_charge_active) for r in requests}
        assert phases >= {(False, False), (True, False), (False, True)}
        assert all(r.continuation_plan is not None for r in requests)
        # Fresh energy reduction rounds downward to a 10 W command. One final
        # interval's power quantum is a legitimate small target difference.
        quantum = max(s.charging_hours for s in previous.slots) * 10. / 1000 * sqrt(.85) / 2. * 100
        assert case.coord._battery_grid_plan.grid_target_soc >= previous.grid_target_soc - quantum
        assert case.coord._battery_charge_minimum_until == case.coord._battery_charge_started_at + timedelta(minutes=5)
        assert case.coord._battery_charge_deadline > case.coord._battery_charge_minimum_until
        case.controller.prepare.assert_awaited_once()
        case.controller.enable_prepared.assert_awaited_once()
        case.controller.verify_engaged.assert_awaited()
        case.controller.disengage.assert_not_awaited()
    finally:
        await case.coord.async_stop_battery_controls()


async def test_disable_during_preparation_never_enables_retained_plan(economic_case, monkeypatch):
    case, _ = phase_case(economic_case)
    monkeypatch.setattr(bp, '_search_schedule', lambda *args, **kwargs: None)

    async def disable(_power):
        case.coord.enabled = False

    case.controller.prepare.side_effect = disable
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        case.controller.prepare.assert_awaited_once()
        case.controller.enable_prepared.assert_not_awaited()
        assert not case.coord._grid_charge_engaged
        assert case.coord._battery_grid_plan is None
    finally:
        await case.coord.async_stop_battery_controls()
