"""Grid support must remain stable with physical feedback between cycles."""

from dataclasses import replace
from datetime import timedelta

import pytest

from custom_components.pv_excess_control.models import Action
from tests.test_optimizer import (
    _make_appliance,
    _make_state,
    _make_power,
    _make_tariff,
    _optimizer_for_tests,
    _empty_plan,
)


def run(apps, states, excess, *, average=None, cheap=True):
    power = _make_power(excess=excess)
    return (
        _optimizer_for_tests()
        .optimize(
            power,
            apps,
            states,
            _empty_plan(),
            [_make_power(excess=excess if average is None else average)],
            _make_tariff(current_price=0.05 if cheap else 0.40),
        )
        .decisions
    )


def test_standard_grid_start_and_running_feedback_stays_supported():
    app = _make_appliance(allow_grid_supplement=True, max_grid_power=700)
    state = _make_state()
    for _ in range(4):
        decision = run([app], [state], 300 - state.current_power)[0]
        assert decision.action == Action.ON
        assert decision.uses_grid_supplement
        assert decision.grid_supplement_watts == pytest.approx(700)
        state = _make_state(is_on=True, current_power=1000)
    assert run([app], [state], -700, cheap=False)[0].action == Action.OFF
    assert run([replace(app, allow_grid_supplement=False)], [state], -700)[0].action == Action.OFF


def test_grid_start_checks_instantaneous_cap():
    app = _make_appliance(allow_grid_supplement=True, max_grid_power=700)
    assert run([app], [_make_state()], 100, average=500)[0].action == Action.IDLE


def test_full_grid_support_does_not_pay_unrelated_household_deficit():
    app = _make_appliance(allow_grid_supplement=True)
    state = _make_state(is_on=True, current_power=1000)
    decision = run([app], [state], -1500)[0]
    assert decision.action == Action.ON
    assert decision.grid_supplement_watts == 1000


def test_unused_grid_headroom_cannot_start_lower_priority_solar_load():
    apps = [
        _make_appliance(id="high", allow_grid_supplement=True, max_grid_power=1000),
        _make_appliance(id="low", priority=2, nominal_power=400),
    ]
    states = [_make_state(id="high", is_on=True, current_power=1000), _make_state(id="low")]
    decisions = run(apps, states, 0)
    assert decisions[1].action == Action.IDLE
    assert not decisions[0].uses_grid_supplement


def dynamic(**kwargs):
    return _make_appliance(
        dynamic_current=True,
        current_entity="number.amps",
        min_current=6,
        max_current=16,
        current_step=1,
        nominal_power=3680,
        allow_grid_supplement=True,
        **kwargs,
    )


@pytest.mark.parametrize("target", [None, 10])
def test_dynamic_cap_is_import_portion_and_feedback_stable(target):
    app = dynamic(max_grid_power=500, cheap_grid_target_current=target)
    state = _make_state()
    expected_amps = 6 if target is None else 10
    solar = expected_amps * 230 - 460
    for _ in range(4):
        decision = run([app], [state], solar - state.current_power)[0]
        assert decision.action == Action.SET_CURRENT
        assert decision.target_current == expected_amps
        assert decision.grid_supplement_watts == pytest.approx(460)
        state = _make_state(is_on=True, current_power=expected_amps * 230)


def test_dynamic_default_rejects_import_above_cap():
    assert run([dynamic(max_grid_power=500)], [_make_state()], 500)[0].action == Action.IDLE


def test_two_grid_loads_do_not_duplicate_solar_across_cycles():
    apps = [
        _make_appliance(id="high", allow_grid_supplement=True, max_grid_power=700),
        _make_appliance(id="low", priority=2, allow_grid_supplement=True, max_grid_power=700),
    ]
    states = [_make_state(id="high"), _make_state(id="low")]
    for _ in range(4):
        decisions = run(apps, states, 600 - sum(s.current_power for s in states))
        assert decisions[0].action == Action.ON
        assert decisions[1].action == Action.IDLE
        states[0] = _make_state(id="high", is_on=True, current_power=1000)


