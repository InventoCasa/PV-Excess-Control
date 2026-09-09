"""Regression coverage for timestamp windows and current command stabilization."""

from dataclasses import replace
from datetime import timedelta, timezone

import pytest

from custom_components.pv_excess_control.models import Action
from custom_components.pv_excess_control.optimizer import (
    Optimizer,
    recent_power_history,
)
from tests.test_optimizer import (
    _empty_plan,
    _make_appliance,
    _make_power,
    _make_state,
    _make_tariff,
)


def _dynamic(**overrides):
    interval = overrides.pop("current_update_interval", 0)
    change = overrides.pop("current_min_change", 0)
    app = _make_appliance(
        dynamic_current=True,
        current_entity="number.charger",
        min_current=6,
        max_current=16,
        current_step=1,
        **overrides,
    )
    return replace(app, current_update_interval=interval, current_min_change=change)


def _running(amps=6, seconds=0, **overrides):
    state = _make_state(is_on=True, current_power=amps * 230, **overrides)
    return replace(state, current_amperage=amps, seconds_since_current_change=seconds)


def _run(apps, states, excess, *, history=None, tariff=None):
    power = _make_power(excess)
    return (
        Optimizer(enable_preemption=False)
        .optimize(
            power,
            apps,
            states,
            _empty_plan(),
            history if history is not None else [power] * 3,
            tariff or _make_tariff(),
        )
        .decisions
    )


def test_custom_average_uses_timestamps_with_fast_irregular_updates():
    power = _make_power(2000)
    history = [
        replace(power, excess_power=0, timestamp=power.timestamp - timedelta(seconds=i))
        for i in (55, 41, 25)
    ] + [power]
    history.insert(
        0,
        replace(
            power, excess_power=4000, timestamp=power.timestamp - timedelta(seconds=90)
        ),
    )
    app = replace(_dynamic(), averaging_window=60)
    result = _run([app], [_running()], 2000, history=history)
    assert result[0].target_current == 8  # 500 W average, not final 2 samples (1000 W)


def test_custom_average_excludes_old_future_and_accepts_utc_naive():
    power = _make_power(230)
    history = [
        replace(
            power, excess_power=4000, timestamp=power.timestamp - timedelta(seconds=61)
        ),
        replace(
            power, excess_power=4000, timestamp=power.timestamp + timedelta(seconds=1)
        ),
    ]
    history += [
        replace(
            power,
            timestamp=(power.timestamp - timedelta(seconds=i)).replace(tzinfo=None),
        )
        for i in (59, 30, 0)
    ]
    result = _run(
        [replace(_dynamic(), averaging_window=60)], [_running()], 4000, history=history
    )
    assert result[0].target_current == 7


@pytest.mark.parametrize(
    ("interval", "minimum", "elapsed", "excess", "target"),
    [
        (60, 0, 59.9, 460, 6),
        (60, 0, 60, 460, 8),
        (0, 3, 100, 460, 6),
        (0, 2, 100, 460, 8),
        (60, 2, None, 460, 8),
        (60, 2, 0, -460, 6),
    ],
)
def test_upward_interval_and_minimum_boundaries(
    interval, minimum, elapsed, excess, target
):
    amps = 8 if excess < 0 else 6
    app = _dynamic(current_update_interval=interval, current_min_change=minimum)
    result = _run([app], [_running(amps, elapsed)], excess)
    assert result[0].target_current == target


def test_held_increase_preserves_budget_for_next_consumer():
    app = _dynamic(current_update_interval=60)
    second = _make_appliance(id="second", priority=2, nominal_power=500, on_threshold=0)
    decisions = _run([app, second], [_running(), _make_state(id="second")], 600)
    assert decisions[0].target_current == 6
    assert decisions[1].action == Action.ON


def test_held_grid_increase_does_not_create_solar_credit():
    app = _dynamic(
        current_update_interval=60,
        allow_grid_supplement=True,
        cheap_grid_target_current=16,
        max_grid_power=4000,
    )
    second = _make_appliance(id="second", priority=2, nominal_power=500, on_threshold=0)
    decisions = _run(
        [app, second],
        [_running(), _make_state(id="second")],
        0,
        tariff=_make_tariff(current_price=0.05),
    )
    assert decisions[0].target_current == 6
    assert decisions[0].grid_supplement_watts == 0
    assert not decisions[0].uses_grid_supplement
    assert decisions[1].action == Action.IDLE


