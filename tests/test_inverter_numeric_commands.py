"""Numeric hold commands require exact native-unit setpoint confirmation."""
from dataclasses import replace

import pytest

from custom_components.pv_excess_control.inverter_control import InverterGridChargeController
from custom_components.pv_excess_control.models import InverterGridChargeConfig


def mapping(**changes):
    return replace(InverterGridChargeConfig(
        enable_entity_id="input_number.discharge",
        enable_engage_value="10",
        enable_disengage_value="5000",
        enable_feedback_entity_id="sensor.discharge_limit",
        timeout_seconds=.02,
    ), **changes)


def install(hass, *, attrs=None, feedback=None, domain="input_number"):
    attributes = attrs or {"min": 10, "max": 5000, "step": 10, "unit_of_measurement": "W"}
    entity = f"{domain}.discharge"
    hass.states.async_set(entity, attributes["max"], attributes)
    hass.states.async_set("sensor.discharge_limit", 5000)
    calls = []

    async def write(call):
        value = call.data["value"]
        calls.append(value)
        hass.states.async_set(entity, str(float(value)), attributes)
        reported = feedback(value) if feedback else value
        hass.states.async_set("sensor.discharge_limit", str(float(reported)))

    hass.services.async_register(domain, "set_value", write)
    return calls


@pytest.mark.parametrize("domain", ["input_number", "number"])
async def test_numeric_hold_commands_accept_equivalent_decimal_feedback(hass, domain):
    calls = install(hass, domain=domain)
    controller = InverterGridChargeController(hass, mapping(enable_entity_id=f"{domain}.discharge"))
    assert controller.confirmation_level == "physical"
    await controller.engage(0)
    await controller.verify_engaged(0)
    await controller.disengage()
    assert calls == [10., 5000.]


async def test_a_hundred_watt_readback_cannot_confirm_ten_watt_hold(hass):
    calls = install(hass, feedback=lambda v: 100 if v == 10 else v)
    controller = InverterGridChargeController(hass, mapping(power_tolerance_w=100))
    with pytest.raises(RuntimeError, match="confirm"):
        await controller.engage(0)
    assert calls == [10., 5000.]


async def test_kw_command_requires_explicit_watt_feedback_values(hass):
    calls = install(hass, attrs={"min": .01, "max": 5., "step": .01, "unit_of_measurement": "kW"}, feedback=lambda v: v * 1000)
    controller = InverterGridChargeController(hass, mapping(
        enable_engage_value="0.01", enable_disengage_value="5",
        enable_feedback_engage_value="10", enable_feedback_disengage_value="5000",
    ))
    await controller.engage(0)
    await controller.disengage()
    assert calls == [.01, 5.]


async def test_different_native_feedback_units_are_not_silently_assumed(hass):
    install(hass, attrs={"min": .01, "max": 5., "step": .01, "unit_of_measurement": "kW"}, feedback=lambda v: v * 1000)
    controller = InverterGridChargeController(hass, mapping(enable_engage_value="0.01", enable_disengage_value="5"))
    with pytest.raises(RuntimeError, match="confirm"):
        await controller.engage(0)


@pytest.mark.parametrize("changes", [
    {"enable_engage_value": "nan"}, {"enable_engage_value": "inf"},
    {"enable_engage_value": "-1"}, {"enable_disengage_value": "broken"},
    {"enable_engage_value": True}, {"enable_disengage_value": "10.0"},
    {"enable_feedback_engage_value": "NaN"},
    {"enable_feedback_engage_value": "20", "enable_feedback_disengage_value": "20.0"},
])
def test_invalid_numeric_mapping_is_rejected_before_use(hass, changes):
    with pytest.raises(ValueError, match="[Nn]umeric"):
        InverterGridChargeController(hass, mapping(**changes))


@pytest.mark.parametrize("changes", [
    {"enable_engage_value": "0"},
    {"enable_engage_value": "15"},
    {"enable_disengage_value": "6000"},
])
async def test_invalid_engage_or_fallback_setpoint_does_not_write(hass, changes):
    calls = install(hass)
    controller = InverterGridChargeController(hass, mapping(**changes))
    with pytest.raises(ValueError):
        await controller.engage(0)
    assert calls == []


