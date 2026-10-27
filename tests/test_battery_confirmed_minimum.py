"""Confirmed charging keeps its existing deadline under economic replanning."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.pv_excess_control import battery_planner as bp
from custom_components.pv_excess_control.models import HourlyForecast, TariffWindow

NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
DEADLINE = NOW + timedelta(minutes=3)
CONFIG = bp.BatteryPlanningConfig(
    capacity_kwh=2., reserve_soc=0., grid_target_soc=100.,
    max_charge_power_w=1000., max_pv_charge_power_w=1000., max_discharge_power_w=1000.,
    roundtrip_efficiency=.85, wear_cost_per_kwh=.03, charge_price_limit=.8,
    hold_supported=True, energy_step_kwh=.05,
    forced_charge_min_power_w=0., forced_charge_max_power_w=1000., forced_charge_power_step_w=10.,
    grid_restart_delay_minutes=5., grid_charge_minimum_minutes=5.,
    grid_charge_startup_seconds=30., grid_charge_enable_seconds=15.,
    grid_charge_active=True, grid_charge_minimum_until=DEADLINE,
)


def inputs(*, solar=None, price=.4):
    tariffs = [TariffWindow(NOW+timedelta(minutes=15*i), NOW+timedelta(minutes=15*(i+1)),
                           price if i == 0 else .1, False) for i in range(4)]
    forecast = [HourlyForecast(row.start, row.end, pv, pv*4000)
                for row, pv in zip(tariffs, solar or [0.]*4)]
    return tariffs, forecast, [500.]*24


def plan(*, now=NOW, soc=10., config=CONFIG, data=None, previous=None):
    tariffs, forecast, load = data or inputs()
    return bp.build_battery_grid_plan(now, soc, tariffs, forecast, load, config,
                                      continuation_plan=previous)


def assert_protected(result, deadline=DEADLINE):
    assert result.valid, result.reason
    protected = [row for row in result.slots if row.start < deadline]
    assert protected and all(row.action == 'charge' and row.grid_charge_kwh > 0 for row in protected)
    assert protected[-1].end == deadline


def test_economic_reversal_keeps_confirmed_minimum_with_honest_negative_savings():
    result = plan()
    assert_protected(result)
    assert result.estimated_savings < 0
    assert result.reason == 'confirmed_charge_minimum'
    assert result.slots[-1].soc_end == pytest.approx(0., abs=1e-8)


def test_repeated_replan_keeps_original_deadline_instead_of_extending_it():
    first = plan()
    later = NOW + timedelta(minutes=1)
    result = plan(now=later, previous=first)
    assert_protected(result)
    assert result.slots[0].start == later
    assert result.slots[0].end == DEADLINE


def test_exact_deadline_releases_economic_constraint():
    result = plan(now=DEADLINE)
    assert result.valid
    assert result.slots[0].action != 'charge'
    assert result.grid_energy_kwh == 0


@pytest.mark.parametrize('active,deadline', [(False, DEADLINE), (True, None)])
def test_unconfirmed_or_unprotected_episode_keeps_economic_stop(active, deadline):
    result = plan(config=replace(CONFIG, grid_charge_active=active, grid_charge_minimum_until=deadline))
    assert result.valid and result.grid_energy_kwh == 0
    assert result.slots[0].action != 'charge'


@pytest.mark.parametrize('reason', ['price_limit', 'target', 'capacity', 'solar', 'terminal', 'horizon'])
def test_protected_minimum_never_overrides_physical_or_economic_feasibility_guards(reason):
    config = CONFIG
    soc = 10.
    data = inputs()
    if reason == 'price_limit':
        config = replace(config, charge_price_limit=.2)
    elif reason == 'target':
        config = replace(config, grid_target_soc=soc)
    elif reason == 'capacity':
        soc = 100.
    elif reason == 'solar':
        config = replace(config, max_pv_charge_power_w=20000.)
        data = inputs(solar=[0., 5., 0., 0.])
    elif reason == 'terminal':
        data = (*data[:2], [0.]*24)
    elif reason == 'horizon':
        end = NOW + timedelta(minutes=2)
        data = ([TariffWindow(NOW, end, .1, False)], [HourlyForecast(NOW, end, 0., 0.)], [500.]*24)
    result = plan(soc=soc, config=config, data=data)
    assert not result.valid
    assert result.reason == 'confirmed_charge_minimum_infeasible'
    assert result.slots == () and result.grid_energy_kwh == 0


def test_missing_forecast_still_rejects_even_when_minimum_is_protected():
    tariffs, forecast, load = inputs()
    result = plan(data=(tariffs, forecast[1:], load))
    assert not result.valid and result.slots == ()
    assert result.reason == 'forecast_gap_or_overlap'


def test_expired_active_deadline_is_not_renewed_from_minimum_minutes():
    result = plan(config=replace(CONFIG, grid_charge_minimum_until=NOW-timedelta(seconds=1)))
    assert result.valid and result.grid_energy_kwh == 0


def test_completed_continuation_survives_exhausted_search_budgets(monkeypatch):
    previous = plan()
    assert_protected(previous)
    for name in ('_EPISODE_COARSE_TRANSITION_BUDGET', '_EPISODE_PRIMARY_TRANSITION_BUDGET',
                 '_REFINEMENT_TRANSITION_BUDGET', '_QUANTITY_REFINEMENT_TRANSITION_BUDGET',
                 '_CHARGE_WINDOW_TRANSITION_BUDGET', '_NATIVE_WINDOW_TRANSITION_BUDGET'):
        monkeypatch.setattr(bp, name, 0)
    result = plan(previous=previous)
    assert_protected(result)
    assert result.estimated_savings == pytest.approx(previous.estimated_savings)
    assert result.grid_energy_kwh == pytest.approx(previous.grid_energy_kwh)


def test_exhausted_search_cannot_return_an_unprotected_baseline(monkeypatch):
    for name in ('_EPISODE_COARSE_TRANSITION_BUDGET', '_EPISODE_PRIMARY_TRANSITION_BUDGET',
                 '_REFINEMENT_TRANSITION_BUDGET', '_QUANTITY_REFINEMENT_TRANSITION_BUDGET',
                 '_CHARGE_WINDOW_TRANSITION_BUDGET', '_NATIVE_WINDOW_TRANSITION_BUDGET'):
        monkeypatch.setattr(bp, name, 0)
    result = plan()
    assert not result.valid and result.reason == 'confirmed_charge_minimum_infeasible'
    assert result.slots == ()


def test_confirmed_deadline_uses_absolute_time_and_does_not_restart_full_minimum():
    offset_deadline = DEADLINE.astimezone(timezone(timedelta(hours=2)))
    result = plan(config=replace(CONFIG, grid_charge_minimum_until=offset_deadline,
                                 grid_charge_minimum_minutes=60.))
    assert_protected(result)
    assert result.slots[1].start == DEADLINE
    assert result.slots[1].action != 'charge'
