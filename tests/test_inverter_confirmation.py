"""Service-level tests for bounded inverter command confirmation and cleanup."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.pv_excess_control.inverter_control import InverterGridChargeController
from custom_components.pv_excess_control.models import InverterGridChargeConfig


def config(**overrides):
    values = dict(
        enable_entity_id="input_select.charge",
        enable_engage_value="Forced charge",
        enable_disengage_value="Stop",
        mode_entity_id="input_select.mode",
        mode_engage_value="Forced",
        mode_disengage_value="Self",
        power_entity_id="input_number.power",
        enable_feedback_entity_id="sensor.charge",
        mode_feedback_entity_id="sensor.mode",
        power_feedback_entity_id="sensor.power",
        timeout_seconds=0.03,
    )
    values.update(overrides)
    return InverterGridChargeConfig(**values)


def setup_inverter(hass, *, ignore=None, fail=None, delay=0, power_unit="W"):
    """Register HA services backed by separate control and physical states."""
    calls = []
    for entity_id, value in (("input_select.charge", "Stop"), ("input_select.mode", "Self")):
        hass.states.async_set(entity_id, value)
        hass.states.async_set(entity_id.replace("input_select", "sensor"), value)
    power_attrs = {"unit_of_measurement": power_unit, "min": 0, "max": 10 if power_unit == "kW" else 10000}
    hass.states.async_set("input_number.power", 0, power_attrs)
    hass.states.async_set("sensor.power", 0, {"unit_of_measurement": power_unit})

    async def service(call):
        entity_id = call.data["entity_id"]
        value = call.data.get("option", call.data.get("value"))
        calls.append((entity_id, value))
        if fail and fail(entity_id, value):
            raise RuntimeError("inverter rejected command")
        attrs = hass.states.get(entity_id).attributes
        hass.states.async_set(entity_id, value, attrs)
        if ignore and ignore(entity_id, value):
            return
        if delay:
            await asyncio.sleep(delay)
        feedback = f"sensor.{entity_id.split('.', 1)[1]}"
        hass.states.async_set(feedback, value, hass.states.get(feedback).attributes)

    hass.services.async_register("input_select", "select_option", service)
    hass.services.async_register("input_number", "set_value", service)
    return calls


async def test_engage_waits_for_physical_confirmation_after_helper_ack(hass):
    calls = setup_inverter(hass, ignore=lambda entity, value: entity == "input_select.charge" and value == "Forced charge")
    ctl = InverterGridChargeController(hass, config())
    with pytest.raises(RuntimeError, match="confirm"):
        await ctl.engage(2500)
    assert calls[-2:] == [("input_select.charge", "Stop"), ("input_select.mode", "Self")]
    assert hass.states.get("sensor.mode").state == "Self"


async def test_engage_waits_for_delayed_service_and_feedback(hass):
    setup_inverter(hass, delay=0.01)
    ctl = InverterGridChargeController(hass, config(timeout_seconds=0.1))
    await ctl.engage(2500)
    await ctl.verify_engaged(2500)
    assert hass.states.get("sensor.charge").state == "Forced charge"
    assert ctl.confirmation_level == "physical"


async def test_disengage_attempts_mode_even_when_stop_service_fails(hass):
    calls = setup_inverter(hass, fail=lambda entity, value: entity == "input_select.charge" and value == "Stop")
    ctl = InverterGridChargeController(hass, config())
    await ctl.engage(2500)
    with pytest.raises(RuntimeError):
        await ctl.disengage()
    assert calls[-2:] == [("input_select.charge", "Stop"), ("input_select.mode", "Self")]
    assert hass.states.get("sensor.mode").state == "Self"


async def test_partial_engage_attempts_both_rollback_commands_even_if_stop_fails(hass):
    calls = setup_inverter(hass, fail=lambda entity, value: entity == "input_number.power" or (entity == "input_select.charge" and value == "Stop"))
    ctl = InverterGridChargeController(hass, config())
    with pytest.raises(RuntimeError, match="rejected"):
        await ctl.engage(2500)
    assert calls[-2:] == [("input_select.charge", "Stop"), ("input_select.mode", "Self")]
    assert hass.states.get("sensor.mode").state == "Self"


async def test_disengage_attempts_mode_when_stop_readback_never_confirms(hass):
    calls = setup_inverter(hass, ignore=lambda entity, value: entity == "input_select.charge" and value == "Stop")
    ctl = InverterGridChargeController(hass, config())
    await ctl.engage(2500)
    with pytest.raises(RuntimeError, match="confirm"):
        await ctl.disengage()
    assert calls[-2:] == [("input_select.charge", "Stop"), ("input_select.mode", "Self")]


async def test_kw_power_control_and_feedback_use_watt_request(hass):
    calls = setup_inverter(hass, power_unit="kW")
    ctl = InverterGridChargeController(hass, config())
    await ctl.engage(2500)
    assert ("input_number.power", 2.5) in calls
    await ctl.verify_engaged(2500)
    hass.states.async_set("sensor.power", 2.3, {"unit_of_measurement": "kW"})
    with pytest.raises(RuntimeError, match="confirm"):
        await ctl.verify_engaged(2500)


@pytest.mark.parametrize("power", [float("nan"), float("inf"), -1, 10001])
async def test_invalid_or_out_of_range_power_does_not_write(hass, power):
    calls = setup_inverter(hass)
    ctl = InverterGridChargeController(hass, config())
    with pytest.raises(ValueError):
        await ctl.engage(power)
    assert calls == []


@pytest.mark.parametrize("value", ["unknown", "unavailable", "nan", "inf", "wrong"])
async def test_missing_or_invalid_physical_feedback_does_not_confirm(hass, value):
    setup_inverter(hass)
    ctl = InverterGridChargeController(hass, config())
    await ctl.engage(2500)
    hass.states.async_set("sensor.power", value, {"unit_of_measurement": "W"})
    with pytest.raises(RuntimeError, match="confirm"):
        await ctl.verify_engaged(2500)


async def test_missing_physical_feedback_does_not_fall_back_to_helper(hass):
    setup_inverter(hass)
    ctl = InverterGridChargeController(hass, config())
    await ctl.engage(2500)
    hass.states.async_remove("sensor.charge")
    with pytest.raises(RuntimeError, match="confirm"):
        await ctl.verify_engaged(2500)


async def test_stable_available_physical_feedback_does_not_need_state_change(hass):
    setup_inverter(hass)
    ctl = InverterGridChargeController(hass, config())
    await ctl.engage(2500)
    old = datetime.now(timezone.utc) - timedelta(hours=12)
    for entity in ("sensor.charge", "sensor.mode", "sensor.power"):
        state = hass.states.get(entity)
        state.last_changed = state.last_updated = state.last_reported = old
    await ctl.verify_engaged(2500)


async def test_helper_only_mapping_is_not_physical_confirmation(hass):
    setup_inverter(hass)
    ctl = InverterGridChargeController(hass, config(enable_feedback_entity_id=None, mode_feedback_entity_id=None, power_feedback_entity_id=None))
    await ctl.engage(2500)
    assert ctl.confirmation_level == "entity"


async def test_service_execution_has_bounded_timeout_and_rolls_back(hass):
    calls = setup_inverter(hass)

    async def hanging_service(call):
        calls.append((call.data["entity_id"], call.data["value"]))
        await asyncio.Event().wait()

    hass.services.async_register("input_number", "set_value", hanging_service)
    ctl = InverterGridChargeController(hass, config())
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(ctl.engage(2500), timeout=0.5)
    assert calls[-2:] == [("input_select.charge", "Stop"), ("input_select.mode", "Self")]


async def test_cancellation_is_propagated_and_cleanup_can_be_retried(hass):
    setup_inverter(hass)
    started = asyncio.Event()

    async def hanging_service(call):
        started.set()
        await asyncio.Event().wait()

    hass.services.async_register("input_number", "set_value", hanging_service)
    ctl = InverterGridChargeController(hass, config(timeout_seconds=1))
    task = asyncio.create_task(ctl.engage(2500))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await ctl.disengage()
    await ctl.verify_disengaged()


async def test_binary_hold_command_can_confirm_named_physical_feedback(hass):
    async def service(call):
        active = call.service == "turn_on"
        hass.states.async_set("switch.hold", "on" if active else "off")
        hass.states.async_set("sensor.hold_status", "Blocked" if active else "Allowed")

    hass.services.async_register("switch", "turn_on", service)
    hass.services.async_register("switch", "turn_off", service)
    ctl = InverterGridChargeController(hass, InverterGridChargeConfig(
        enable_entity_id="switch.hold",
        enable_engage_value="on",
        enable_disengage_value="off",
        enable_feedback_entity_id="sensor.hold_status",
        enable_feedback_engage_value="Blocked",
        enable_feedback_disengage_value="Allowed",
        timeout_seconds=0.03,
    ))
    await ctl.engage(0)
    await ctl.verify_engaged(0)
    assert hass.states.get("switch.hold").state == "on"
    await ctl.disengage()
    await ctl.verify_disengaged()
    assert hass.states.get("switch.hold").state == "off"


async def test_feedback_can_arrive_after_service_returns(hass):
    setup_inverter(hass)

    async def delayed_feedback(call):
        entity_id = call.data["entity_id"]
        value = call.data["option"]
        hass.states.async_set(entity_id, value)
        hass.loop.call_later(0.01, hass.states.async_set, entity_id.replace("input_select", "sensor"), value)

    hass.services.async_register("input_select", "select_option", delayed_feedback)
    ctl = InverterGridChargeController(hass, config(timeout_seconds=0.2))
    await ctl.engage(2500)
    assert hass.states.get("sensor.charge").state == "Forced charge"


async def test_unsupported_power_unit_rejects_before_writing(hass):
    calls = setup_inverter(hass, power_unit="A")
    ctl = InverterGridChargeController(hass, config())
    with pytest.raises(ValueError, match="unit"):
        await ctl.engage(2500)
    assert calls == []


async def test_native_switch_readback_is_physical(hass):
    cfg = InverterGridChargeConfig("switch.charge", "on", "off", timeout_seconds=0.03)
    ctl = InverterGridChargeController(hass, cfg)
    hass.states.async_set("switch.charge", "on")
    assert ctl.confirmation_level == "physical"
    await ctl.verify_engaged(2500)
