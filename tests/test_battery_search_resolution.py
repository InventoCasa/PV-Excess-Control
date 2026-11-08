"""Bounded multiwindow search quality with invented tariffs and household load."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from math import sqrt

import pytest

from custom_components.pv_excess_control import battery_planner as bp
from custom_components.pv_excess_control.models import HourlyForecast, TariffWindow


START = datetime(2030, 1, 1, 10, tzinfo=timezone.utc)
PRICES = (.8, .3, .8, .3, .18, .25, .4, .3, .3, .8, .8, .25, .3, .25, .4, .22)
LOADS = (1500, 700, 1100, 1500)
CONFIG = bp.BatteryPlanningConfig(
    capacity_kwh=4, reserve_soc=0, grid_target_soc=100,
    max_charge_power_w=2400, max_discharge_power_w=3000,
    roundtrip_efficiency=.85, wear_cost_per_kwh=.03,
    charge_price_limit=.4, hold_supported=True, horizon_hours=4,
    forced_charge_min_power_w=100, forced_charge_max_power_w=3000,
    forced_charge_power_step_w=10, grid_restart_delay_minutes=5,
    grid_charge_minimum_minutes=5, grid_charge_startup_seconds=30,
    grid_charge_enable_seconds=15,
)


def inputs():
    tariffs = [TariffWindow(START + timedelta(minutes=15 * index),
                           START + timedelta(minutes=15 * (index + 1)), price, False)
               for index, price in enumerate(PRICES)]
    forecast = [HourlyForecast(row.start, row.end, 0., 0.) for row in tariffs]
    return tariffs, forecast, [0.] * 10 + list(LOADS) + [0.] * 10


def evaluate(actions, purchases, offset):
    """Independent storage/cost accounting; all solar is zero in this fixture."""
    eta = sqrt(CONFIG.roundtrip_efficiency)
    energy = CONFIG.capacity_kwh * .1
    cost = 0.
    for index, (action, purchase, price) in enumerate(zip(actions, purchases, PRICES, strict=True)):
        hours = (900 - (offset if index == 0 else 0)) / 3600
        load = LOADS[index // 4] * hours / 1000
        energy += purchase * eta
        discharge = min(load, energy * eta) if action == "self_consumption" else 0.
        energy -= discharge / eta
        cost += (load - discharge + purchase) * price + discharge * CONFIG.wear_cost_per_kwh
        assert -1e-8 <= energy <= CONFIG.capacity_kwh + 1e-8
    return cost, energy


@pytest.fixture
def fine_search_exhausted(monkeypatch):
    # A dense production horizon can exhaust these budgets. Isolate that case
    # without making this small economic regression spend hundreds of thousands
    # of transitions merely to reach the same fallback paths.
    for name in ("_EPISODE_PRIMARY_TRANSITION_BUDGET", "_REFINEMENT_TRANSITION_BUDGET",
                 "_QUANTITY_REFINEMENT_TRANSITION_BUDGET"):
        monkeypatch.setattr(bp, name, 0)


@pytest.mark.parametrize("offset", [27, 61])
def test_completed_intermediate_search_keeps_useful_multiple_charge_windows(
    fine_search_exhausted, offset,
):
    # Three executable episodes use 2060 W, 2400/2000 W and 1030 W commands.
    # Holding their energy for the peaks costs less than the coarse search's
    # extra purchase in the earlier 30-cent interval.
    actions = ["self_consumption", "charge", "self_consumption", "hold", "charge", "charge",
               "self_consumption", "hold", "hold", "self_consumption", "self_consumption",
               "charge", "hold", "hold", "self_consumption", "self_consumption"]
    purchases = [0.] * 16
    for index, watts in ((1, 2060), (4, 2400), (11, 1030)):
        purchases[index] = watts * (900 - 30) / 3_600_000
    purchases[5] = 2000 / 4000
    reference_cost, reference_end = evaluate(actions, purchases, offset)
    native_cost, native_end = evaluate(["self_consumption"] * 16, [0.] * 16, offset)
    assert reference_end <= native_end + 1e-8
    assert native_cost - reference_cost > .48

    result = bp.build_battery_grid_plan(START + timedelta(seconds=offset), 10, *inputs(), CONFIG)
    actual_cost, actual_end = evaluate(
        [row.action for row in result.slots], [row.grid_charge_kwh for row in result.slots], offset,
    )
    assert result.valid
    assert actual_end <= native_end + 1e-8
    assert result.estimated_savings == pytest.approx(native_cost - actual_cost)
    # Bounded pruning may miss a few Wh; require the independently executable
    # schedule's cost within 0.01 cent, not continuous global optimality.
    assert actual_cost <= reference_cost + .0001
    for row in result.slots:
        if row.action == "charge":
            assert row.price <= CONFIG.charge_price_limit
            assert row.battery_charge_power_w <= CONFIG.max_charge_power_w + 1e-8
            assert row.battery_charge_power_w / 10 == pytest.approx(round(row.battery_charge_power_w / 10))
            episode = bp.charge_episode_at(result, row.start)
            assert episode.end - episode.available_at >= timedelta(minutes=5, seconds=15)


@pytest.mark.parametrize("budget", [0, 1, 40])
def test_exhausted_intermediate_searches_preserve_complete_incumbents(
    monkeypatch, fine_search_exhausted, budget,
):
    with monkeypatch.context() as disabled:
        disabled.setattr(bp, "_EPISODE_RESOLUTION_SEARCHES", ())
        incumbent = bp.build_battery_grid_plan(START, 10, *inputs(), CONFIG)
    monkeypatch.setattr(bp, "_EPISODE_RESOLUTION_SEARCHES", ((32, budget), (40, budget)))
    result = bp.build_battery_grid_plan(START, 10, *inputs(), CONFIG)
    assert result == incumbent
    assert result.slots[-1].end == START + timedelta(hours=4)


def test_intermediate_work_is_bounded_and_skips_existing_resolutions(monkeypatch):
    searches = []
    original = bp._search_schedule

    def record(*args, **kwargs):
        if kwargs.get("charge_window") is None:
            searches.append((args[6], args[7], kwargs["transition_budget"]))
        return original(*args, **kwargs)

    monkeypatch.setattr(bp, "_search_schedule", record)
    config = replace(CONFIG, capacity_kwh=10)
    result = bp.build_battery_grid_plan(START, 10, *inputs(), config)
    assert result.valid
    intermediate = [row for row in searches if .1 < row[0] < 1.25]
    assert 0 < len(intermediate) <= 2
    assert sum(row[2] for row in intermediate) <= 250_000

    searches.clear()
    bp.build_battery_grid_plan(START, 10, *inputs(), replace(CONFIG, energy_step_kwh=.5))
    assert all(row[0] not in (.125, .1) for row in searches)
