"""Small exhaustive comparisons using invented hourly loads and prices.

The reference enumerates only five charge powers over four hours (625 schedules).
It measures battery contents as deliverable AC energy and does not call planner
transitions, pruning, or search helpers. This is a bounded quality check, not a
claim that the production search solves every continuous schedule optimally.
"""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from itertools import product
from math import sqrt

import pytest

from custom_components.pv_excess_control.battery_planner import (
    BatteryPlanningConfig,
    build_battery_grid_plan,
)
from custom_components.pv_excess_control.models import HourlyForecast, TariffWindow


START = datetime(2030, 1, 1, tzinfo=timezone.utc)
CONFIG = BatteryPlanningConfig(
    capacity_kwh=2,
    reserve_soc=0,
    grid_target_soc=100,
    max_charge_power_w=400,
    max_discharge_power_w=1000,
    roundtrip_efficiency=0.85,
    wear_cost_per_kwh=0.03,
    charge_price_limit=0.35,
)


@dataclass(frozen=True)
class _Evaluation:
    cost: float
    stored_kwh: float
    solar_input_kwh: tuple[float, ...]
    discharge_kwh: tuple[float, ...]
    soc: tuple[float, ...]


def _evaluate(prices, loads, solar, grid, config, initial_soc):
    """Evaluate a fixed no-hold schedule, with one hour per interval.

    Deliverable AC energy increases by input times roundtrip efficiency and
    decreases directly by the load supplied. Only the initial energy and storage
    limits need the one-way conversion factor. Invalid purchases return None.
    """
    eta = sqrt(config.roundtrip_efficiency)
    available = config.capacity_kwh * initial_soc / 100 * eta
    reserve = config.capacity_kwh * config.reserve_soc / 100 * eta
    capacity = config.capacity_kwh * eta
    grid_ceiling = capacity * config.grid_target_soc / 100
    cost = 0.0
    solar_inputs, discharged, soc = [], [], []
    for price, demand, production, purchase in zip(prices, loads, solar, grid, strict=True):
        pv_input = min(
            max(0.0, production - demand),
            config.native_charge_power_w / 1000,
            max(0.0, capacity - available) / config.roundtrip_efficiency,
        )
        if purchase < 0:
            return None
        if purchase > 1e-8 and (
            price > config.charge_price_limit
            or purchase > config.max_charge_power_w / 1000 + 1e-8
            or purchase + pv_input > config.native_charge_power_w / 1000 + 1e-8
            or available + (purchase + pv_input) * config.roundtrip_efficiency > grid_ceiling + 1e-8
        ):
            return None
        available += (pv_input + purchase) * config.roundtrip_efficiency
        deficit = max(0.0, demand - production)
        supplied = 0.0 if purchase > 1e-8 else min(
            deficit, config.max_discharge_power_w / 1000, max(0.0, available - reserve),
        )
        available -= supplied
        cost += (deficit - supplied + purchase) * price + supplied * config.wear_cost_per_kwh
        solar_inputs.append(pv_input)
        discharged.append(supplied)
        soc.append(available / capacity * 100)
    return _Evaluation(cost, available / eta, tuple(solar_inputs), tuple(discharged), tuple(soc))


def _reference(prices, loads, solar, config, initial_soc):
    baseline = _evaluate(prices, loads, solar, [0.0] * 4, config, initial_soc)
    best_cost = baseline.cost
    for grid in product((0.0, 0.1, 0.2, 0.3, 0.4), repeat=4):
        candidate = _evaluate(prices, loads, solar, grid, config, initial_soc)
        if candidate is None or candidate.stored_kwh > baseline.stored_kwh + 1e-8:
            continue
        if any(actual + 1e-8 < native for actual, native in zip(
            candidate.solar_input_kwh, baseline.solar_input_kwh, strict=True,
        )):
            continue
        best_cost = min(best_cost, candidate.cost)
    return baseline, best_cost


def _checked_plan(prices, loads, solar, *, config=CONFIG, initial_soc=5):
    tariffs, forecast = [], []
    profile = [0.0] * 24
    for index in range(4):
        start = START + timedelta(hours=index)
        end = start + timedelta(hours=1)
        tariffs.append(TariffWindow(start, end, prices[index], False))
        forecast.append(HourlyForecast(start, end, solar[index], solar[index] * 1000))
        profile[index] = loads[index] * 1000
    result = build_battery_grid_plan(START, initial_soc, tariffs, forecast, profile, config)
    assert result.valid
    assert len(result.slots) == 4
    grid = [slot.grid_charge_kwh for slot in result.slots]
    actual = _evaluate(prices, loads, solar, grid, config, initial_soc)
    assert actual is not None
    baseline, reference_cost = _reference(prices, loads, solar, config, initial_soc)
    assert actual.stored_kwh <= baseline.stored_kwh + 1e-8
    assert all(actual_pv + 1e-8 >= native_pv for actual_pv, native_pv in zip(
        actual.solar_input_kwh, baseline.solar_input_kwh, strict=True,
    ))
    assert result.grid_energy_kwh == pytest.approx(sum(grid))
    assert result.estimated_savings == pytest.approx(baseline.cost - actual.cost, abs=1e-8)
    for index, slot in enumerate(result.slots):
        assert slot.soc_end == pytest.approx(actual.soc[index])
        assert slot.pv_charge_kwh == pytest.approx(actual.solar_input_kwh[index])
        assert slot.discharge_kwh == pytest.approx(actual.discharge_kwh[index])
        assert slot.action == ("charge" if grid[index] > 1e-8 else "self_consumption")
        assert slot.soc_end >= min(initial_soc, config.reserve_soc) - 1e-8
        assert slot.soc_end <= 100 + 1e-8
    return result, actual.cost, reference_cost


