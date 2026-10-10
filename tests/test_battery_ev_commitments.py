"""Active major loads and historical house demand share solar exactly once."""
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.pv_excess_control import battery_planner as planner
from custom_components.pv_excess_control.models import HourlyForecast, TariffWindow


NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
CONFIG = planner.BatteryPlanningConfig(
    capacity_kwh=10,
    reserve_soc=10,
    grid_target_soc=80,
    max_charge_power_w=3000,
    max_discharge_power_w=3000,
    charge_price_limit=0.25,
    wear_cost_per_kwh=0.03,
)


def commitment(start=NOW, end=None, power_w=11000, cap=None, floor=None):
    floor_args = {} if floor is None else {"household_power_floor_w": floor}
    return planner.BatteryLoadCommitment(
        start=start,
        end=end if end is not None else start + timedelta(hours=1),
        power_w=power_w,
        max_discharge_power_w=cap,
        **floor_args,
    )


def inputs(prices, solar, loads, *, now=NOW):
    windows, forecast = [], []
    profile = [0.0] * 24
    for i, price in enumerate(prices):
        start = now.astimezone(timezone.utc) + timedelta(hours=i)
        end = start + timedelta(hours=1)
        windows.append(TariffWindow(start, end, price, False))
        forecast.append(HourlyForecast(start, end, solar[i], solar[i] * 1000))
        profile[start.astimezone(now.tzinfo).hour] = loads[i] * 1000
    return windows, forecast, profile


def plan(prices, solar, loads, *, commitments=(), soc=20, config=CONFIG, now=NOW):
    windows, forecast, profile = inputs(prices, solar, loads, now=now)
    return planner.build_battery_grid_plan(
        now, soc, windows, forecast, profile, config,
        load_commitments=commitments,
    )


def test_ev_consumed_solar_creates_useful_partial_grid_topup():
    args = ([0.1, 0.4, 0.4, 0.4], [3, 1, 0, 0], [0, 0, 1.2, 1.2])
    unreserved = plan(*args)
    corrected = plan(*args, commitments=(commitment(cap=100),))
    assert unreserved.grid_energy_kwh == 0
    assert corrected.valid
    assert corrected.slots[0].grid_charge_kwh == pytest.approx(
        (2.4 - CONFIG.roundtrip_efficiency ** 0.5 - 0.85) / 0.85
    )
    assert corrected.grid_target_soc < 40
    assert corrected.estimated_savings > 0
    assert corrected.slots[0].pv_kwh == 0
    assert corrected.slots[0].reserved_pv_kwh == pytest.approx(3)
    assert corrected.slots[0].pv_charge_kwh == 0
    assert corrected.slots[1].pv_charge_kwh == pytest.approx(1)


def test_ev_does_not_gain_phantom_soc_or_draw_from_battery():
    result = plan([0.4, 0.4], [5, 0], [0, 0], commitments=(commitment(cap=0),))
    assert result.valid
    assert result.grid_energy_kwh == 0
    assert all(slot.soc_end == pytest.approx(20) for slot in result.slots)
    assert all(slot.discharge_kwh == 0 for slot in result.slots)
    assert sum(slot.reserved_pv_kwh for slot in result.slots) == pytest.approx(5)


@pytest.mark.parametrize("cap", [0, 100, 5000])
def test_commitment_discharge_cap_applies_only_until_its_end(cap):
    result = plan(
        [0.4, 0.4], [0, 0], [4, 4], soc=90,
        commitments=(commitment(cap=cap),),
    )
    assert result.slots[0].discharge_kwh == pytest.approx(min(3, cap / 1000))
    assert result.slots[1].discharge_kwh == pytest.approx(3)


@pytest.mark.parametrize("cap", [0, 100])
def test_future_grid_energy_bound_uses_commitment_discharge_cap(cap):
    result = plan(
        [0.1, 0.4], [0, 0], [0, 2], soc=10,
        commitments=(commitment(start=NOW + timedelta(hours=1), cap=cap),),
    )
    assert result.grid_energy_kwh == pytest.approx(cap / 1000 / 0.85)
    assert result.slots[-1].soc_end == pytest.approx(10)


def test_overlapping_loads_reserve_pv_once_and_use_strictest_cap():
    result = plan(
        [0.4, 0.4], [4, 4], [1, 1], soc=80,
        commitments=(commitment(power_w=2500, cap=100), commitment(power_w=2500, cap=0)),
    )
    first, second = result.slots
    assert first.pv_kwh == 0
    assert first.reserved_pv_kwh == pytest.approx(4)
    assert first.discharge_kwh == 0
    assert second.pv_kwh == pytest.approx(4)
    assert second.reserved_pv_kwh == 0
    assert second.pv_charge_kwh > 0