@pytest.mark.parametrize("kind", ["max_runtime", "disconnected", "override", "on_only"])
def test_safety_early_returns_get_no_running_grid_credit(kind):
    app = _make_appliance(id="high", allow_grid_supplement=True)
    state = _make_state(id="high", is_on=True, current_power=1000)
    if kind == "max_runtime":
        app = replace(app, max_daily_runtime=timedelta(minutes=1))
        state = replace(state, runtime_today=timedelta(minutes=2))
    elif kind == "disconnected":
        app = replace(app, ev_connected_entity="binary_sensor.connected")
        state = replace(state, ev_connected=False)
    elif kind == "override":
        app = replace(app, override_active=True)
    else:
        app = replace(app, on_only=True)
    low = _make_appliance(id="low", priority=2, nominal_power=100)
    decisions = run([app, low], [state, _make_state(id="low")], -500)
    assert decisions[1].action == Action.IDLE
    assert not decisions[0].uses_grid_supplement


def test_running_priority_reserves_solar_when_lower_load_exceeds_its_grid_cap():
    apps = [
        _make_appliance(id="high", allow_grid_supplement=True, max_grid_power=700),
        _make_appliance(id="low", priority=2, allow_grid_supplement=True, max_grid_power=700),
    ]
    states = [_make_state(id=a.id, is_on=True, current_power=1000) for a in apps]
    decisions = run(apps, states, -1400)
    assert decisions[0].action == Action.ON
    assert decisions[0].grid_supplement_watts == 400
    assert decisions[1].action == Action.OFF


def test_dynamic_ramp_releases_solar_once_and_stays_stable():
    apps = [dynamic(id="high"), _make_appliance(id="low", priority=2, nominal_power=400)]
    states = [_make_state(id="high", is_on=True, current_power=3680), _make_state(id="low")]
    for _ in range(4):
        decisions = run(apps, states, 1840 - sum(s.current_power for s in states))
        assert decisions[0].action == Action.SET_CURRENT
        assert decisions[0].target_current == 8
        assert decisions[1].action == Action.IDLE
        states[0] = _make_state(id="high", is_on=True, current_power=1840)


def test_old_solar_average_cannot_bypass_grid_cap_with_daily_minimum():
    app = _make_appliance(
        allow_grid_supplement=True, max_grid_power=700, min_daily_runtime=timedelta(hours=1)
    )
    assert run([app], [_make_state()], 100, average=1500)[0].action == Action.IDLE


def test_idle_dynamic_load_cannot_lend_its_nominal_draw_to_other_consumers():
    apps = [
        replace(dynamic(id="high"), actual_power_entity="sensor.charger_power"),
        _make_appliance(id="low", priority=2, nominal_power=400),
    ]
    states = [_make_state(id="high", is_on=True, current_power=0), _make_state(id="low")]
    decisions = run(apps, states, 0)
    assert decisions[0].target_current == 6
    assert decisions[0].grid_supplement_watts == 1380
    assert decisions[1].action == Action.IDLE


def test_grid_protection_uses_structured_fields_independent_of_status_text():
    app = _make_appliance(allow_grid_supplement=True)
    state = _make_state(is_on=True, current_power=1000)
    optimizer = _optimizer_for_tests()
    power = _make_power(excess=-1000)
    decision = optimizer.optimize(
        power, [app], [state], _empty_plan(), [power], _make_tariff(current_price=0.05)
    ).decisions[0]
    decisions = [replace(decision, reason="Translated user-facing description")]
    optimizer._shed(decisions, [app], {app.id: state}, -1000)
    assert decisions[0].action == Action.ON
    assert optimizer._battery_discharge_protection(decisions, [app]).should_limit
    decisions = [replace(decision, uses_grid_supplement=False, reason="Grid supplement words only")]
    assert not optimizer._battery_discharge_protection(decisions, [app]).should_limit


