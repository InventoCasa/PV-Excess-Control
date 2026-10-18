"""Bounded charge-window candidates with invented loads and quarter-hour prices."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from math import sqrt

import pytest

from custom_components.pv_excess_control import battery_planner as bp
from custom_components.pv_excess_control.models import HourlyForecast, TariffWindow


START = datetime(2030, 1, 1, tzinfo=timezone.utc)
CONFIG = bp.BatteryPlanningConfig(
    capacity_kwh=4, reserve_soc=0, grid_target_soc=100,
    max_charge_power_w=2000, max_pv_charge_power_w=2500,
    max_discharge_power_w=2000, roundtrip_efficiency=.85,
    wear_cost_per_kwh=.02, charge_price_limit=.5, hold_supported=True,
    forced_charge_min_power_w=0, forced_charge_max_power_w=2500,
    forced_charge_power_step_w=10, grid_charge_startup_seconds=30,
    grid_charge_enable_seconds=15, grid_charge_minimum_minutes=5,
    grid_restart_delay_minutes=5,
)
PRICES = (
    .250, .317, .315, .308, .305, .310, .308, .316, .306, .310,
    .312, .318, .310, .306, .315, .312, .305, .318, .320, .316,
    .318, .306, .315, .318, .314, .309, .302, .309, .333, .342,
    .344, .329, .341, .323, .339, .331, .315, .337, .327, .340,
)
SOLAR = (
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, .033, .068, .099, .135,
    .170, .176, .192, .251, .182, .196, .166, .135, .090, .069,
    .030, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
)
LOADS = (300, 600, 500, 400, 500, 300, 400, 400, 400, 400)


def inputs():
    tariffs = [TariffWindow(START + timedelta(minutes=15 * i),
                           START + timedelta(minutes=15 * (i + 1)), price, False)
               for i, price in enumerate(PRICES)]
    solar = [HourlyForecast(row.start, row.end, pv, pv * 4000)
             for row, pv in zip(tariffs, SOLAR, strict=True)]
    return tariffs, solar, [*LOADS, *([0.] * 14)]


def evaluate(actions, grid):
    """Independent AC/storage accounting for this complete quarter-hour fixture."""
    eta = sqrt(CONFIG.roundtrip_efficiency)
    stored = cost = 0.
    captured = []
    for index, (action, purchase, pv, price) in enumerate(zip(actions, grid, SOLAR, PRICES, strict=True)):
        load = LOADS[index // 4] / 4000
        solar = min(max(0., pv - load), CONFIG.native_charge_power_w / 4000,
                    max(0., CONFIG.capacity_kwh - stored) / eta)
        stored += (solar + purchase) * eta
        deficit = max(0., load - pv)
        discharge = (min(deficit, CONFIG.max_discharge_power_w / 4000, stored * eta)
                     if action == 'self_consumption' else 0.)
        stored -= discharge / eta
        cost += (deficit - discharge + purchase) * price + discharge * CONFIG.wear_cost_per_kwh
        captured.append(solar)
        assert stored <= CONFIG.capacity_kwh + 1e-8
    return cost, stored, captured


def test_executable_cheap_window_survives_competing_partial_charge_paths():
    # One full-power purchase is held through moderate prices and released at
    # the later peak. The interleaved broad search previously lost this path.
    holds = {*range(1, 14), *range(24, 29), 31}
    actions = ['charge' if i == 0 else 'hold' if i in holds else 'self_consumption'
               for i in range(40)]
    grid = [CONFIG.max_charge_power_w * (15 * 60 - 30) / 3_600_000, *([0.] * 39)]
    reference_cost, final_energy, captured = evaluate(actions, grid)
    native_cost, native_final, native_capture = evaluate(['self_consumption'] * 40, [0.] * 40)
    assert final_energy <= native_final + 1e-8
    assert captured == pytest.approx(native_capture)
    assert native_cost - reference_cost > .01

    result = bp.build_battery_grid_plan(START, 0, *inputs(), CONFIG)
    actual_cost, final_energy, captured = evaluate(
        [row.action for row in result.slots], [row.grid_charge_kwh for row in result.slots],
    )
    assert result.valid
    assert final_energy <= native_final + 1e-8
    assert captured == pytest.approx(native_capture)
    assert result.estimated_savings == pytest.approx(native_cost - actual_cost)
    assert actual_cost <= reference_cost + 1e-8
    episode = bp.charge_episode_at(result, START)
    assert episode.available_at == START + timedelta(seconds=30)
    assert episode.end - episode.available_at >= timedelta(minutes=5, seconds=15)
    assert episode.power_w / 10 == pytest.approx(round(episode.power_w / 10))
    assert episode.power_w <= CONFIG.max_charge_power_w


@pytest.fixture
def windows_only(monkeypatch):
    """Make the new completed candidates carry the regression's full result."""
    for name in ('_EPISODE_COARSE_TRANSITION_BUDGET', '_EPISODE_PRIMARY_TRANSITION_BUDGET',
                 '_REFINEMENT_TRANSITION_BUDGET', '_QUANTITY_REFINEMENT_TRANSITION_BUDGET'):
        monkeypatch.setattr(bp, name, 0)


