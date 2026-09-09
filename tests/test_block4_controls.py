"""Generic gate, delayed solar allocation and phase-accounting regressions."""

from dataclasses import replace
from datetime import timedelta

import pytest

from custom_components.pv_excess_control.const import Action
from custom_components.pv_excess_control.optimizer import Optimizer
from tests.test_optimizer import (
    _empty_plan,
    _make_appliance,
    _make_power,
    _make_state,
    _make_tariff,
)


def run(apps, states, excess=4000, **kwargs):
    power = _make_power(excess)
    tariff = kwargs.pop("tariff", _make_tariff())
    return (
        Optimizer(min_good_samples=1, **kwargs)
        .optimize(power, apps, states, _empty_plan(), [power], tariff)
        .decisions
    )


@pytest.mark.parametrize("allowed", [False, None])
@pytest.mark.parametrize("on", [False, True])
@pytest.mark.parametrize("mode", ["start_only", "while_running"])
def test_condition_gate_mode_and_unknown(allowed, on, mode):
    app = replace(
        _make_appliance(on_only=True),
        enable_condition_entity="binary_sensor.ready",
        enable_condition_mode=mode,
    )
    state = replace(_make_state(is_on=on), enable_condition=allowed)
    decision = run([app], [state])[0]
    expected = (
        Action.ON if on and mode == "start_only" else Action.OFF if on else Action.IDLE
    )
    assert decision.action == expected
    if expected == Action.OFF:
        assert decision.bypasses_cooldown


def test_explicit_override_bypasses_condition():
    app = replace(
        _make_appliance(override_active=True),
        enable_condition_entity="binary_sensor.ready",
    )
    assert (
        run([app], [replace(_make_state(), enable_condition=False)])[0].action
        == Action.ON
    )


def test_delayed_start_does_not_reserve_budget_or_start_dependency():
    app = replace(
        _make_appliance(nominal_power=1000, requires_appliance="helper"), start_delay=60
    )
    helper = _make_appliance(id="helper", nominal_power=100, helper_only=True)
    second = _make_appliance(id="second", priority=2, nominal_power=1000)
    states = [_make_state(), _make_state(id="helper"), _make_state(id="second")]
    decisions = {d.appliance_id: d for d in run([app, helper, second], states, 1500)}
    assert decisions["app_1"].action == Action.IDLE
    assert decisions["app_1"].solar_start_qualified
    assert decisions["helper"].action == Action.IDLE
    assert decisions["second"].action == Action.ON


def test_delay_passes_only_after_continuous_qualification():
    app = replace(_make_appliance(nominal_power=1000), start_delay=60)
    state = replace(_make_state(), solar_start_elapsed=60)
    assert run([app], [state], 1500)[0].action == Action.ON
    assert not run([app], [state], 0)[0].solar_start_qualified


def test_delayed_preemption_qualifies_without_shedding():
    app = replace(_make_appliance(nominal_power=1000), start_delay=60)
    lower = _make_appliance(id="lower", priority=2, nominal_power=2000)
    states = [_make_state(), _make_state(id="lower", is_on=True, current_power=2000)]
    decisions = run([app, lower], states, 0)
    assert decisions[0].solar_start_qualified and decisions[0].action == Action.IDLE
    assert decisions[1].action == Action.ON
    decisions = run(
        [app, lower], [replace(states[0], solar_start_elapsed=60), states[1]], 0
    )
    assert decisions[0].action == Action.ON and decisions[1].action == Action.OFF


def test_cheap_grid_start_bypasses_delay():
    app = replace(
        _make_appliance(nominal_power=1000, allow_grid_supplement=True), start_delay=60
    )
    assert (
        run([app], [_make_state()], 0, tariff=_make_tariff(current_price=-0.1))[
            0
        ].action
        == Action.ON
    )


@pytest.mark.parametrize("running", [False, True])
def test_positive_buffer_remains_after_dynamic_allocation(running):
    app = _make_appliance(
        dynamic_current=True,
        current_entity="number.amps",
        min_current=6,
        max_current=16,
        current_step=1,
    )
    state = replace(
        _make_state(is_on=running, current_power=1380 if running else 0),
        current_amperage=6 if running else None,
    )
    decision = run([app], [state], 2300 if not running else 920, off_threshold=300)[0]
    assert decision.target_current == 8  # leaves 460W, not 0W


