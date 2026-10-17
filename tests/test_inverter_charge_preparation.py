"""Preparing a scheduled charge must not enable it before authorization."""
import pytest
from homeassistant.core import State

from custom_components.pv_excess_control.inverter_control import InverterGridChargeController, InverterConfirmationError
from custom_components.pv_excess_control.models import InverterGridChargeConfig
from tests.test_inverter_control import _make_hass_with_async_call


async def controller():
    hass = _make_hass_with_async_call()
    config = InverterGridChargeConfig("input_select.command", "Charge", "Stop",
        mode_entity_id="input_select.mode", mode_engage_value="Forced", mode_disengage_value="Self",
        power_entity_id="input_number.power", timeout_seconds=.01)
    ctl = InverterGridChargeController(hass, config)
    await ctl.disengage()
    hass.services.async_call.reset_mock()
    return ctl, hass


async def test_prepare_confirms_mode_and_power_without_enabling_charge():
    ctl, hass = await controller()
    await ctl.prepare(1800)
    assert [call.args[2]["entity_id"] for call in hass.services.async_call.await_args_list] == [
        "input_select.mode", "input_number.power",
    ]
    assert hass.states.get("input_select.command").state == "Stop"
    await ctl.enable_prepared(1800)
    assert hass.states.get("input_select.command").state == "Charge"


async def test_power_handoff_does_not_repeat_mode_or_charge_command():
    ctl, hass = await controller()
    await ctl.engage(1800)
    hass.services.async_call.reset_mock()
    await ctl.set_power(900)
    await ctl.verify_engaged(900)
    assert len(hass.services.async_call.await_args_list) == 1
    assert hass.services.async_call.await_args.args[2] == {"entity_id": "input_number.power", "value": 900}


async def test_enabling_unprepared_mode_is_rejected_without_writing_charge():
    ctl, hass = await controller()
    with pytest.raises(InverterConfirmationError):
        await ctl.enable_prepared(1800)
    hass.services.async_call.assert_not_awaited()


async def test_automatic_preparation_rejects_existing_unowned_charge_before_writes():
    ctl, hass = await controller()
    await ctl.engage(1800)
    hass.services.async_call.reset_mock()
    with pytest.raises(InverterConfirmationError):
        await ctl.prepare(1800)
    hass.services.async_call.assert_not_awaited()


async def test_final_authorization_runs_immediately_before_enable_write():
    ctl, hass = await controller()
    await ctl.prepare(1800)
    hass.services.async_call.reset_mock()
    async def changed():
        raise RuntimeError("authorization changed")
    with pytest.raises(RuntimeError, match="authorization changed"):
        await ctl.enable_prepared(1800, before_enable=changed)
    hass.services.async_call.assert_not_awaited()


async def test_prepared_state_change_is_rejected_after_final_authorization():
    ctl, hass = await controller()
    await ctl.prepare(1800)
    hass.services.async_call.reset_mock()
    async def changed():
        await hass.services.async_call("input_select", "select_option",
            {"entity_id": "input_select.mode", "option": "Self"}, blocking=True)
    with pytest.raises(InverterConfirmationError):
        await ctl.enable_prepared(1800, before_enable=changed)
    assert [c.args[2]["entity_id"] for c in hass.services.async_call.await_args_list] == ["input_select.mode"]


async def test_enable_write_and_confirmation_share_the_declared_timeout():
    import asyncio
    ctl, hass = await controller()
    await ctl.prepare(1800)
    original = hass.services.async_call.side_effect
    async def slow(*args, **kwargs):
        await asyncio.sleep(.03)
        await original(*args, **kwargs)
    hass.services.async_call.side_effect = slow
    with pytest.raises(TimeoutError):
        await ctl.enable_prepared(1800)
    assert hass.states.get("input_select.command").state == "Stop"


async def test_enable_confirmation_uses_only_the_remaining_write_budget():
    import asyncio
    from dataclasses import replace
    ctl, hass = await controller()
    ctl._config = replace(ctl.config, timeout_seconds=.05)
    await ctl.prepare(1800)
    original = hass.services.async_call.side_effect
    async def delayed_write(*args, **kwargs):
        await asyncio.sleep(.03)
        await original(*args, **kwargs)
    async def delayed_confirmation(_):
        await asyncio.sleep(.03)
    hass.services.async_call.side_effect = delayed_write
    ctl.verify_engaged = delayed_confirmation
    with pytest.raises(InverterConfirmationError):
        await ctl.enable_prepared(1800)