def test_preemption_cannot_free_nominal_power_of_a_new_dynamic_start():
    apps = [
        _make_appliance(id="high", nominal_power=1500),
        dynamic(id="middle", priority=2, max_grid_power=500),
        dynamic(id="low", priority=3, max_grid_power=3000),
    ]
    states = [_make_state(id=a.id) for a in apps]
    decisions = run(apps, states, 1500)
    assert decisions[0].action == Action.IDLE
    assert decisions[1].action == Action.SET_CURRENT
    assert decisions[2].grid_supplement_watts == 1260


def test_cheap_start_without_hysteresis_buffer_does_not_claim_grid_import():
    app = _make_appliance(allow_grid_supplement=True)
    decision = run([app], [_make_state()], 1000)[0]
    assert decision.action == Action.ON
    assert not decision.uses_grid_supplement
    assert decision.grid_supplement_watts == 0


@pytest.mark.parametrize("is_on", [False, True])
def test_unavailable_mapped_draw_holds_without_grid_credit_or_start(is_on):
    app = replace(dynamic(id="high"), actual_power_entity="sensor.charger_power")
    low = _make_appliance(id="low", priority=2, nominal_power=400)
    state = _make_state(id="high", is_on=is_on)
    state.current_power_available = False
    decisions = run([app, low], [state, _make_state(id="low")], -500)
    assert decisions[0].action == (Action.ON if is_on else Action.IDLE)
    assert decisions[0].uses_grid_supplement is is_on
    assert decisions[0].grid_supplement_watts == 0
    assert decisions[1].action == Action.IDLE


def test_unavailable_appliance_power_does_not_hide_battery_soc_safety():
    app = replace(dynamic(), actual_power_entity="sensor.charger_power")
    state = _make_state(is_on=True)
    state.current_power_available = False
    power = replace(_make_power(excess=-500), battery_soc=10)
    decision = (
        _optimizer_for_tests()
        .optimize(
            power,
            [app],
            [state],
            _empty_plan(),
            [power],
            _make_tariff(current_price=0.05),
            min_battery_soc=20,
        )
        .decisions[0]
    )
    assert decision.action == Action.OFF
    assert decision.bypasses_cooldown


@pytest.mark.parametrize("unmanaged_id", ["paused", "disabled"])
def test_unmanaged_running_states_are_not_added_back_to_solar(unmanaged_id):
    app = _make_appliance(id="active", allow_grid_supplement=True)
    states = [
        _make_state(id=unmanaged_id, is_on=True, current_power=1000),
        _make_state(id="active", is_on=True, current_power=1000),
    ]
    decision = run([app], states, -1000)[0]
    assert decision.action == Action.ON
    assert decision.uses_grid_supplement
    assert decision.grid_supplement_watts == 1000


def test_preemption_of_valid_zero_load_cannot_start_higher_priority_consumer():
    high = _make_appliance(id="high", nominal_power=500)
    idle = replace(
        _make_appliance(id="idle", priority=3, allow_grid_supplement=True, max_grid_power=0),
        actual_power_entity="sensor.idle",
    )
    decisions = run([high, idle], [_make_state(id="high"), _make_state(id="idle", is_on=True)], 0)
    assert decisions[0].action == Action.IDLE
    assert decisions[1].action == Action.ON


@pytest.mark.parametrize("excess, action", [(0, Action.ON), (-500, Action.OFF)])
def test_leaving_cheap_window_does_not_invent_draw_from_valid_zero(excess, action):
    app = replace(dynamic(), actual_power_entity="sensor.idle")
    state = replace(_make_state(is_on=True), current_amperage=16)
    decision = run([app], [state], excess, cheap=False)[0]
    assert decision.action == action
    assert decision.target_current is None
    assert not decision.uses_grid_supplement


def test_shedding_valid_zero_load_does_not_hide_remaining_deficit():
    apps = [
        _make_appliance(id="high"),
        replace(_make_appliance(id="idle", priority=3), actual_power_entity="sensor.idle"),
    ]
    states = [
        _make_state(id="high", is_on=True, current_power=1000),
        _make_state(id="idle", is_on=True),
    ]
    decisions = run(apps, states, -500, cheap=False)
    assert decisions[0].action == Action.OFF
    assert decisions[1].action == Action.OFF