def test_fixed_start_respects_positive_off_buffer():
    app = _make_appliance(nominal_power=1000, on_threshold=0)
    assert run([app], [_make_state()], 1200, off_threshold=300)[0].action == Action.IDLE


def test_dynamic_on_only_regulates_down_to_minimum_without_shedding():
    app = _make_appliance(
        dynamic_current=True,
        current_entity="number.amps",
        min_current=6,
        max_current=16,
        on_only=True,
    )
    state = replace(_make_state(is_on=True, current_power=3680), current_amperage=16)
    decision = run([app], [state], -5000)[0]
    assert decision.action == Action.SET_CURRENT and decision.target_current == 6
    assert run([app], [_make_state()], 0)[0].action == Action.IDLE


def test_deadline_start_bypasses_delay_even_with_sufficient_solar():
    from datetime import datetime

    deadline = (datetime.now() + timedelta(minutes=10)).time()
    app = replace(
        _make_appliance(nominal_power=1000, min_daily_runtime=timedelta(minutes=30)),
        schedule_deadline=deadline,
        start_delay=60,
    )
    assert run([app], [_make_state()], 4000)[0].action == Action.ON


def test_dynamic_delay_does_not_start_dependency_until_ready():
    app = replace(
        _make_appliance(
            dynamic_current=True,
            current_entity="number.amps",
            min_current=6,
            max_current=16,
            requires_appliance="helper",
        ),
        start_delay=60,
    )
    helper = _make_appliance(id="helper", nominal_power=1000, helper_only=True)
    states = [_make_state(), _make_state(id="helper")]
    assert run([app, helper], states, 2000)[0].action == Action.IDLE
    decisions = run(
        [app, helper], [replace(states[0], solar_start_elapsed=60), states[1]], 3000
    )
    assert decisions[0].action == Action.SET_CURRENT
    assert decisions[0].target_current * 230 + 1000 <= 3000
    assert decisions[1].action == Action.ON


def test_coordinator_phase_fallback_and_global_threshold():
    from tests.test_block3_coordinator import ev_coordinator
    from tests.test_init import MockState

    coord = ev_coordinator()
    coord.config_entry.data["on_threshold"] = 350
    coord.config_entry.subentries["ev"].data.update(
        phase_count_entity="sensor.phases", phases=3
    )
    assert coord._get_appliance_configs()[0].phases == 3
    coord.hass.states._states["sensor.phases"] = MockState("1")
    assert coord._get_appliance_configs()[0].phases == 1
    assert coord._get_appliance_config_by_id("ev").on_threshold == 350
    coord.hass.states._states["sensor.phases"] = MockState("0")
    assert coord._get_appliance_configs()[0].phases == 1
    coord.hass.states._states["sensor.phases"] = MockState("1.5")
    assert coord._get_appliance_config_by_id("ev").phases == 1


def test_coordinator_start_delay_uses_monotonic_and_resets(monkeypatch):
    from custom_components.pv_excess_control.models import ControlDecision
    from tests.test_block3_coordinator import ev_coordinator

    coord = ev_coordinator(on="off")
    coord.config_entry.subentries["ev"].data.update(
        start_delay=60, enable_condition_entity="binary_sensor.connected"
    )
    monkeypatch.setattr(
        "custom_components.pv_excess_control.coordinator._time.monotonic", lambda: 100
    )
    pending = ControlDecision(
        "ev", Action.IDLE, None, "pending", False, solar_start_qualified=True
    )
    coord._update_start_qualification([pending])
    monkeypatch.setattr(
        "custom_components.pv_excess_control.coordinator._time.monotonic", lambda: 145
    )
    state = coord._get_appliance_states(coord._get_appliance_configs())["ev"]
    assert state.solar_start_elapsed == 45 and state.enable_condition is True
    coord._update_start_qualification([replace(pending, solar_start_qualified=False)])
    assert (
        coord._get_appliance_states(coord._get_appliance_configs())[
            "ev"
        ].solar_start_elapsed
        == 0
    )