def test_subhour_commitment_splits_intervals_and_releases_pv_after_stop():
    result = plan(
        [0.4], [4], [0], soc=10,
        commitments=(commitment(start=NOW + timedelta(minutes=15), end=NOW + timedelta(minutes=45), power_w=2000),),
    )
    assert [slot.pv_kwh for slot in result.slots] == pytest.approx([1, 1, 1])
    assert [slot.reserved_pv_kwh for slot in result.slots] == pytest.approx([0, 1, 0])
    assert sum(slot.pv_kwh + slot.reserved_pv_kwh for slot in result.slots) == pytest.approx(4)
    assert result.slots[-1].soc_end == pytest.approx(10 + 2.5 * 0.85 ** 0.5 * 10)


def test_commitment_is_clipped_at_horizon_and_outside_load_has_no_effect():
    spanning = commitment(start=NOW - timedelta(hours=1), end=NOW + timedelta(hours=1, minutes=30), power_w=1000)
    outside = commitment(start=NOW + timedelta(hours=3), power_w=11000)
    result = plan([0.4, 0.4], [2, 2], [0, 0], commitments=(spanning, outside))
    assert result.valid
    assert sum(slot.reserved_pv_kwh for slot in result.slots) == pytest.approx(1.5)
    assert result.slots[-1].reserved_pv_kwh == 0


def test_commitment_duration_crossing_repeated_dst_hour_is_absolute():
    zone = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 10, 25, 1, tzinfo=zone)
    start = datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=0)
    end = datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=1)
    result = plan(
        [0.4] * 4, [2] * 4, [0] * 4, now=now,
        commitments=(commitment(start=start, end=end, power_w=1000),),
    )
    assert result.valid
    assert sum(slot.reserved_pv_kwh for slot in result.slots) == pytest.approx(1)
    assert sum((slot.end - slot.start).total_seconds() for slot in result.slots) == 4 * 3600


@pytest.mark.parametrize("updates", [
    {"power_w": -1}, {"power_w": float("nan")}, {"power_w": float("inf")},
    {"power_w": True}, {"cap": -1}, {"cap": float("nan")}, {"cap": float("inf")},
    {"cap": True}, {"end": NOW}, {"end": NOW - timedelta(seconds=1)},
    {"start": NOW.replace(tzinfo=None)}, {"end": NOW.replace(tzinfo=None)},
])
def test_invalid_commitment_blocks_plan(updates):
    result = plan([0.1, 0.4], [0, 0], [0, 2], commitments=(commitment(**updates),))
    assert not result.valid
    assert result.grid_energy_kwh == 0
    assert result.slots == ()


@pytest.mark.parametrize("kind", ["forecast", "tariff"])
def test_commitment_does_not_disguise_missing_provider_coverage(kind):
    windows, forecast, profile = inputs([0.1, 0.4, 0.4], [1, 1, 0], [0, 0, 2])
    rows = forecast if kind == "forecast" else windows
    del rows[1]
    result = planner.build_battery_grid_plan(
        NOW, 20, windows, forecast, profile, CONFIG,
        load_commitments=(commitment(end=NOW + timedelta(hours=2)),),
    )
    assert not result.valid
    assert result.reason == f"{kind}_gap_or_overlap"


def test_commitment_boundaries_preserve_interval_count_limit():
    commitments = tuple(
        commitment(start=NOW + timedelta(seconds=i * 2), end=NOW + timedelta(seconds=i * 2 + 1), power_w=100)
        for i in range(150)
    )
    result = plan([0.1, 0.4], [0, 0], [0, 2], commitments=commitments)
    assert not result.valid
    assert result.reason == "too_many_intervals"


def test_load_commitment_is_immutable():
    assert hasattr(planner, "BatteryLoadCommitment")
    value = commitment()
    with pytest.raises(FrozenInstanceError):
        value.power_w = 500


def test_default_and_explicit_empty_commitments_keep_identical_plan():
    windows, forecast, profile = inputs([0.1, 0.4], [1, 0], [0, 2])
    original = planner.build_battery_grid_plan(NOW, 20, windows, forecast, profile, CONFIG)
    explicit = planner.build_battery_grid_plan(NOW, 20, windows, forecast, profile, CONFIG, load_commitments=())
    assert original == explicit


