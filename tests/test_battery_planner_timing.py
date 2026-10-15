"""Neutral load metadata and executable charge timing use synthetic inputs."""
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from math import sqrt
from itertools import product
from zoneinfo import ZoneInfo

import pytest

from custom_components.pv_excess_control import battery_planner as bp
from custom_components.pv_excess_control.models import HourlyForecast, TariffWindow

START = datetime(2030, 1, 1, tzinfo=timezone.utc)
CONFIG = bp.BatteryPlanningConfig(
    capacity_kwh=2, reserve_soc=0, grid_target_soc=100,
    max_charge_power_w=400, max_discharge_power_w=1000,
    roundtrip_efficiency=.85, wear_cost_per_kwh=.03,
    charge_price_limit=.4, hold_supported=True,
)


def inputs(prices=(.15, .15, .35, .15), loads=(.2, .4, .8, .2), solar=(.2, .1, .2, 0)):
    prices = [TariffWindow(START + timedelta(hours=i), START + timedelta(hours=i + 1), p, False)
              for i, p in enumerate(prices)]
    forecast = [HourlyForecast(p.start, p.end, pv, pv * 1000) for p, pv in zip(prices, solar)]
    profile = [0.] * 24
    profile[:len(loads)] = [value * 1000 for value in loads]
    return prices, forecast, profile


def plan(*, commitments=(), config=CONFIG, data=None):
    return bp.build_battery_grid_plan(START, 20, *(data or inputs()), config, load_commitments=commitments)


def timeline(commitments=(), *, data=None, now=START):
    return bp._timeline(now, *(data or inputs()), 24, commitments)


def cost(result, config=CONFIG):
    """Independent AC cost from public schedule balances, without search helpers."""
    return sum((max(0., s.load_kwh - s.pv_kwh) + max(0., s.external_load_kwh - s.reserved_pv_kwh)
                - s.discharge_kwh + s.grid_charge_kwh) * s.price
               + s.discharge_kwh * config.wear_cost_per_kwh for s in result.slots)


def assert_same_timeline(actual, expected):
    assert len(actual) == len(expected)
    for left, right in zip(actual, expected, strict=True):
        for key, value in asdict(left).items():
            other = getattr(right, key)
            assert value == (pytest.approx(other, abs=1e-12) if isinstance(other, float) else other)


def assert_same_plan(actual, expected):
    assert actual.valid == expected.valid
    assert actual.reason == expected.reason
    assert actual.grid_energy_kwh == pytest.approx(expected.grid_energy_kwh, abs=1e-12)
    assert actual.estimated_savings == pytest.approx(expected.estimated_savings, abs=1e-12)
    assert len(actual.slots) == len(expected.slots)
    for left, right in zip(actual.slots, expected.slots, strict=True):
        for key, value in asdict(left).items():
            other = getattr(right, key)
            assert value == (pytest.approx(other, abs=1e-12) if isinstance(other, float) else other)


def test_neutral_short_household_floor_cannot_change_schedule():
    neutral = bp.BatteryLoadCommitment(START, START + timedelta(minutes=1), 0, household_power_floor_w=200)
    assert_same_timeline(timeline((neutral,)), timeline())
    assert_same_plan(plan(commitments=(neutral,)), plan())


def test_dominated_nested_floor_does_not_create_an_action_boundary():
    long = bp.BatteryLoadCommitment(START, START + timedelta(minutes=30), 0, household_power_floor_w=900)
    short = replace(long, end=START + timedelta(minutes=1), household_power_floor_w=700)
    assert_same_timeline(timeline((long, short)), timeline((long,)))
    assert_same_plan(plan(commitments=(long, short)), plan(commitments=(long,)))


def test_adjacent_equivalent_load_commitments_have_one_physical_boundary():
    long = bp.BatteryLoadCommitment(START, START + timedelta(minutes=30), 300, 100, 900)
    left = replace(long, end=START + timedelta(minutes=10))
    right = replace(long, start=left.end)
    assert_same_timeline(timeline((left, right)), timeline((long,)))


@pytest.mark.parametrize('commitment', [
    bp.BatteryLoadCommitment(START, START + timedelta(minutes=1), 1),
    bp.BatteryLoadCommitment(START, START + timedelta(minutes=1), 0, 100),
    bp.BatteryLoadCommitment(START, START + timedelta(minutes=1), 0, household_power_floor_w=201),
])
def test_genuine_external_household_and_cap_changes_keep_their_boundaries(commitment):
    rows = timeline((commitment,))
    assert rows[0].end == commitment.end
    assert rows[1].start == commitment.end
    if commitment.max_discharge_power_w is not None:
        assert rows[0].max_discharge_power_w == 100
        assert rows[1].max_discharge_power_w is None