def test_new_controls_schemas_accept_gates_phases_and_positive_threshold():
    from custom_components.pv_excess_control.config_flow import (
        _appliance_constraints_schema,
        _appliance_current_schema,
        _settings_schema,
    )

    assert (
        _appliance_current_schema()({"phase_count_entity": "sensor.phases"})[
            "phase_count_entity"
        ]
        == "sensor.phases"
    )
    values = _appliance_constraints_schema()(
        {
            "enable_condition_entity": "input_boolean.ready",
            "enable_condition_mode": "while_running",
            "start_delay": 90,
        }
    )
    assert values["start_delay"] == 90
    assert (
        _settings_schema()({"off_threshold": 300, "on_threshold": 350})["off_threshold"]
        == 300
    )


async def test_grid_switch_and_global_price_controls_persist():
    from unittest.mock import MagicMock

    from custom_components.pv_excess_control.number import GlobalPriceThresholdNumber
    from custom_components.pv_excess_control.switch import ApplianceGridSupplementSwitch
    from tests.test_block3_coordinator import ev_coordinator

    coord = ev_coordinator()
    coord.hass.config_entries = MagicMock()
    entity = ApplianceGridSupplementSwitch(coord, "ev", "EV")
    entity.hass = coord.hass
    entity.async_write_ha_state = MagicMock()
    await entity.async_turn_on()
    args = coord.hass.config_entries.async_update_subentry.call_args
    assert args.kwargs["data"]["allow_grid_supplement"] is True
    price = GlobalPriceThresholdNumber(coord, "cheap_price_threshold")
    price.hass = coord.hass
    price.async_write_ha_state = MagicMock()
    await price.async_set_native_value(-0.25)
    assert (
        coord.hass.config_entries.async_update_entry.call_args.kwargs["data"][
            "cheap_price_threshold"
        ]
        == -0.25
    )


def test_start_delay_does_not_qualify_on_stale_average_without_instant_power():
    app = replace(_make_appliance(nominal_power=1000), start_delay=60)
    decision = (
        Optimizer(min_good_samples=1)
        .optimize(
            _make_power(0),
            [app],
            [_make_state()],
            _empty_plan(),
            [_make_power(4000)],
            _make_tariff(),
        )
        .decisions[0]
    )
    assert decision.action == Action.IDLE and not decision.solar_start_qualified


def test_dependency_condition_cannot_be_bypassed_by_dependent_start():
    app = _make_appliance(nominal_power=1000, requires_appliance="dependency")
    dep = replace(
        _make_appliance(id="dependency", priority=2, nominal_power=100),
        enable_condition_entity="binary_sensor.ready",
    )
    states = [
        _make_state(),
        replace(_make_state(id="dependency"), enable_condition=False),
    ]
    assert all(d.action == Action.IDLE for d in run([app, dep], states))


def test_dynamic_minimum_is_shed_when_positive_buffer_cannot_be_kept():
    app = _make_appliance(
        dynamic_current=True,
        current_entity="number.amps",
        min_current=6,
        max_current=16,
    )
    state = replace(_make_state(is_on=True, current_power=1380), current_amperage=6)
    assert run([app], [state], 100, off_threshold=300)[0].action == Action.OFF


@pytest.mark.parametrize(
    "step", ["async_step_constraints", "async_step_reconfigure_constraints"]
)
@pytest.mark.parametrize(
    "extra", [{"enable_condition_entity": "input_boolean.ready"}, {"start_delay": 30}]
)
async def test_helper_rejects_independent_gate_and_delay(step, extra):
    from tests.test_config_flow_subentry import (
        VALID_BASIC_INPUT,
        VALID_CONSTRAINTS_INPUT,
        VALID_CURRENT_INPUT_DISABLED,
        _make_subentry_flow,
    )

    flow = _make_subentry_flow()
    flow._data = {**VALID_BASIC_INPUT, **VALID_CURRENT_INPUT_DISABLED}
    result = await getattr(flow, step)(
        {**VALID_CONSTRAINTS_INPUT, "helper_only": True, **extra}
    )
    assert result["errors"]["helper_only"] == "helper_only_with_gate"