def test_dynamic_shed_releases_commanded_power_once_after_allocation():
    app = dynamic()
    state = _make_state(is_on=True, current_power=3680)
    optimizer = _optimizer_for_tests()
    power = _make_power(excess=0)
    optimizer.optimize(power, [app], [state], _empty_plan(), [power], _make_tariff())
    from custom_components.pv_excess_control.models import ControlDecision

    decisions = [ControlDecision(app.id, Action.SET_CURRENT, 6, "Solar target", False)]
    budget = optimizer._shed(decisions, [app], {app.id: state}, -500)
    assert decisions[0].action == Action.OFF
    assert budget == 880  # -500 + the already-committed 1380W


def test_dynamic_override_debits_full_target_when_measured_draw_is_zero():
    app = replace(dynamic(), actual_power_entity="sensor.idle", override_active=True)
    state = replace(_make_state(is_on=True), current_amperage=16)
    decision, debit = _optimizer_for_tests()._allocate_appliance(
        app,
        state,
        avg_budget=0,
        instant_budget=0,
        plan=_empty_plan(),
        tariff=_make_tariff(),
    )
    assert decision.target_current == 16
    assert debit == 3680


def test_running_grid_support_keeps_discharge_block_when_meter_unavailable():
    app = replace(dynamic(), actual_power_entity="sensor.charger_power")
    optimizer = _optimizer_for_tests()
    power = _make_power(excess=-1380)
    tariff = _make_tariff(current_price=0.05)
    state = _make_state(is_on=True, current_power=1380)
    first = optimizer.optimize(power, [app], [state], _empty_plan(), [power], tariff)
    assert first.battery_discharge_action.should_limit
    state = replace(state, current_power=0, current_power_available=False)
    held = optimizer.optimize(power, [app], [state], _empty_plan(), [power], tariff)
    assert held.decisions[0].action == Action.ON
    assert held.decisions[0].target_current is None
    assert held.decisions[0].uses_grid_supplement
    assert held.decisions[0].grid_supplement_watts == 0
    assert held.battery_discharge_action.should_limit
    assert held.battery_discharge_action.max_discharge_watts == 0


def test_unavailable_draw_holds_state_after_tariff_support_ends():
    """Meter recovery is required to resume ordinary power regulation."""
    app = replace(dynamic(), actual_power_entity="sensor.charger_power")
    state = replace(_make_state(is_on=True), current_power_available=False)
    power = _make_power(excess=-1380)
    result = _optimizer_for_tests().optimize(
        power,
        [app],
        [state],
        _empty_plan(),
        [power],
        _make_tariff(current_price=0.40),
    )
    assert result.decisions[0].action == Action.ON
    assert not result.decisions[0].uses_grid_supplement
    assert not result.battery_discharge_action.should_limit


@pytest.mark.parametrize("is_dynamic", [False, True])
@pytest.mark.parametrize("solar_offset", [-1, 0, 1])
def test_tariff_start_is_monotonic_at_full_solar_coverage(is_dynamic, solar_offset):
    app = (
        dynamic(max_grid_power=700)
        if is_dynamic
        else _make_appliance(
            allow_grid_supplement=True,
            max_grid_power=700,
        )
    )
    required = 1380 if is_dynamic else 1000
    decision = run([app], [_make_state()], required + solar_offset)[0]
    assert decision.action == (Action.SET_CURRENT if is_dynamic else Action.ON)
    if is_dynamic:
        assert decision.target_current == 6
    assert decision.grid_supplement_watts == max(-solar_offset, 0)
    assert decision.uses_grid_supplement is (solar_offset < 0)


@pytest.mark.parametrize("is_dynamic", [False, True])
@pytest.mark.parametrize("solar_offset", [-1, 0, 1])
def test_solar_only_start_keeps_buffer_at_full_coverage(is_dynamic, solar_offset):
    app = dynamic() if is_dynamic else _make_appliance(allow_grid_supplement=True)
    required = 1380 if is_dynamic else 1000
    decision = run([app], [_make_state()], required + solar_offset, cheap=False)[0]
    assert decision.action == Action.IDLE
    assert not decision.uses_grid_supplement
