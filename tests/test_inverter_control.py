"""Tests for InverterGridChargeController."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, call

from homeassistant.core import State

from custom_components.pv_excess_control.inverter_control import (
    InverterGridChargeController,
)
from custom_components.pv_excess_control.models import InverterGridChargeConfig


def _make_hass_with_async_call() -> MagicMock:
    hass = MagicMock()
    hass.services = MagicMock()
    states = {}

    def get_state(entity_id):
        return states.get(entity_id, State(entity_id, "0", {
            "unit_of_measurement": "W", "min": 0, "max": 10000,
        }))

    async def apply_service(domain, service, data, *, blocking):
        entity_id = data["entity_id"]
        value = data.get("option", data.get("value"))
        if service in {"turn_on", "turn_off"}:
            value = "on" if service == "turn_on" else "off"
        states[entity_id] = State(entity_id, str(value), get_state(entity_id).attributes)

    hass.states.get.side_effect = get_state
    hass.services.async_call = AsyncMock(side_effect=apply_service)
    return hass


@pytest.mark.asyncio
async def test_engage_full_triple_writes_mode_then_power_then_command():
    hass = _make_hass_with_async_call()
    cfg = InverterGridChargeConfig(
        enable_entity_id="input_select.set_sg_battery_forced_charge_discharge_cmd",
        enable_engage_value="Forced charge",
        enable_disengage_value="Stop (default)",
        mode_entity_id="input_select.set_sg_ems_mode",
        mode_engage_value="Forced mode",
        mode_disengage_value="Self-consumption mode (default)",
        power_entity_id="input_number.set_sg_forced_charge_discharge_power",
    )
    ctl = InverterGridChargeController(hass, cfg)

    await ctl.engage(power_w=5000.0)

    assert hass.services.async_call.await_count == 3
    assert hass.services.async_call.await_args_list == [
        call("input_select", "select_option",
             {"entity_id": "input_select.set_sg_ems_mode", "option": "Forced mode"},
             blocking=True),
        call("input_number", "set_value",
             {"entity_id": "input_number.set_sg_forced_charge_discharge_power", "value": 5000.0},
             blocking=True),
        call("input_select", "select_option",
             {"entity_id": "input_select.set_sg_battery_forced_charge_discharge_cmd", "option": "Forced charge"},
             blocking=True),
    ]


@pytest.mark.asyncio
async def test_engage_skips_mode_when_not_configured():
    hass = _make_hass_with_async_call()
    cfg = InverterGridChargeConfig(
        enable_entity_id="switch.battery_force_charge",
        enable_engage_value="on",
        enable_disengage_value="off",
    )
    ctl = InverterGridChargeController(hass, cfg)

    await ctl.engage(power_w=5000.0)

    assert hass.services.async_call.await_count == 1
    assert hass.services.async_call.await_args_list[0] == call(
        "switch", "turn_on", {"entity_id": "switch.battery_force_charge"}, blocking=True,
    )


@pytest.mark.asyncio
async def test_engage_skips_power_when_not_configured():
    hass = _make_hass_with_async_call()
    cfg = InverterGridChargeConfig(
        enable_entity_id="input_select.cmd",
        enable_engage_value="Forced charge",
        enable_disengage_value="Stop",
        mode_entity_id="input_select.mode",
        mode_engage_value="Forced",
        mode_disengage_value="Self",
    )
    ctl = InverterGridChargeController(hass, cfg)

    await ctl.engage(power_w=5000.0)

    assert hass.services.async_call.await_count == 2
    # mode then enable; no power call
    assert hass.services.async_call.await_args_list[0].args[:2] == ("input_select", "select_option")
    assert hass.services.async_call.await_args_list[1].args[:2] == ("input_select", "select_option")


@pytest.mark.asyncio
async def test_disengage_writes_command_then_mode_in_reverse():
    hass = _make_hass_with_async_call()
    cfg = InverterGridChargeConfig(
        enable_entity_id="input_select.cmd", enable_engage_value="Forced charge", enable_disengage_value="Stop",
        mode_entity_id="input_select.mode", mode_engage_value="Forced", mode_disengage_value="Self",
        power_entity_id="input_number.power",
    )
    ctl = InverterGridChargeController(hass, cfg)

    await ctl.disengage()

    assert hass.services.async_call.await_count == 2
    assert hass.services.async_call.await_args_list == [
        call("input_select", "select_option",
             {"entity_id": "input_select.cmd", "option": "Stop"}, blocking=True),
        call("input_select", "select_option",
             {"entity_id": "input_select.mode", "option": "Self"}, blocking=True),
    ]


@pytest.mark.asyncio
async def test_disengage_does_not_touch_power_entity():
    hass = _make_hass_with_async_call()
    cfg = InverterGridChargeConfig(
        enable_entity_id="input_select.cmd", enable_engage_value="Forced charge", enable_disengage_value="Stop",
        power_entity_id="input_number.power",
    )
    ctl = InverterGridChargeController(hass, cfg)

    await ctl.disengage()

    # No call should reference power entity
    for c in hass.services.async_call.await_args_list:
        assert "input_number" not in c.args


@pytest.mark.parametrize("domain", ["input_select", "select"])
@pytest.mark.asyncio
async def test_service_derivation_select_like(domain):
    hass = _make_hass_with_async_call()
    cfg = InverterGridChargeConfig(
        enable_entity_id=f"{domain}.cmd",
        enable_engage_value="Forced charge",
        enable_disengage_value="Stop",
    )
    ctl = InverterGridChargeController(hass, cfg)

    await ctl.engage(power_w=0.0)
    last = hass.services.async_call.await_args
    assert last.args[:2] == (domain, "select_option")
    assert last.args[2] == {"entity_id": f"{domain}.cmd", "option": "Forced charge"}
    assert last.kwargs == {"blocking": True}


@pytest.mark.asyncio
async def test_service_derivation_switch_engage_value_on():
    hass = _make_hass_with_async_call()
    cfg = InverterGridChargeConfig(
        enable_entity_id="switch.batt_force",
        enable_engage_value="on",
        enable_disengage_value="off",
    )
    ctl = InverterGridChargeController(hass, cfg)

    await ctl.engage(power_w=0.0)
    assert hass.services.async_call.await_args.args[:2] == ("switch", "turn_on")


@pytest.mark.asyncio
async def test_service_derivation_switch_engage_value_off_falls_back_to_turn_off():
    hass = _make_hass_with_async_call()
    cfg = InverterGridChargeConfig(
        enable_entity_id="switch.batt_force",
        enable_engage_value="off",  # not truthy
        enable_disengage_value="on",
    )
    ctl = InverterGridChargeController(hass, cfg)

    await ctl.engage(power_w=0.0)
    assert hass.services.async_call.await_args.args[:2] == ("switch", "turn_off")


@pytest.mark.asyncio
async def test_service_derivation_input_boolean():
    hass = _make_hass_with_async_call()
    cfg = InverterGridChargeConfig(
        enable_entity_id="input_boolean.batt_force",
        enable_engage_value="true",
        enable_disengage_value="false",
    )
    ctl = InverterGridChargeController(hass, cfg)

    await ctl.engage(power_w=0.0)
    assert hass.services.async_call.await_args.args[:2] == ("input_boolean", "turn_on")


@pytest.mark.parametrize("domain", ["input_number", "number"])
@pytest.mark.asyncio
async def test_service_derivation_power_writes_set_value_with_float(domain):
    hass = _make_hass_with_async_call()
    cfg = InverterGridChargeConfig(
        enable_entity_id="input_select.cmd",
        enable_engage_value="Forced charge",
        enable_disengage_value="Stop",
        power_entity_id=f"{domain}.power",
    )
    ctl = InverterGridChargeController(hass, cfg)

    await ctl.engage(power_w=4321.0)

    power_call = next(
        c for c in hass.services.async_call.await_args_list
        if c.args[0] == domain
    )
    assert power_call.args[:2] == (domain, "set_value")
    assert power_call.args[2] == {"entity_id": f"{domain}.power", "value": 4321.0}
    assert power_call.kwargs == {"blocking": True}


def test_unsupported_enable_domain_raises_value_error_at_construction():
    hass = _make_hass_with_async_call()
    cfg = InverterGridChargeConfig(
        enable_entity_id="light.kitchen",  # unsupported
        enable_engage_value="on",
        enable_disengage_value="off",
    )
    with pytest.raises(ValueError, match="unsupported domain"):
        InverterGridChargeController(hass, cfg)


def test_unsupported_power_domain_raises_value_error_at_construction():
    hass = _make_hass_with_async_call()
    cfg = InverterGridChargeConfig(
        enable_entity_id="switch.batt", enable_engage_value="on", enable_disengage_value="off",
        power_entity_id="sensor.kitchen_temp",  # unsupported
    )
    with pytest.raises(ValueError, match="unsupported domain"):
        InverterGridChargeController(hass, cfg)