async def test_cleanup_remains_possible_after_minimum_was_raised(hass):
    calls = install(hass, attrs={"min": 100, "max": 5000, "step": 100})
    controller = InverterGridChargeController(hass, mapping())
    await controller.disengage()
    assert calls == [5000.]


@pytest.mark.parametrize("invalid", [
    {"step": 0}, {"step": float("nan")}, {"step": True},
    {"min": float("nan")}, {"max": float("inf")}, {"min": 6000},
])
async def test_invalid_numeric_metadata_never_writes(hass, invalid):
    calls = install(hass, attrs={"min": 10, "max": 5000, "step": 10, **invalid})
    controller = InverterGridChargeController(hass, mapping())
    with pytest.raises(ValueError):
        await controller.engage(0)
    assert calls == []


async def test_invalid_numeric_mode_fallback_is_checked_before_any_command(hass):
    calls = install(hass)
    hass.states.async_set("input_select.command", "Stop")
    hass.states.async_set("sensor.command", "Stop")
    controller = InverterGridChargeController(hass, mapping(
        enable_entity_id="input_select.command", enable_engage_value="Charge",
        enable_disengage_value="Stop", enable_feedback_entity_id="sensor.command",
        mode_entity_id="input_number.discharge", mode_engage_value="10",
        mode_disengage_value="6000", mode_feedback_entity_id="sensor.discharge_limit",
    ))
    with pytest.raises(ValueError):
        await controller.engage(0)
    assert calls == []


async def test_release_reasserts_bridge_after_an_unchanged_helper_did_not_reach_hardware(hass):
    calls = install(hass, feedback=lambda value: 10)
    hass.states.async_set("sensor.discharge_limit", 10)
    hass.states.async_set("automation.discharge_bridge", "on")
    triggers = []

    async def trigger(call):
        triggers.append(dict(call.data))
        value = hass.states.get("input_number.discharge").state
        hass.states.async_set("sensor.discharge_limit", value)

    hass.services.async_register("automation", "trigger", trigger)
    controller = InverterGridChargeController(hass, mapping(
        enable_reassert_entity_id="automation.discharge_bridge",
    ))
    state = hass.states.get("input_number.discharge")
    hass.states.async_set(state.entity_id, 10, state.attributes)
    # The first changed-helper command fails at the physical bridge; a retry
    # must still write the register although the helper now already says 5000.
    with pytest.raises(RuntimeError, match="confirm"):
        await controller.disengage()
    assert triggers == []
    await controller.disengage()
    assert calls == [5000., 5000.]
    assert triggers == [{"entity_id": "automation.discharge_bridge", "skip_condition": False}]


@pytest.mark.parametrize("initial_helper,initial_feedback", [(10, 10), (5000, 5000)])
async def test_confirmed_or_changed_numeric_command_needs_no_bridge_reassert(hass, initial_helper, initial_feedback):
    calls = install(hass)
    state = hass.states.get("input_number.discharge")
    hass.states.async_set(state.entity_id, initial_helper, state.attributes)
    hass.states.async_set("sensor.discharge_limit", initial_feedback)
    hass.states.async_set("automation.discharge_bridge", "on")
    triggers = []

    async def trigger(call):
        triggers.append(call)

    hass.services.async_register("automation", "trigger", trigger)
    controller = InverterGridChargeController(hass, mapping(enable_reassert_entity_id="automation.discharge_bridge"))
    await controller.engage(0)
    assert calls == [10.]
    assert not triggers


@pytest.mark.parametrize("entity", ["script.bridge", "input_boolean.bridge", "broken", "automation.bad name"])
def test_reassert_mapping_only_accepts_an_explicit_automation(hass, entity):
    with pytest.raises(ValueError):
        InverterGridChargeController(hass, mapping(enable_reassert_entity_id=entity))