def test_equal_prices_forecasts_and_hour_boundaries_are_authoritative():
    data = inputs(prices=(.2,) * 4, loads=(.2,) * 4, solar=(.1,) * 4)
    assert len(timeline(data=data)) == 4
    prices, forecast, profile = data
    split = START + timedelta(minutes=20)
    prices = [replace(prices[0], end=split), replace(prices[0], start=split), *prices[1:]]
    split_pv = START + timedelta(minutes=40)
    forecast = [replace(forecast[0], end=split_pv, expected_kwh=.1 * 2 / 3),
                replace(forecast[0], start=split_pv, expected_kwh=.1 / 3), *forecast[1:]]
    assert {r.end for r in timeline(data=(prices, forecast, profile))} >= {split, split_pv, START + timedelta(hours=1)}


def test_many_neutral_boundaries_still_fail_closed_before_coalescing():
    commitments = tuple(bp.BatteryLoadCommitment(START, START + timedelta(seconds=i), 0) for i in range(1, 290))
    result = plan(commitments=commitments)
    assert not result.valid
    assert result.reason == 'too_many_intervals'


def test_smaller_charge_quantities_improve_a_known_feasible_schedule():
    result = plan(data=inputs(prices=(.25, .35, .35, .45), loads=(.1, .2, .4, .8), solar=(.2,) * 4))
    assert result.valid
    assert cost(result) <= .13320750307
    assert result.slots[-1].soc_end == pytest.approx(0)


@pytest.mark.parametrize('budget', [0, 1, 40])
def test_aborted_quantity_refinement_preserves_both_completed_searches(monkeypatch, budget):
    data = inputs(prices=(.25, .35, .35, .45), loads=(.1, .2, .4, .8), solar=(.2,) * 4)
    monkeypatch.setattr(bp, '_QUANTITY_REFINEMENT_TRANSITION_BUDGET', 0)
    incumbent = plan(data=data)
    monkeypatch.setattr(bp, '_QUANTITY_REFINEMENT_TRANSITION_BUDGET', budget)
    result = plan(data=data)
    assert result == incumbent
    assert len(result.slots) == 4


def test_quantity_refinement_requires_verified_hold(monkeypatch):
    data = inputs(prices=(.25, .35, .35, .45), loads=(.1, .2, .4, .8), solar=(.2,) * 4)
    config = replace(CONFIG, hold_supported=False)
    monkeypatch.setattr(bp, '_QUANTITY_REFINEMENT_TRANSITION_BUDGET', 0)
    original = plan(data=data, config=config)
    monkeypatch.setattr(bp, '_QUANTITY_REFINEMENT_TRANSITION_BUDGET', 100_000)
    assert plan(data=data, config=config) == original


def short_plan(prices, *, delay=0, not_before=None, active=False, soc=0):
    windows = [TariffWindow(START + timedelta(minutes=i), START + timedelta(minutes=i + 1), p, False)
               for i, p in enumerate(prices)]
    solar = [HourlyForecast(w.start, w.end, 0, 0) for w in windows]
    config = replace(CONFIG, capacity_kwh=1, max_charge_power_w=6000, max_discharge_power_w=5000,
                     grid_restart_delay_minutes=delay, grid_charge_not_before=not_before,
                     grid_charge_active=active, energy_step_kwh=.01)
    return bp.build_battery_grid_plan(START, soc, windows, solar, [1000.] * 24, config), config


def assert_restart_delay(result, delay, *, active=False, not_before=None):
    eligible = not_before
    for slot in result.slots:
        charging = slot.action == 'charge'
        if charging and not active:
            assert eligible is None or slot.start >= eligible
        if active and not charging:
            deadline = slot.start + timedelta(minutes=delay)
            eligible = max(eligible, deadline) if eligible is not None else deadline
        active = charging


def test_initial_restart_deadline_splits_a_price_slot_and_blocks_early_charge():
    until = START + timedelta(seconds=30)
    result, _ = short_plan([.1, .5, .5, .5], delay=5, not_before=until)
    assert result.valid
    assert result.slots[0].end == until
    assert result.slots[0].action != 'charge'
    assert result.slots[1].start == until
    assert result.slots[1].action == 'charge'
    assert_restart_delay(result, 5, not_before=until)


@pytest.mark.parametrize('delay', [0, 5])
def test_current_charge_may_continue_without_waiting_for_a_restart_deadline(delay):
    until = START + timedelta(minutes=10)
    result, _ = short_plan([.1, .5, .5, .5], delay=delay, not_before=until, active=True)
    assert result.valid
    assert result.slots[0].action == 'charge'
    assert_restart_delay(result, delay, active=True, not_before=until)


