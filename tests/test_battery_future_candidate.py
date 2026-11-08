"""Future purchase schedules remain candidates under fresh economic inputs."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from math import sqrt
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.pv_excess_control import battery_planner as bp
from custom_components.pv_excess_control.battery_continuation import replay_continuation
from custom_components.pv_excess_control.models import HourlyForecast, TariffWindow
from tests.test_battery_control_economics import economic_case  # noqa: F401


NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
CONFIG = bp.BatteryPlanningConfig(
    capacity_kwh=2., reserve_soc=0., grid_target_soc=100.,
    max_charge_power_w=1000., max_discharge_power_w=1000.,
    roundtrip_efficiency=.85, wear_cost_per_kwh=.03,
    charge_price_limit=.8, hold_supported=True,
    forced_charge_min_power_w=0., forced_charge_max_power_w=1000.,
    forced_charge_power_step_w=10., grid_restart_delay_minutes=5.,
    grid_charge_minimum_minutes=5., grid_charge_startup_seconds=30.,
    grid_charge_enable_seconds=15.,
)


def inputs():
    tariffs = [TariffWindow(NOW + timedelta(hours=i), NOW + timedelta(hours=i + 1), price, False)
               for i, price in enumerate((.5, .1, .2, .95, .9, .25))]
    forecast = [HourlyForecast(row.start, row.end, 0., 0.) for row in tariffs]
    return tariffs, forecast, [0., 0., 0., 800., 800., 0.] + [0.] * 18


@pytest.fixture
def previous_future_plan():
    result = bp.build_battery_grid_plan(NOW, 10., *inputs(), CONFIG)
    assert result.valid and result.grid_energy_kwh > 1.
    assert result.slots[0].action != "charge"
    return result


def candidate(previous, *, now=NOW, soc=10., config=CONFIG, data=None):
    tariffs, forecast, load = data or inputs()
    timeline = bp._timeline(now, tariffs, forecast, load, config.horizon_hours,
                            grid_charge_not_before=config.grid_charge_not_before)
    initial = bp._Node(soc * config.capacity_kwh / 100, 0., 0., None, None,
                       charge_not_before=config.grid_charge_not_before)
    return replay_continuation(timeline, config, sqrt(config.roundtrip_efficiency), initial, previous)


def test_idle_future_schedule_replays_with_fresh_balances_and_preserves_source(previous_future_plan):
    original = deepcopy(previous_future_plan)
    result = candidate(previous_future_plan, now=NOW + timedelta(seconds=30))
    assert result is not None
    assert result.grid_energy == pytest.approx(previous_future_plan.grid_energy_kwh)
    assert result.energy == pytest.approx(0., abs=1e-8)
    assert bp._slots(result)[0].start == NOW + timedelta(seconds=30)
    assert previous_future_plan == original


def test_idle_previous_schedule_survives_exhausted_new_searches(previous_future_plan, monkeypatch):
    monkeypatch.setattr(bp, "_EPISODE_RESOLUTION_SEARCHES", ())
    for name in ("_EPISODE_COARSE_TRANSITION_BUDGET", "_EPISODE_PRIMARY_TRANSITION_BUDGET",
                 "_REFINEMENT_TRANSITION_BUDGET", "_QUANTITY_REFINEMENT_TRANSITION_BUDGET",
                 "_CHARGE_WINDOW_TRANSITION_BUDGET", "_NATIVE_WINDOW_TRANSITION_BUDGET"):
        monkeypatch.setattr(bp, name, 0)
    result = bp.build_battery_grid_plan(NOW, 10., *inputs(), CONFIG,
                                      continuation_plan=previous_future_plan)
    assert result.grid_energy_kwh == pytest.approx(previous_future_plan.grid_energy_kwh)
    assert result.estimated_savings == pytest.approx(previous_future_plan.estimated_savings)


@pytest.mark.parametrize("change", ["soc", "demand"])
def test_small_balance_change_reduces_future_purchase_without_losing_schedule(previous_future_plan, change):
    tariffs, forecast, load = inputs()
    soc = 10.
    if change == "soc":
        soc = 10.5
    else:
        load[4] -= 10.
    original = deepcopy(previous_future_plan)
    result = candidate(previous_future_plan, soc=soc, data=(tariffs, forecast, load))
    assert result is not None
    assert 1. < result.grid_energy < previous_future_plan.grid_energy_kwh
    assert result.energy == pytest.approx(0., abs=1e-8)
    assert previous_future_plan == original
    for row in bp._slots(result):
        if row.grid_charge_kwh > 0:
            assert row.battery_charge_power_w / 10. == pytest.approx(round(row.battery_charge_power_w / 10.))
    reduced = bp.BatteryGridPlan(bp._slots(result), result.grid_energy, 0., "economic_plan", 100.)
    repeated = candidate(reduced, soc=soc, data=(tariffs, forecast, load))
    assert repeated is not None
    assert repeated.grid_energy == pytest.approx(result.grid_energy)
    assert repeated.cost == pytest.approx(result.cost)


def test_no_purchase_plan_is_not_a_future_charge_candidate():
    tariffs, forecast, load = inputs()
    previous = bp.build_battery_grid_plan(NOW, 10.,
        [replace(row, price=.1) for row in tariffs], forecast, load, CONFIG)
    assert previous.grid_energy_kwh == 0.
    assert candidate(previous) is None


def test_finished_purchase_plan_is_not_a_future_charge_candidate(previous_future_plan):
    assert candidate(previous_future_plan, now=NOW + timedelta(hours=5), soc=0.) is None


def test_old_purchase_outside_shortened_horizon_is_not_a_future_charge_candidate(previous_future_plan):
    tariffs, forecast, load = inputs()
    assert candidate(previous_future_plan, data=(tariffs[:1], forecast[:1], load)) is None


@pytest.mark.parametrize("change", [
    "price", "power", "actuator_max", "actuator_step", "target", "restart", "demand", "solar",
])
def test_changed_inputs_can_exclude_future_trajectory(previous_future_plan, change):
    tariffs, forecast, load = inputs()
    config = CONFIG
    if change == "price":
        config = replace(config, charge_price_limit=.05)
    elif change == "power":
        config = replace(config, max_charge_power_w=100.)
    elif change == "actuator_max":
        config = replace(config, forced_charge_max_power_w=100.)
    elif change == "actuator_step":
        config = replace(config, forced_charge_power_step_w=300.)
    elif change == "target":
        config = replace(config, grid_target_soc=10.)
    elif change == "restart":
        config = replace(config, grid_charge_not_before=NOW + timedelta(hours=3))
    elif change == "demand":
        load = [0.] * 24
    elif change == "solar":
        forecast[2] = replace(forecast[2], expected_kwh=3., expected_watts=3000.)
    assert candidate(previous_future_plan, config=config, data=(tariffs, forecast, load)) is None


def test_price_reversal_recalculates_economics_instead_of_pinning_old_plan(previous_future_plan):
    tariffs, forecast, load = inputs()
    tariffs = [replace(row, price=.7 if row.start.hour < 3 else .1) for row in tariffs]
    result = bp.build_battery_grid_plan(NOW, 10., tariffs, forecast, load, CONFIG,
                                      continuation_plan=previous_future_plan)
    assert result.valid
    assert result.grid_energy_kwh == 0.


def test_future_reuse_cannot_restart_an_old_current_charge_without_confirmation(previous_future_plan):
    first = next(row for row in previous_future_plan.slots if row.action == "charge")
    assert candidate(previous_future_plan, now=first.start) is None


def test_idle_coordinator_offers_previous_future_plan_for_fresh_revalidation(economic_case):
    case = economic_case([.5, .1, .9], [0., 0., 0.], [0., 0., 2000.],
        min_battery_soc=0., battery_grid_target_soc=100.)
    case.coord._battery_hold_ctl = MagicMock(confirmation_level="physical",
        engage=AsyncMock(), disengage=AsyncMock(), verify_engaged=AsyncMock())
    assert case.coord._refresh_battery_grid_plan(case.tariff, None) is None
    previous = case.coord._battery_grid_plan
    assert previous.grid_energy_kwh > 0 and previous.slots[0].action != "charge"
    assert not case.coord._grid_charge_engaged
    reason, request = case.coord._battery_planning_request(case.tariff, None)
    assert reason is None
    assert request.continuation_plan is previous