@pytest.mark.parametrize("middle_price", [0.33, 0.34, 0.35])
@pytest.mark.parametrize("peak_price", [0.38, 0.39, 0.40])
def test_small_storage_bucket_keeps_energy_that_avoids_an_expensive_purchase(middle_price, peak_price):
    # Buying 0.4 in each cheaper hour beats buying 0.3, 0.1, then 0.4.
    # Both buy 0.8 kWh, but the former allows native supply in the middle hour.
    result, actual_cost, reference_cost = _checked_plan(
        [0.23, middle_price, 0.17, peak_price], [0.2, 0.1, 0.1, 0.8], [0.0] * 4,
    )
    assert result.estimated_savings > 0
    assert actual_cost <= reference_cost + 1e-8


@pytest.mark.parametrize(("prices", "loads", "solar", "config", "initial_soc"), [
    ([0.18, 0.12, 0.34, 0.45], [0.1, 0.2, 0.3, 0.8], [0.0] * 4, CONFIG, 0),
    ([0.12, 0.28, 0.45, 0.39], [0.1, 0.1, 0.6, 0.4], [0.0, 0.5, 0.0, 0.0],
     replace(CONFIG, max_pv_charge_power_w=800), 0),
    ([-0.05, 0.17, 0.39, 0.45], [0.1, 0.1, 0.5, 0.8], [0.0] * 4, CONFIG, 0),
    ([0.12, 0.28, 0.45, 0.39], [0.1, 0.1, 0.6, 0.4], [0.0] * 4,
     replace(CONFIG, reserve_soc=10, grid_target_soc=30), 20),
])
def test_small_reference_comparison_respects_physics_and_reports_real_costs(prices, loads, solar, config, initial_soc):
    _, actual_cost, reference_cost = _checked_plan(
        prices, loads, solar, config=config, initial_soc=initial_soc,
    )
    # Continuous demand endpoints may beat the coarser reference enumeration.
    assert actual_cost <= reference_cost + 1e-8


def test_cheaper_plan_cannot_replace_native_solar_uptake():
    result, actual_cost, reference_cost = _checked_plan(
        [-0.05, 0.17, 0.40, 0.40], [0.0, 0.0, 0.3, 0.4], [0.0, 2.0, 0.0, 0.0],
        config=replace(CONFIG, capacity_kwh=1, max_pv_charge_power_w=2000), initial_soc=0,
    )
    assert result.grid_energy_kwh == 0
    assert actual_cost == pytest.approx(reference_cost)


def test_negative_prices_cannot_buy_energy_left_unused_at_the_horizon():
    result, actual_cost, reference_cost = _checked_plan(
        [-0.05, 0.17, 0.40, 0.40], [0.0] * 4, [0.0] * 4, initial_soc=0,
    )
    assert result.grid_energy_kwh == 0
    assert actual_cost == pytest.approx(reference_cost)


@pytest.mark.parametrize("budget", [0, 1, 40])
def test_refinement_budget_exhaustion_keeps_a_complete_feasible_plan(monkeypatch, budget):
    from custom_components.pv_excess_control import battery_planner

    # This fixture has a profitable incumbent even when refinement is abandoned.
    monkeypatch.setattr(battery_planner, "_REFINEMENT_TRANSITION_BUDGET", budget)
    result, actual_cost, _ = _checked_plan(
        [0.23, 0.34, 0.17, 0.39], [0.2, 0.1, 0.1, 0.8], [0.0] * 4,
    )
    complete_schedule = _evaluate(
        [0.23, 0.34, 0.17, 0.39], [0.2, 0.1, 0.1, 0.8], [0.0] * 4,
        [0.3, 0.1, 0.4, 0.0], CONFIG, 5,
    )
    assert result.grid_energy_kwh > 0
    assert result.slots[0].start == START
    assert result.slots[-1].end == START + timedelta(hours=4)
    assert actual_cost <= complete_schedule.cost + 1e-8


def test_refinement_cannot_replace_a_cheaper_completed_incumbent(monkeypatch):
    from custom_components.pv_excess_control import battery_planner

    prices = [0.12, 0.28, 0.17, 0.39]
    loads = [0.0, 0.3, 0.0, 0.8]
    solar = [0.2, 0.1, 0.1, 0.0]
    with monkeypatch.context() as disabled:
        disabled.setattr(battery_planner, "_REFINEMENT_TRANSITION_BUDGET", 0)
        incumbent, incumbent_cost, _ = _checked_plan(prices, loads, solar)
    improved, improved_cost, _ = _checked_plan(prices, loads, solar)
    # In this case regrouping storage states originally lost an exact demand
    # endpoint. A completed incumbent remains an available candidate regardless
    # of whether later refinement is better, worse, or interrupted.
    assert incumbent.estimated_savings > 0
    assert improved.estimated_savings + 1e-8 >= incumbent.estimated_savings
    assert improved_cost <= incumbent_cost + 1e-8
