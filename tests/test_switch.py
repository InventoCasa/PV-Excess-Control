"""Tests for ForceChargeSwitch snappy engage/disengage behaviour."""
from __future__ import annotations

import pytest
from homeassistant.core import State

from custom_components.pv_excess_control.const import (
    CONF_AUTO_BATTERY_GRID_CHARGE,
    CONF_BATTERY_GRID_CHARGE_POWER_W,
    CONF_BATTERY_TARGET_SOC,
    CONF_BATTERY_SOC,
    CONF_INVERTER_FORCE_CHARGE_ENABLE_ENTITY,
    CONF_INVERTER_FORCE_CHARGE_ENABLE_ENGAGE_VALUE,
    CONF_INVERTER_FORCE_CHARGE_ENABLE_DISENGAGE_VALUE,
)


def _fresh_soc(coordinator, value=70):
    coordinator.config_entry.data[CONF_BATTERY_SOC] = "sensor.soc"
    state = State("sensor.soc", str(value))
    coordinator.hass.states.get.side_effect = lambda entity_id: state if entity_id == "sensor.soc" else None


@pytest.mark.asyncio
async def test_force_charge_switch_on_calls_engage_immediately(
    coordinator_factory, mock_inverter_controller, mock_tariff_at, mock_power_state_with_soc,
):
    """Flipping the switch ON triggers engage immediately, not on the next coordinator cycle."""
    from custom_components.pv_excess_control.switch import ForceChargeSwitch

    coordinator = coordinator_factory(
        config_data={
            CONF_BATTERY_GRID_CHARGE_POWER_W: 5000.0,
            CONF_INVERTER_FORCE_CHARGE_ENABLE_ENTITY: "input_select.cmd",
            CONF_INVERTER_FORCE_CHARGE_ENABLE_ENGAGE_VALUE: "Forced charge",
            CONF_INVERTER_FORCE_CHARGE_ENABLE_DISENGAGE_VALUE: "Stop",
        },
        inverter_ctl=mock_inverter_controller,
    )
    _fresh_soc(coordinator)
    sw = ForceChargeSwitch(coordinator)
    sw.hass = coordinator.hass
    sw.async_write_ha_state = lambda: None  # silence the entity registry write

    await sw.async_turn_on()

    mock_inverter_controller.engage.assert_awaited_once_with(5000.0)
    assert coordinator._grid_charge_engaged is True
    assert coordinator.force_charge is True
    await sw.async_turn_off()


@pytest.mark.asyncio
async def test_force_charge_switch_off_calls_disengage_immediately_when_not_auto_should_engage(
    coordinator_factory, mock_inverter_controller, mock_tariff_at, mock_power_state_with_soc,
):
    """When auto_should_engage_now is False, flipping switch OFF disengages immediately."""
    from custom_components.pv_excess_control.switch import ForceChargeSwitch

    coordinator = coordinator_factory(
        config_data={
            CONF_AUTO_BATTERY_GRID_CHARGE: False,  # auto OFF
            CONF_BATTERY_GRID_CHARGE_POWER_W: 5000.0,
            CONF_INVERTER_FORCE_CHARGE_ENABLE_ENTITY: "input_select.cmd",
            CONF_INVERTER_FORCE_CHARGE_ENABLE_ENGAGE_VALUE: "Forced charge",
            CONF_INVERTER_FORCE_CHARGE_ENABLE_DISENGAGE_VALUE: "Stop",
        },
        inverter_ctl=mock_inverter_controller,
    )
    _fresh_soc(coordinator)
    coordinator._latest_tariff = mock_tariff_at(0.50, 0.02)  # not cheap
    coordinator._latest_power_state = mock_power_state_with_soc(70.0)
    sw = ForceChargeSwitch(coordinator)
    sw.hass = coordinator.hass
    sw.async_write_ha_state = lambda: None

    await sw.async_turn_on()
    assert coordinator._grid_charge_engaged is True

    await sw.async_turn_off()

    mock_inverter_controller.disengage.assert_awaited_once()
    assert coordinator._grid_charge_engaged is False
    assert coordinator.force_charge is False


@pytest.mark.asyncio
async def test_force_charge_switch_off_always_stops_even_when_automatic_charge_is_enabled(
    coordinator_factory, mock_inverter_controller, mock_tariff_at, mock_power_state_with_soc,
):
    """Explicit OFF must release an owned charge even during a cheap tariff."""
    from custom_components.pv_excess_control.switch import ForceChargeSwitch

    coordinator = coordinator_factory(
        config_data={
            CONF_AUTO_BATTERY_GRID_CHARGE: True,
            CONF_BATTERY_GRID_CHARGE_POWER_W: 5000.0,
            CONF_BATTERY_TARGET_SOC: 100,
            CONF_INVERTER_FORCE_CHARGE_ENABLE_ENTITY: "input_select.cmd",
            CONF_INVERTER_FORCE_CHARGE_ENABLE_ENGAGE_VALUE: "Forced charge",
            CONF_INVERTER_FORCE_CHARGE_ENABLE_DISENGAGE_VALUE: "Stop",
        },
        inverter_ctl=mock_inverter_controller,
    )
    _fresh_soc(coordinator)
    coordinator._latest_tariff = mock_tariff_at(0.01, 0.02)  # cheap
    coordinator._latest_power_state = mock_power_state_with_soc(70.0)
    sw = ForceChargeSwitch(coordinator)
    sw.hass = coordinator.hass
    sw.async_write_ha_state = lambda: None

    await sw.async_turn_on()
    assert coordinator._grid_charge_engaged
    await sw.async_turn_off()

    mock_inverter_controller.disengage.assert_awaited_once()
    assert coordinator._grid_charge_engaged is False


@pytest.mark.asyncio
async def test_force_charge_switch_no_inverter_configured_no_engage_call(coordinator_factory):
    """No inverter wired — switch still works (sheds appliances), no engage/disengage calls."""
    from custom_components.pv_excess_control.switch import ForceChargeSwitch

    coordinator = coordinator_factory(config_data={}, inverter_ctl=None)
    sw = ForceChargeSwitch(coordinator)
    sw.hass = coordinator.hass
    sw.async_write_ha_state = lambda: None

    # Must not raise
    await sw.async_turn_on()
    await sw.async_turn_off()

    assert coordinator.force_charge is False  # final state
