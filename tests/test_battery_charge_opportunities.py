"""Generic useful charge opportunities around solar refills and later demand."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from math import sqrt

import pytest

from custom_components.pv_excess_control import battery_planner as bp
from custom_components.pv_excess_control.models import HourlyForecast, TariffWindow

START = datetime(2032, 2, 3, tzinfo=timezone.utc)
CONFIG = bp.BatteryPlanningConfig(
    capacity_kwh=2, reserve_soc=0, grid_target_soc=100,
    max_charge_power_w=2000, max_pv_charge_power_w=2500,
    max_discharge_power_w=2000, roundtrip_efficiency=.85,
    wear_cost_per_kwh=.02, charge_price_limit=.5, hold_supported=True,
    forced_charge_min_power_w=0, forced_charge_max_power_w=2500,
    forced_charge_power_step_w=10, grid_charge_startup_seconds=30,
    grid_charge_enable_seconds=15, grid_charge_minimum_minutes=5,
    grid_restart_delay_minutes=5,
)


@pytest.fixture
def bounded_windows_only(monkeypatch):
    """Require a supplemental completed candidate after other budgets run out."""
    for name in ('_EPISODE_COARSE_TRANSITION_BUDGET', '_EPISODE_PRIMARY_TRANSITION_BUDGET',
                 '_REFINEMENT_TRANSITION_BUDGET', '_QUANTITY_REFINEMENT_TRANSITION_BUDGET'):
        monkeypatch.setattr(bp, name, 0)


def solar_refill_inputs(start=START):
    tariffs, forecast = [], []
    for index in range(40):
        left = start + timedelta(minutes=15 * index)
        right = left + timedelta(minutes=15)
        price = .25 if index == 0 else .1 + (index - 16) * .01 if 16 <= index < 20 else .6
        watts = 3000 if 16 <= index < 32 else 0
        tariffs.append(TariffWindow(left, right, price, False))
        forecast.append(HourlyForecast(left, right, watts / 4000, watts))
    return tariffs, forecast, [500.] * 24


def assert_physical_plan(plan, config=CONFIG):
    """Independent AC-energy accounting and actuator/timing checks."""
    eta = sqrt(config.roundtrip_efficiency)
    ac_energy = 0.
    previous_episode_end = None
    episode = None
    for row in plan.slots:
        input_kwh = row.pv_charge_kwh + row.grid_charge_kwh
        ac_energy += input_kwh * config.roundtrip_efficiency - row.discharge_kwh
        assert ac_energy >= -1e-8
        assert ac_energy <= config.capacity_kwh * eta + 1e-8
        assert row.soc_end == pytest.approx(ac_energy / eta / config.capacity_kwh * 100)
        assert row.discharge_kwh <= max(0., row.load_kwh - row.pv_kwh) + 1e-8
        if row.action == 'charge':
            if episode is None or row.start >= episode.end:
                episode = bp.charge_episode_at(plan, row.start)
                assert episode.available_at - episode.start == timedelta(seconds=30)
                assert episode.end - episode.available_at >= timedelta(minutes=5, seconds=15)
                if previous_episode_end is not None:
                    assert episode.start - previous_episode_end >= timedelta(minutes=5)
                previous_episode_end = episode.end
            assert row.discharge_kwh == 0
            assert row.charge_power_w <= config.max_charge_power_w + 1e-8
            assert row.battery_charge_power_w <= config.native_charge_power_w + 1e-8
            assert row.battery_charge_power_w / 10 == pytest.approx(round(row.battery_charge_power_w / 10))


@pytest.mark.parametrize('offset_hours', [0, 7, 19])
def test_useful_earlier_charge_is_not_hidden_by_four_cheaper_solar_windows(
    bounded_windows_only, offset_hours,
):
    # Every cheapest window lies during a refill that already fills the store.
    # Only the earlier window can supply extra energy before that solar arrives.
    start = START + timedelta(hours=offset_hours)
    result = bp.build_battery_grid_plan(start, 0, *solar_refill_inputs(start), CONFIG)
    assert result.valid
    assert result.grid_energy_kwh > .4
    assert result.estimated_savings > .1
    assert result.slots[0].action == 'charge'
    assert all(row.action != 'charge' for row in result.slots[16:32])
    assert sum(row.pv_charge_kwh for row in result.slots) == pytest.approx(2 / sqrt(.85))
    assert result.slots[-1].soc_end == pytest.approx((2 - 1 / sqrt(.85)) / 2 * 100)
    assert_physical_plan(result)


def test_distinct_useful_period_gets_a_candidate_before_same_period_duplicates(
    bounded_windows_only,
):
    tariffs, forecast = [], []
    for index in range(40):
        left = START + timedelta(minutes=15 * index)
        right = left + timedelta(minutes=15)
        price = .1 if index < 4 else .25 if index == 28 else .6
        watts = 2500 if 8 <= index < 16 else 0
        tariffs.append(TariffWindow(left, right, price, False))
        forecast.append(HourlyForecast(left, right, watts / 4000, watts))
    profile = [100., 100., *([1000.] * 8), *([0.] * 14)]
    result = bp.build_battery_grid_plan(START, 0, tariffs, forecast, profile, CONFIG)
    # The first four cheap windows serve the same tiny demand before a solar
    # refill. The later, costlier window serves substantially more peak demand.
    assert result.estimated_savings > .11
    assert any(row.action == 'charge' and row.start == START + timedelta(hours=7)
               for row in result.slots)
    assert_physical_plan(result)


def test_pure_plan_has_no_advisory_scenario_unless_a_caller_attaches_one():
    result = bp.build_battery_grid_plan(START, 0, *solar_refill_inputs(), CONFIG)
    assert getattr(result, 'low_pv_plan', 'absent') is None
