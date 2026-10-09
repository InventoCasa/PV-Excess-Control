"""Economic battery planning uses physical, chronological energy balances."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from zoneinfo import ZoneInfo

import pytest

from custom_components.pv_excess_control.battery_planner import (
    BatteryPlanningConfig,
    build_battery_grid_plan,
)
from custom_components.pv_excess_control.models import HourlyForecast, TariffWindow


NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)
CONFIG = BatteryPlanningConfig(
    capacity_kwh=10,
    reserve_soc=10,
    grid_target_soc=80,
    max_charge_power_w=2500,
    max_discharge_power_w=3000,
    charge_price_limit=0.25,
)


def make_inputs(prices, solar=None, loads=None, *, now=NOW, minutes=60):
    windows, forecast = [], []
    solar = solar or [0.0] * len(prices)
    profile = [0.0] * 24
    for i, price in enumerate(prices):
        start = now.astimezone(timezone.utc) + timedelta(minutes=i * minutes)
        end = start + timedelta(minutes=minutes)
        windows.append(TariffWindow(start, end, price, False))
        forecast.append(HourlyForecast(start, end, solar[i], solar[i] * 60000 / minutes))
        if loads:
            profile[start.astimezone(now.tzinfo).hour] = loads[i] * 60000 / minutes
    return windows, forecast, profile


def plan(prices, solar=None, loads=None, *, soc=10, config=CONFIG, now=NOW):
    windows, forecast, profile = make_inputs(prices, solar, loads, now=now)
    return build_battery_grid_plan(now, soc, windows, forecast, profile, config)


def test_uneconomic_spread_after_losses_does_not_charge():
    result = plan([0.20, 0.23], loads=[0, 2])
    assert result.valid
    assert result.grid_energy_kwh == 0
    assert result.reason == "no_economic_benefit"


def test_wear_buffer_changes_the_economic_decision():
    result = plan([0.20, 0.26], loads=[0, 2], config=replace(CONFIG, wear_cost_per_kwh=0.03))
    assert result.grid_energy_kwh == 0
    without_wear = plan([0.20, 0.26], loads=[0, 2])
    assert without_wear.grid_energy_kwh > 0


def test_later_cheaper_opportunity_is_used_before_expensive_demand():
    result = plan([0.20, 0.10, 0.40], loads=[0, 0, 2])
    assert result.slots[0].grid_charge_kwh == 0
    assert result.slots[1].grid_charge_kwh == pytest.approx(2 / 0.85)
    assert result.estimated_savings == pytest.approx(0.8 - 0.2 / 0.85)
    assert result.slots[1].charge_power_w == pytest.approx(2000 / 0.85)


def test_solar_supply_leaves_autumn_headroom():
    result = plan([0.1, 0.3, 0.4], solar=[0, 6, 0], loads=[0, 0, 4], config=replace(CONFIG, max_charge_power_w=6000, max_discharge_power_w=5000))
    assert result.grid_energy_kwh == 0
    assert result.slots[1].soc_end > 60


def test_winter_forecast_charges_only_the_missing_energy():
    result = plan([0.1, 0.3, 0.4], solar=[0, 1, 0], loads=[0, 0, 2.5])
    assert result.grid_energy_kwh == pytest.approx((2.5 - 0.85) / 0.85)
    assert result.grid_target_soc < 35
    assert result.slots[-1].soc_end == pytest.approx(10)


def test_solar_can_fill_above_the_grid_target():
    result = plan([0.1, 0.4], solar=[20, 0], loads=[0, 0], soc=70)
    assert result.grid_energy_kwh == 0
    assert result.slots[0].soc_end > CONFIG.grid_target_soc


def test_grid_target_and_charge_power_limit_are_respected():
    result = plan([0.1, 0.4], loads=[0, 8], config=replace(CONFIG, grid_target_soc=25))
    assert 0 < result.grid_energy_kwh <= 2.5
    assert result.slots[0].soc_end == pytest.approx(25)
    assert min(slot.soc_end for slot in result.slots) >= 10 - 1e-8


def test_negative_price_does_not_buy_unneeded_energy():
    assert plan([-0.2, 0.4], loads=[0, 0]).grid_energy_kwh == 0
    result = plan([-0.2, 0.4], loads=[0, 1])
    assert result.grid_energy_kwh == pytest.approx(1 / 0.85)
    assert result.slots[-1].soc_end == pytest.approx(10)


def test_negative_price_does_not_displace_future_solar():
    result = plan([-0.2, 0.1, 0.4], solar=[0, 20, 0], loads=[0, 0, 2])
    assert result.grid_energy_kwh == 0


def test_no_hold_capability_accounts_for_immediate_self_consumption():
    # The battery discharges in the cheap middle hour unless a real hold exists.
    without_hold = plan([0.2, 0.1, 0.4], loads=[0, 3, 2], config=replace(CONFIG, charge_price_limit=0.2))
    assert without_hold.slots[0].grid_charge_kwh == 0
    assert without_hold.slots[1].action == "charge"
    # A price cap that excludes the middle hour cannot disguise holding as charging.
    blocked_later = plan([0.2, 0.22, 0.4], loads=[0, 3, 2], config=replace(CONFIG, charge_price_limit=0.2))
    assert blocked_later.grid_energy_kwh == 0
    with_hold = plan([0.2, 0.22, 0.4], loads=[0, 3, 2], config=replace(CONFIG, charge_price_limit=0.2, hold_supported=True))
    assert with_hold.slots[1].action == "hold"
    assert with_hold.estimated_savings > 0


@pytest.mark.parametrize("soc", [None, float("nan"), float("inf"), -1, 101])
def test_unknown_or_invalid_soc_refuses_planning(soc):
    result = plan([0.1, 0.4], loads=[0, 2], soc=soc)
    assert not result.valid
    assert result.grid_energy_kwh == 0
    assert result.slots == ()


@pytest.mark.parametrize("kind", ["forecast_gap", "tariff_gap", "forecast_overlap", "tariff_overlap", "load_nan", "pv_nan", "price_nan"])
def test_incomplete_or_ambiguous_data_refuses_planning(kind):
    windows, forecast, profile = make_inputs([0.1, 0.2, 0.4], loads=[0, 0, 2])
    if kind == "forecast_gap":
        del forecast[1]
    elif kind == "tariff_gap":
        del windows[1]
    elif kind == "forecast_overlap":
        forecast.append(forecast[0])
    elif kind == "tariff_overlap":
        windows.append(windows[0])
    elif kind == "load_nan":
        profile[0] = float("nan")
    elif kind == "pv_nan":
        forecast[1] = replace(forecast[1], expected_kwh=float("nan"))
    elif kind == "price_nan":
        windows[1] = replace(windows[1], price=float("nan"))
    result = build_battery_grid_plan(NOW, 10, windows, forecast, profile, CONFIG)
    assert not result.valid
    assert result.grid_energy_kwh == 0


def test_partial_current_interval_scales_power_and_energy():
    windows, forecast, profile = make_inputs([0.1, 0.4], loads=[0, 2])
    now = NOW + timedelta(minutes=45)
    result = build_battery_grid_plan(now, 10, windows, forecast, profile, CONFIG)
    assert result.slots[0].grid_charge_kwh == pytest.approx(0.625)
    assert result.slots[0].start == now
    assert result.slots[0].charge_power_w == pytest.approx(2500)


def test_dst_fall_back_counts_both_local_hours_once():
    now = datetime(2026, 10, 25, 1, tzinfo=ZoneInfo("Europe/Berlin"))
    windows, forecast, profile = make_inputs([0.1, 0.4, 0.4, 0.4], now=now)
    profile[2] = 1000
    result = build_battery_grid_plan(now, 10, windows, forecast, profile, CONFIG)
    assert result.valid
    assert sum(slot.load_kwh for slot in result.slots) == pytest.approx(2)
    assert sum((slot.end - slot.start).total_seconds() for slot in result.slots) == 4 * 3600


def test_horizon_is_limited_by_the_shorter_complete_provider():
    windows, forecast, profile = make_inputs([0.1, 0.2, 0.4], loads=[0, 0, 2])
    result = build_battery_grid_plan(NOW, 10, windows[:2], forecast, profile, CONFIG)
    assert result.valid
    assert len(result.slots) == 2
    assert result.grid_energy_kwh == 0


@pytest.mark.parametrize("efficiency", [0.8, 0.85, 1.0])
def test_energy_and_savings_are_exact_for_the_selected_efficiency(efficiency):
    result = plan([0.1, 0.4], loads=[0, 1.7], config=replace(CONFIG, roundtrip_efficiency=efficiency))
    assert result.grid_energy_kwh == pytest.approx(1.7 / efficiency)
    assert result.slots[1].discharge_kwh == pytest.approx(1.7)
    assert result.estimated_savings == pytest.approx(1.7 * 0.4 - 1.7 / efficiency * 0.1)


@pytest.mark.parametrize("updates", [
    {"capacity_kwh": 0}, {"capacity_kwh": float("nan")},
    {"reserve_soc": -1}, {"grid_target_soc": 5}, {"grid_target_soc": 101},
    {"max_charge_power_w": 0}, {"max_discharge_power_w": float("inf")},
    {"roundtrip_efficiency": 0}, {"roundtrip_efficiency": 1.1},
    {"wear_cost_per_kwh": -0.01}, {"charge_price_limit": float("nan")},
    {"energy_step_kwh": 0}, {"horizon_hours": 100},
])
def test_invalid_physical_configuration_refuses_planning(updates):
    result = plan([0.1, 0.4], loads=[0, 2], config=replace(CONFIG, **updates))
    assert not result.valid
    assert result.reason == "invalid_configuration"


def test_naive_timestamps_refuse_planning():
    windows, forecast, profile = make_inputs([0.1, 0.4], loads=[0, 2])
    windows[0] = replace(windows[0], start=windows[0].start.replace(tzinfo=None))
    result = build_battery_grid_plan(NOW, 10, windows, forecast, profile, CONFIG)
    assert not result.valid
    assert result.reason == "timezone_required"


def test_spring_dst_skips_the_nonexistent_local_hour():
    now = datetime(2026, 3, 29, 1, tzinfo=ZoneInfo("Europe/Berlin"))
    windows, forecast, profile = make_inputs([0.1, 0.4, 0.4], now=now)
    profile[2] = 9000
    profile[3] = 1000
    result = build_battery_grid_plan(now, 10, windows, forecast, profile, CONFIG)
    assert result.valid
    assert sum(slot.load_kwh for slot in result.slots) == pytest.approx(1)


def test_battery_below_native_reserve_is_not_assumed_to_contain_reserve_energy():
    result = plan([0.4, 0.4], loads=[1, 1], soc=5)
    assert result.grid_energy_kwh == 0
    assert all(slot.discharge_kwh == 0 and slot.soc_end == 5 for slot in result.slots)


def test_discharge_power_limits_grid_energy_to_usable_household_supply():
    result = plan([0.1, 0.4], loads=[0, 8], config=replace(CONFIG, max_discharge_power_w=1000))
    assert result.grid_energy_kwh == pytest.approx(1 / 0.85)
    assert result.slots[-1].discharge_kwh == pytest.approx(1)


def test_missing_night_forecast_is_unknown_even_if_daytime_pv_is_plentiful():
    windows, forecast, profile = make_inputs([0.1, 0.4], solar=[0, 5], loads=[0, 2])
    result = build_battery_grid_plan(NOW, 10, windows, forecast[1:], profile, CONFIG)
    assert not result.valid


def test_no_grid_energy_when_existing_energy_covers_future_demand():
    result = plan([0.1, 0.4], loads=[0, 2], soc=70)
    assert result.grid_energy_kwh == 0


def test_charge_and_discharge_balances_respect_every_physical_limit():
    prices = [0.18] * 8 + [0.07] * 4 + [0.4] * 8 + [0.2] * 4
    result = plan(prices, solar=[0] * 8 + [1] * 8 + [0] * 8, loads=[0.8] * 24, soc=35)
    assert result.valid
    energy = 3.5
    efficiency = CONFIG.roundtrip_efficiency ** 0.5
    for slot in result.slots:
        hours = (slot.end - slot.start).total_seconds() / 3600
        solar = min(max(0, slot.pv_kwh - slot.load_kwh), CONFIG.max_charge_power_w * hours / 1000, (10 - energy) / efficiency)
        assert slot.grid_charge_kwh + solar <= CONFIG.max_charge_power_w * hours / 1000 + 1e-8
        assert slot.discharge_kwh <= min(max(0, slot.load_kwh - slot.pv_kwh), CONFIG.max_discharge_power_w * hours / 1000) + 1e-8
        energy += (solar + slot.grid_charge_kwh) * efficiency - slot.discharge_kwh / efficiency
        assert slot.soc_end == pytest.approx(energy * 10)
        assert 10 - 1e-8 <= slot.soc_end <= 100 + 1e-8
        if slot.action == "charge":
            assert slot.soc_end <= CONFIG.grid_target_soc + 1e-8
            assert slot.discharge_kwh == 0
        else:
            assert slot.action == "self_consumption"
            assert slot.grid_charge_kwh == 0


def test_forced_charge_power_includes_both_pv_and_grid_contributions():
    result = plan([0.1, 0.4], solar=[1, 0], loads=[0, 2])
    first = result.slots[0]
    assert first.pv_charge_kwh == pytest.approx(1)
    assert first.grid_charge_kwh == pytest.approx(2 / 0.85 - 1)
    assert first.battery_charge_power_w == pytest.approx(2000 / 0.85)
    assert first.battery_charge_power_w <= CONFIG.max_charge_power_w


def test_native_solar_capacity_is_independent_of_the_grid_purchase_limit():
    config = replace(CONFIG, max_pv_charge_power_w=5000, max_discharge_power_w=5000)
    result = plan([.1, .3, .4], solar=[0, 5, 0], loads=[0, 0, 4], config=config)
    assert result.grid_energy_kwh == 0
    assert result.slots[1].pv_charge_kwh == pytest.approx(5)


def test_total_forced_power_can_include_solar_above_the_grid_purchase_limit():
    config = replace(CONFIG, max_pv_charge_power_w=5000, max_discharge_power_w=4000)
    result = plan([.1, .4], solar=[1.5, 0], loads=[0, 3], config=config)
    assert result.grid_energy_kwh == pytest.approx(3 / .85 - 1.5)
    assert result.slots[0].battery_charge_power_w == pytest.approx(3000 / .85)
    assert result.slots[0].charge_power_w <= config.max_charge_power_w
    assert result.slots[0].battery_charge_power_w <= config.max_pv_charge_power_w


def test_solar_and_grid_share_the_native_charger_capacity():
    config = replace(CONFIG, max_pv_charge_power_w=3000, max_discharge_power_w=5000)
    result = plan([.1, .4], solar=[1.5, 0], loads=[0, 4], config=config)
    assert result.slots[0].grid_charge_kwh == pytest.approx(1.5)
    assert result.slots[0].battery_charge_power_w == pytest.approx(3000)


def test_additional_grid_input_still_respects_its_own_purchase_limit():
    config = replace(CONFIG, max_pv_charge_power_w=5000, max_discharge_power_w=5000)
    result = plan([.1, .4], solar=[1, 0], loads=[0, 5], config=config)
    assert result.grid_energy_kwh == pytest.approx(2.5)
    assert result.slots[0].battery_charge_power_w == pytest.approx(3500)


@pytest.mark.parametrize("native_limit", [0, -1, float("nan"), float("inf")])
def test_invalid_native_charge_limit_refuses_planning(native_limit):
    result = plan([.1, .4], loads=[0, 2], config=replace(CONFIG, max_pv_charge_power_w=native_limit))
    assert not result.valid
    assert result.grid_energy_kwh == 0