def simple_plan(periods, *, config=CONFIG, load=500, solar=None):
    tariffs, forecast = [], []
    start = START
    for index, (minutes, price) in enumerate(periods):
        end = start + timedelta(minutes=minutes)
        pv = solar[index] if solar is not None else 0.
        tariffs.append(TariffWindow(start, end, price, False))
        forecast.append(HourlyForecast(start, end, pv, pv * 60000 / minutes))
        start = end
    return bp.build_battery_grid_plan(START, 0, tariffs, forecast, [load] * 24, config)


def test_fixed_window_crosses_real_boundaries_to_complete_minimum_duration(windows_only):
    result = simple_plan([(3, .1), (3, .12), (60, .7)])
    episode = bp.charge_episode_at(result, START)
    assert result.valid and result.grid_energy_kwh > 0
    assert episode.end == START + timedelta(minutes=6)
    assert episode.available_at == START + timedelta(seconds=30)
    assert episode.end - episode.available_at >= timedelta(minutes=5, seconds=15)
    assert all(row.action == 'charge' for row in result.slots[:2])


def test_fixed_window_respects_initial_restart_deadline(windows_only):
    eligible = START + timedelta(minutes=6)
    result = simple_plan([(15, .1), (60, .7)], config=replace(CONFIG, grid_charge_not_before=eligible))
    assert result.grid_energy_kwh > 0
    assert result.slots[0].action != 'charge'
    assert bp.charge_episode_at(result, eligible).start == eligible


def test_fixed_window_cannot_extend_through_a_price_ceiling(windows_only):
    result = simple_plan([(5.5, .1), (60, .7)])
    assert result.valid
    assert result.grid_energy_kwh == 0


def test_fixed_window_cannot_leave_negative_price_purchases_unused(windows_only):
    result = simple_plan([(15, -.1), (60, .7)], load=0)
    assert result.valid
    assert result.grid_energy_kwh == 0
    assert result.slots[-1].soc_end == 0


def test_fixed_window_cannot_displace_later_native_solar(windows_only):
    result = simple_plan([(15, -.1), (60, .4), (60, .7)],
                         config=replace(CONFIG, capacity_kwh=.5), solar=[0., 2., 0.])
    assert result.valid
    assert result.grid_energy_kwh == 0
    assert sum(row.pv_charge_kwh for row in result.slots) == pytest.approx(.5 / sqrt(.85))


@pytest.mark.parametrize('budget', [0, 1, 40])
def test_aborted_window_candidates_preserve_the_best_completed_plan(monkeypatch, budget):
    with monkeypatch.context() as disabled:
        disabled.setattr(bp, '_CHARGE_WINDOW_CANDIDATE_LIMIT', 0)
        incumbent = bp.build_battery_grid_plan(START, 0, *inputs(), CONFIG)
    monkeypatch.setattr(bp, '_CHARGE_WINDOW_TRANSITION_BUDGET', budget)
    result = bp.build_battery_grid_plan(START, 0, *inputs(), CONFIG)
    assert result == incumbent
    assert result.slots[-1].end == START + timedelta(hours=10)


def test_completed_window_candidates_cannot_replace_a_cheaper_incumbent(monkeypatch):
    with monkeypatch.context() as disabled:
        disabled.setattr(bp, '_CHARGE_WINDOW_CANDIDATE_LIMIT', 0)
        incumbent = simple_plan([(30, .1), (60, .7), (30, .1), (60, .7)])
    result = simple_plan([(30, .1), (60, .7), (30, .1), (60, .7)])
    assert incumbent.grid_energy_kwh > 0
    assert result.estimated_savings >= incumbent.estimated_savings - 1e-9


def test_additional_window_work_has_a_fixed_total_transition_budget(monkeypatch):
    search = bp._search_schedule
    budgets = []

    def record(*args, **kwargs):
        if kwargs.get('charge_window') is not None:
            budgets.append(kwargs['transition_budget'])
        return search(*args, **kwargs)

    monkeypatch.setattr(bp, '_search_schedule', record)
    result = bp.build_battery_grid_plan(START, 0, *inputs(), CONFIG)
    assert result.valid
    assert 0 < len(budgets) <= 4
    assert sum(budgets) <= 100_000