def test_future_charge_stop_requires_full_restart_delay():
    unconstrained, _ = short_plan([.15, .5, .05, .5], delay=0)
    result, _ = short_plan([.15, .5, .05, .5], delay=5)
    assert [s.action for s in unconstrained.slots] == ['charge', 'self_consumption', 'charge', 'self_consumption']
    assert result.valid
    assert_restart_delay(result, 5)
    assert sum(s.action == 'charge' for s in result.slots) == 1


def test_zero_restart_delay_preserves_legacy_timing_behavior():
    config = replace(CONFIG, grid_restart_delay_minutes=0, grid_charge_active=False)
    assert plan(config=config) == plan()


@pytest.mark.parametrize('field,value', [
    ('grid_restart_delay_minutes', -1), ('grid_restart_delay_minutes', float('nan')),
    ('grid_restart_delay_minutes', True), ('grid_charge_active', 1),
    ('grid_restart_delay_minutes', 61), ('grid_restart_delay_minutes', 1e300),
    ('grid_charge_not_before', START.replace(tzinfo=None)), ('grid_charge_not_before', 'tomorrow'),
])
def test_invalid_restart_configuration_fails_closed(field, value):
    result = plan(config=replace(CONFIG, **{field: value}))
    assert not result.valid


@pytest.mark.parametrize('delay,prices', [
    (0, (.15, .5, .05, .5)), (2, (.15, .5, .05, .5)),
    (5, (.15, .5, .05, .5)), (2, (.1, .11, .5, .5)),
])
def test_restart_schedule_matches_independent_small_discrete_reference(delay, prices):
    result, config = short_plan(prices, delay=delay)
    assert result.valid
    assert_restart_delay(result, delay)
    best = float('inf')
    # AC-side accounting and timing are independent of planner transitions,
    # buckets and state classes. Native and hold are distinct explicit modes.
    choices = ('native', 'hold', *(.01 * i for i in range(1, 11)))
    for commands in product(choices, repeat=4):
        available = total = 0.
        active = False
        eligible = 0.
        valid = True
        for minute, (price, command) in enumerate(zip(prices, commands, strict=True)):
            charging = isinstance(command, float)
            if charging and (price > config.charge_price_limit or (not active and minute < eligible)):
                valid = False
                break
            if active and not charging:
                eligible = minute + delay
            active = charging
            if charging:
                available += command * config.roundtrip_efficiency
                if available > config.capacity_kwh * sqrt(config.roundtrip_efficiency):
                    valid = False
                    break
            supplied = min(available, 1 / 60) if command == 'native' else 0.
            available -= supplied
            total += (1 / 60 - supplied + (command if charging else 0)) * price + supplied * .03
        if valid and available <= 1e-8:
            best = min(best, total)
    assert cost(result, config) <= best + 1e-8


def test_initial_eligibility_boundary_survives_neutral_load_coalescing():
    until = START + timedelta(minutes=1)
    neutral = bp.BatteryLoadCommitment(START, until, 0, household_power_floor_w=200)
    rows = bp._timeline(START, *inputs(), 24, (neutral,), grid_charge_not_before=until)
    assert rows[0].end == until
    assert rows[1].start == until


def test_neutral_load_spanning_repeated_local_hour_preserves_utc_durations():
    local = datetime(2030, 10, 27, 1, 30, tzinfo=ZoneInfo('Europe/Berlin'))
    begin = local.astimezone(timezone.utc)
    end = begin + timedelta(hours=4)
    prices = [TariffWindow(begin, end, .2, False)]
    solar = [HourlyForecast(begin, end, 0, 0)]
    profile = [200.] * 24
    profile[2] = 400.
    neutral = bp.BatteryLoadCommitment(begin + timedelta(minutes=5), end - timedelta(minutes=5), 0,
                                      household_power_floor_w=200)
    baseline = bp._timeline(local, prices, solar, profile, 24)
    assert_same_timeline(bp._timeline(local, prices, solar, profile, 24, (neutral,)), baseline)
    assert sum(r.hours for r in baseline) == 4
    repeated = [r for r in baseline if r.start.astimezone(local.tzinfo).hour == 2]
    assert len(repeated) == 2
    assert all(r.load / r.hours == pytest.approx(.4) for r in repeated)


def test_earlier_completed_incumbent_survives_quantity_refinement(monkeypatch):
    data = inputs(prices=(.12, .28, .17, .39), loads=(0, .3, 0, .8), solar=(.2, .1, .1, 0))
    with monkeypatch.context() as disabled:
        disabled.setattr(bp, '_QUANTITY_REFINEMENT_TRANSITION_BUDGET', 0)
        incumbent = plan(data=data)
    result = plan(data=data)
    assert cost(result) <= cost(incumbent) + 1e-9