def test_dependent_waits_for_independent_dependency_start_delay():
    app = _make_appliance(nominal_power=1000, requires_appliance="dependency")
    dep = replace(
        _make_appliance(id="dependency", priority=2, nominal_power=100), start_delay=60
    )
    states = [_make_state(), _make_state(id="dependency")]
    decisions = run([app, dep], states)
    assert decisions[0].action == Action.IDLE
    assert decisions[1].action == Action.IDLE and decisions[1].solar_start_qualified


def test_dynamic_on_only_reduces_to_minimum_when_grid_allowance_insufficient():
    app = _make_appliance(
        dynamic_current=True,
        current_entity="number.amps",
        min_current=6,
        max_current=16,
        on_only=True,
        allow_grid_supplement=True,
        max_grid_power=100,
    )
    state = replace(_make_state(is_on=True, current_power=3680), current_amperage=16)
    decision = run([app], [state], -5000, tariff=_make_tariff(current_price=-0.1))[0]
    assert decision.action == Action.SET_CURRENT and decision.target_current == 6
    assert decision.grid_supplement_watts <= 100


@pytest.mark.parametrize(
    "key", ["cheap_price_threshold", "battery_charge_price_threshold"]
)
async def test_global_price_number_listener_replans_without_startup_blackout(key):
    from unittest.mock import AsyncMock, MagicMock

    from custom_components.pv_excess_control import _async_update_listener
    from custom_components.pv_excess_control.const import DOMAIN
    from custom_components.pv_excess_control.number import GlobalPriceThresholdNumber
    from tests.test_block3_coordinator import ev_coordinator

    coord = ev_coordinator()
    entry = coord.config_entry
    eid = entry.entry_id
    coord.hass.data[DOMAIN] = {
        eid: coord,
        f"{eid}_config_snapshot": dict(entry.data),
        f"{eid}_subentry_count": 1,
        f"{eid}_subentries_snapshot": {"ev": dict(entry.subentries["ev"].data)},
    }
    startup = coord._startup_time
    coord.current_plan = _empty_plan()
    coord._planner_counter = 0
    coord.hass.config_entries.async_update_entry = MagicMock(
        side_effect=lambda entry, data: setattr(entry, "data", data)
    )
    coord.hass.config_entries.async_reload = AsyncMock()
    number = GlobalPriceThresholdNumber(coord, key)
    number.hass = coord.hass
    number.async_write_ha_state = MagicMock()
    await number.async_set_native_value(-0.05)
    await _async_update_listener(coord.hass, entry)
    coord.hass.config_entries.async_reload.assert_not_awaited()
    assert coord._startup_time == startup
    assert coord.current_plan is None
    assert (
        coord._planner_counter
        >= coord._planner_interval / coord.update_interval.total_seconds()
    )
    assert coord.hass.data[DOMAIN][f"{eid}_config_snapshot"][key] == -0.05
    assert getattr(coord._get_tariff_info(), key) == -0.05


async def test_grid_switch_listener_replans_without_reload():
    from unittest.mock import AsyncMock, MagicMock

    from custom_components.pv_excess_control import _async_update_listener
    from custom_components.pv_excess_control.const import DOMAIN
    from custom_components.pv_excess_control.switch import ApplianceGridSupplementSwitch
    from tests.test_block3_coordinator import ev_coordinator

    coord = ev_coordinator()
    entry = coord.config_entry
    eid = entry.entry_id
    coord.hass.data[DOMAIN] = {
        eid: coord,
        f"{eid}_config_snapshot": dict(entry.data),
        f"{eid}_subentry_count": 1,
        f"{eid}_subentries_snapshot": {"ev": dict(entry.subentries["ev"].data)},
    }
    coord._planner_counter = 0
    coord.hass.config_entries.async_update_subentry = MagicMock(
        side_effect=lambda entry, subentry, data: setattr(subentry, "data", data)
    )
    coord.hass.config_entries.async_reload = AsyncMock()
    switch = ApplianceGridSupplementSwitch(coord, "ev", "EV")
    switch.hass = coord.hass
    switch.async_write_ha_state = MagicMock()
    await switch.async_turn_on()
    await _async_update_listener(coord.hass, entry)
    coord.hass.config_entries.async_reload.assert_not_awaited()
    assert coord._get_appliance_configs()[0].allow_grid_supplement
    assert (
        coord._planner_counter
        >= coord._planner_interval / coord.update_interval.total_seconds()
    )
