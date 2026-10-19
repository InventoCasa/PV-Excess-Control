"""Synthetic regressions for avoidable current holds in a bounded search."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from math import sqrt

import pytest

from custom_components.pv_excess_control import battery_planner as bp
from custom_components.pv_excess_control.models import HourlyForecast, TariffWindow


START = datetime(2030, 1, 1, tzinfo=timezone.utc)
CONFIG = bp.BatteryPlanningConfig(
    capacity_kwh=2, reserve_soc=0, grid_target_soc=100,
    max_charge_power_w=2000, max_discharge_power_w=2000,
    roundtrip_efficiency=.85, wear_cost_per_kwh=.03,
    charge_price_limit=0, hold_supported=True,
)


def inputs(prices, first_minutes, profile):
    edges = [START, START + timedelta(minutes=first_minutes)]
    for _ in prices[1:]:
        edges.append(edges[-1] + timedelta(minutes=15))
    tariffs = [TariffWindow(left, right, price, False)
               for left, right, price in zip(edges, edges[1:], prices)]
    solar = [HourlyForecast(START, edges[-1], 0, 0)]
    return tariffs, solar, [*profile, *([300.] * (24 - len(profile)))]


def check_physics(plan, soc, config=CONFIG):
    """Independently account for the zero-solar, zero-purchase fixtures."""
    energy = soc / 100 * config.capacity_kwh
    eta = sqrt(config.roundtrip_efficiency)
    cost = 0.
    for slot in plan.slots:
        hours = (slot.end - slot.start).total_seconds() / 3600
        discharge = (min(slot.load_kwh, config.max_discharge_power_w * hours / 1000,
                         max(0., energy - config.capacity_kwh * config.reserve_soc / 100) * eta)
                     if slot.action == 'self_consumption' else 0.)
        energy -= discharge / eta
        assert slot.grid_charge_kwh == 0
        assert slot.pv_charge_kwh == 0
        assert slot.discharge_kwh == pytest.approx(discharge)
        assert slot.soc_end == pytest.approx(energy / config.capacity_kwh * 100)
        cost += (slot.load_kwh - discharge) * slot.price + discharge * config.wear_cost_per_kwh
    return cost, energy


def test_short_current_peak_is_not_held_for_a_cheaper_suffix():
    args = inputs([.50, .40, .30, .35, .25, .48, .25], 1.5, [300, 1100])
    result = bp.build_battery_grid_plan(START, 20, *args, CONFIG)
    assert result.valid
    assert result.slots[0].action == 'self_consumption'
    assert check_physics(result, 20)[1] == pytest.approx(0)


def test_equal_energy_exchange_keeps_more_expensive_future_demand_covered():
    # Just releasing now would consume energy needed at .45. Exchange the same
    # amount with the planned .25 discharge instead, retaining that later use.
    args = inputs([.40, .25, .30, .25, .45, .45], 2, [600, 600])
    result = bp.build_battery_grid_plan(START, 30, *args, CONFIG)
    assert result.valid
    assert result.slots[0].action == 'self_consumption'
    cost, terminal = check_physics(result, 30)
    assert terminal == pytest.approx(0)
    assert any(slot.action == 'hold' and slot.price == .25 for slot in result.slots)
    assert any(slot.discharge_kwh > 0 and slot.price == .45 for slot in result.slots)
    assert cost < .12


def test_useful_current_hold_survives_a_more_expensive_future_price():
    args = inputs([.30, .60], 15, [1000])
    result = bp.build_battery_grid_plan(START, 10, *args, CONFIG)
    assert result.slots[0].action == 'hold'
    assert result.slots[-1].discharge_kwh > 0
    check_physics(result, 10)


def unrefined_context(monkeypatch):
    """Capture a real completed incumbent before the additional checks."""
    captured = []

    def remember(*args):
        captured.append(args)
        return args[4]

    with monkeypatch.context() as context:
        context.setattr(bp, '_improve_current_hold', remember)
        bp.build_battery_grid_plan(
            START, 20, *inputs([.50, .40, .30, .35, .25, .48, .25], 1.5, [300, 1100]), CONFIG,
        )
    return captured[0]


@pytest.mark.parametrize('phase', ['grid_charge_active', 'grid_charge_prepared'])
def test_current_charge_phase_is_not_rewritten(monkeypatch, phase):
    timeline, config, eta, initial, incumbent, solar, bounds = unrefined_context(monkeypatch)
    assert bp._slots(incumbent)[0].action == 'hold'
    changed_phase = replace(config, **{phase: True})
    assert bp._improve_current_hold(timeline, changed_phase, eta, initial, incumbent, solar, bounds) is incumbent


def test_future_charge_trajectory_is_not_rewritten(monkeypatch):
    timeline, config, eta, initial, incumbent, solar, bounds = unrefined_context(monkeypatch)
    # Scope guard also applies when a completed plan contains a future command.
    charged = replace(incumbent, slot=replace(incumbent.slot, action='charge', grid_charge_kwh=.1))
    assert bp._improve_current_hold(timeline, config, eta, initial, charged, solar, bounds) is charged


def test_replays_have_a_fixed_linear_transition_budget(monkeypatch):
    args = unrefined_context(monkeypatch)
    transition = bp._transition
    calls = []

    def counted(*args, **kwargs):
        calls.append(None)
        return transition(*args, **kwargs)

    monkeypatch.setattr(bp, '_transition', counted)
    result = bp._improve_current_hold(*args)
    assert result.cost < args[4].cost
    assert len(calls) <= (1 + bp._HOLD_EXCHANGE_CANDIDATE_LIMIT) * (len(args[0]) + 1)


def test_consumer_discharge_limit_and_reserve_survive_exchanges():
    args = inputs([.40, .25, .30, .25, .45, .45], 2, [600, 600])
    config = replace(CONFIG, reserve_soc=10)
    limit_end = START + timedelta(minutes=17)
    commitment = bp.BatteryLoadCommitment(START, limit_end, 0, max_discharge_power_w=100)
    result = bp.build_battery_grid_plan(START, 40, *args, config, load_commitments=(commitment,))
    for slot in result.slots:
        assert slot.soc_end >= 10 - 1e-8
        if slot.start < limit_end:
            assert slot.discharge_kwh <= (slot.end - slot.start).total_seconds() * 100 / 3_600_000 + 1e-8
    assert result.grid_energy_kwh == 0