def test_held_grid_increase_retains_actual_import_allocation():
    app = _dynamic(
        current_update_interval=60,
        allow_grid_supplement=True,
        cheap_grid_target_current=16,
        max_grid_power=4000,
    )
    decision = _run(
        [app], [_running()], -1000, tariff=_make_tariff(current_price=0.05)
    )[0]
    assert decision.target_current == 6
    assert decision.grid_supplement_watts == 1000
    assert decision.uses_grid_supplement


@pytest.mark.parametrize("override", [False, True])
def test_initial_start_and_manual_override_are_immediate(override):
    app = _dynamic(
        current_update_interval=999, current_min_change=99, override_active=override
    )
    state = _running() if override else _make_state()
    result = _run([app], [state], 5000)
    assert result[0].target_current == 16


@pytest.mark.parametrize(
    ("connected", "expected_action"), [(False, Action.ON), (None, Action.IDLE)]
)
def test_off_ev_override_only_reserves_power_when_connection_possible(
    connected, expected_action
):
    app = _dynamic(override_active=True, ev_connected_entity="binary_sensor.connected")
    second = _make_appliance(id="second", priority=2, nominal_power=500, on_threshold=0)
    decisions = _run(
        [app, second],
        [_make_state(ev_connected=connected), _make_state(id="second")],
        600,
    )
    assert decisions[0].action == Action.SET_CURRENT
    assert decisions[0].target_current == 16
    assert decisions[1].action == expected_action


def test_on_disconnected_ev_override_does_not_reserve_new_power():
    app = _dynamic(override_active=True, ev_connected_entity="binary_sensor.connected")
    second = _make_appliance(id="second", priority=2, nominal_power=500, on_threshold=0)
    state = _running(ev_connected=False)
    state.current_power = 0
    decisions = _run([app, second], [state, _make_state(id="second")], 600)
    assert decisions[0].target_current == 16
    assert decisions[1].action == Action.ON


def test_recent_history_boundaries_preserve_order_and_normalize_timezones():
    power = _make_power()
    samples = [
        replace(power, timestamp=power.timestamp - timedelta(seconds=age))
        for age in (60, 10, -1, 59.99, 0)
    ]
    samples[1] = replace(
        samples[1],
        timestamp=samples[1].timestamp.astimezone(timezone(timedelta(hours=2))),
    )
    samples[3] = replace(
        samples[3], timestamp=samples[3].timestamp.replace(tzinfo=None)
    )
    expected = [samples[1], samples[3], samples[4]]
    assert recent_power_history(samples, 60, power.timestamp) == expected
    assert (
        recent_power_history(samples, 60, power.timestamp.replace(tzinfo=None))
        == expected
    )
    assert recent_power_history(samples, 0, power.timestamp) == []


def test_narrow_history_falls_back_until_three_good_samples():
    power = _make_power(0)
    history = [
        replace(
            power, excess_power=1200, timestamp=power.timestamp - timedelta(seconds=age)
        )
        for age in (120, 90, 61)
    ]
    history += [
        replace(power, excess_power=bad) for bad in (0, float("nan"), float("inf"))
    ]
    result = _run(
        [replace(_dynamic(), averaging_window=60)], [_running()], 2000, history=history
    )
    assert result[0].target_current == 9  # Global mean of four valid samples is900 W.


@pytest.mark.parametrize("current", [None, float("nan"), 5, 17])
def test_unknown_or_out_of_range_current_does_not_block_adjustment(current):
    app = _dynamic(current_update_interval=999, current_min_change=99)
    state = replace(_running(), current_amperage=current)
    decision = _run([app], [state], 460)[0]
    assert decision.target_current == 8


def test_max_runtime_safety_remains_immediate_with_current_limiter():
    app = _dynamic(
        current_update_interval=999,
        current_min_change=99,
        max_daily_runtime=timedelta(minutes=30),
        override_active=True,
    )
    decision = _run([app], [_running(runtime_today=timedelta(minutes=31))], 5000)[0]
    assert decision.action == Action.OFF
