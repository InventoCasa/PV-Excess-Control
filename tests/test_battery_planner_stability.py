"""Equivalent economic plans prefer stable earlier useful charging."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.pv_excess_control.battery_planner import (
    BatteryLoadCommitment, BatteryPlanningConfig, build_battery_grid_plan,
)
from custom_components.pv_excess_control.models import HourlyForecast, TariffWindow


NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
CONFIG = BatteryPlanningConfig(
    capacity_kwh=10, reserve_soc=10, grid_target_soc=100,
    max_charge_power_w=5000, max_discharge_power_w=5000,
    wear_cost_per_kwh=.03, charge_price_limit=.3,
)


def build(*, external=11000, cap=100, prices=None, solar=None, demand=4000, soc=40, config=CONFIG):
    prices = prices or [.17] * 4 + [.4] * 4
    solar = solar or [0.] * len(prices)
    windows = [TariffWindow(NOW + timedelta(minutes=15 * i), NOW + timedelta(minutes=15 * (i + 1)), price, False)
               for i, price in enumerate(prices)]
    forecast = [HourlyForecast(row.start, row.end, pv, pv * 4000) for row, pv in zip(windows, solar)]
    profile = [0.] * 24
    profile[13] = demand
    return build_battery_grid_plan(
        NOW, soc, windows, forecast, profile, config,
        load_commitments=(BatteryLoadCommitment(NOW, NOW + timedelta(hours=1), external, cap),),
    )


def test_constant_external_grid_cost_cannot_randomly_rearrange_equal_price_charge():
    plans = [build(external=watts) for watts in (11000, 11100, 11200, 11300, 11400)]
    required = 4 / .85 - 3 / .85 ** .5
    for result in plans:
        assert result.valid
        assert result.grid_energy_kwh == pytest.approx(required)
        assert result.estimated_savings == pytest.approx(plans[0].estimated_savings, abs=1e-9)
        # The bounded search need not find the globally earliest schedule, but
        # equivalent candidates consistently put their largest purchase first.
        assert result.slots[0].grid_charge_kwh > max(slot.grid_charge_kwh for slot in result.slots[1:4])
        assert [slot.grid_charge_kwh for slot in result.slots] == pytest.approx(
            [slot.grid_charge_kwh for slot in plans[0].slots], abs=1e-9,
        )
        # Remaining positive slots prevent the real 100 W native leakage;
        # converting them to zero-charge free holding would change economics.
        assert all(slot.grid_charge_kwh > 0 for slot in result.slots[:4])
        assert result.slots[-1].soc_end == pytest.approx(10)


def test_verified_zero_cap_allows_unneeded_later_charge_slots_to_disappear():
    result = build(cap=0, demand=1000, soc=10)
    assert result.slots[0].grid_charge_kwh == pytest.approx(1 / .85)
    assert all(slot.grid_charge_kwh == 0 for slot in result.slots[1:])
    assert result.slots[-1].soc_end == pytest.approx(10)


def test_genuinely_cheaper_later_window_still_has_economic_priority():
    result = build(cap=0, prices=[.17, .16, .17, .17] + [.4] * 4)
    assert result.slots[1].grid_charge_kwh == pytest.approx(1.25)
    assert result.grid_energy_kwh == pytest.approx(4 / .85 - 3 / .85 ** .5)
    assert result.slots[0].grid_charge_kwh < result.slots[1].grid_charge_kwh


def test_early_tie_preference_preserves_future_pv_headroom_and_energy_limits():
    result = build(
        external=0, cap=0, soc=80, demand=8000,
        solar=[0, 1.25, 1.25, 1.25, 0, 0, 0, 0],
        config=replace(CONFIG, max_discharge_power_w=10000),
    )
    assert result.valid
    # Native solar already fills the battery; extra early grid energy must
    # never replace that uptake simply to move purchases earlier.
    assert result.grid_energy_kwh == 0
    assert result.slots[3].soc_end == pytest.approx(100)
    assert sum(slot.pv_charge_kwh for slot in result.slots) == pytest.approx(2 / .85 ** .5)
    assert all(10 - 1e-8 <= slot.soc_end <= 100 + 1e-8 for slot in result.slots)