def test_heat_pump_already_in_house_profile_is_not_added_twice():
    result = plan(
        [0.4], [4], [3],
        commitments=(commitment(power_w=0, floor=3000),),
    )
    assert result.valid
    assert result.slots[0].load_kwh == pytest.approx(3)
    assert result.slots[0].reserved_pv_kwh == 0
    assert result.slots[0].pv_charge_kwh == pytest.approx(1)


def test_observed_house_load_floor_replaces_underestimated_profile():
    result = plan(
        [0.4], [3], [0.5], soc=50,
        commitments=(commitment(power_w=0, floor=4000),),
    )
    first = result.slots[0]
    assert first.load_kwh == pytest.approx(4)
    assert first.pv_charge_kwh == 0
    assert first.discharge_kwh == pytest.approx(1)
    assert first.soc_end < 50


def test_house_load_floor_is_bounded_and_original_profile_recovers():
    result = plan(
        [0.4], [0], [1], soc=80,
        commitments=(commitment(
            start=NOW + timedelta(minutes=15),
            end=NOW + timedelta(minutes=45),
            power_w=0, floor=4000,
        ),),
    )
    assert [slot.load_kwh for slot in result.slots] == pytest.approx([0.25, 2, 0.25])


def test_overlapping_house_floors_use_maximum_without_summing():
    result = plan(
        [0.4], [6], [1],
        commitments=(commitment(power_w=0, floor=3000), commitment(power_w=0, floor=5000)),
    )
    assert result.slots[0].load_kwh == pytest.approx(5)
    assert result.slots[0].pv_charge_kwh == pytest.approx(1)


def test_house_floor_and_excluded_load_share_pv_without_double_counting():
    result = plan(
        [0.4], [5], [1], soc=50,
        commitments=(commitment(power_w=2000, floor=4000),),
    )
    first = result.slots[0]
    assert first.reserved_pv_kwh == pytest.approx(2)
    assert first.pv_kwh == pytest.approx(3)
    assert first.load_kwh == pytest.approx(4)
    assert first.discharge_kwh == pytest.approx(1)


@pytest.mark.parametrize("floor", [-1, float("nan"), float("inf"), True, "1000"])
def test_invalid_household_floor_blocks_plan(floor):
    result = plan(
        [0.1, 0.4], [0, 0], [0, 2],
        commitments=(commitment(power_w=0, floor=floor),),
    )
    assert not result.valid
    assert result.grid_energy_kwh == 0
    assert result.slots == ()


@pytest.mark.parametrize("cap, expected", [(0, 0), (100, 0.1), (None, 1.5)])
def test_external_load_draws_only_as_much_battery_as_actual_global_cap_allows(cap, expected):
    result = plan(
        [0.4, 0.4], [0, 0], [0, 0], soc=80,
        commitments=(commitment(power_w=1500, cap=cap),),
    )
    first, second = result.slots
    assert first.external_load_kwh == pytest.approx(1.5)
    assert first.load_kwh == 0
    assert first.discharge_kwh == pytest.approx(expected)
    assert first.soc_end == pytest.approx(80 - expected / 0.85 ** 0.5 * 10)
    assert second.external_load_kwh == 0
    assert second.discharge_kwh == 0


def test_global_discharge_cap_is_shared_between_house_and_external_load():
    result = plan(
        [0.4], [0], [0.05], soc=80,
        commitments=(commitment(power_w=1500, cap=100),),
    )
    assert result.slots[0].discharge_kwh == pytest.approx(0.1)


def test_future_external_demand_is_limited_by_actual_discharge_cap_in_bounds():
    result = plan(
        [0.1, 0.4], [0, 0], [0, 0], soc=10,
        commitments=(commitment(start=NOW + timedelta(hours=1), power_w=850, cap=1000),),
    )
    assert result.grid_energy_kwh == pytest.approx(1)
    assert result.slots[-1].discharge_kwh == pytest.approx(0.85)
    assert result.slots[-1].soc_end == pytest.approx(10)
    assert result.estimated_savings == pytest.approx(0.85 * (0.4 - CONFIG.wear_cost_per_kwh) - 0.1)


def test_verified_hold_can_retain_energy_while_external_load_uses_cheap_grid():
    result = plan(
        [0.1, 0.4], [0, 0], [0, 2], soc=30,
        config=replace(CONFIG, hold_supported=True, charge_price_limit=0),
        commitments=(commitment(power_w=1000),),
    )
    assert result.slots[0].action == "hold"
    assert result.slots[0].discharge_kwh == 0
    assert result.slots[-1].discharge_kwh == pytest.approx(2 * 0.85 ** 0.5)
    assert result.estimated_savings > 0
