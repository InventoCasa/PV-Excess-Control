"""Tests for economic grid-charge decisions and native discharge controls."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import State
from homeassistant.util import dt as dt_util

from custom_components.pv_excess_control.models import (
    ForecastData, HourlyForecast, TariffInfo, TariffWindow,
)


def _grid_inputs(coordinator_factory, controller, **overrides):
    """Supply fresh physical observations and complete economic planning inputs."""
    coordinator = coordinator_factory(config_data={
        "auto_battery_grid_charge": True,
        "allow_grid_charging": True,
        "battery_soc": "sensor.soc",
        "battery_capacity": 10,
        "battery_grid_charge_power_w": 5000,
        "battery_max_charge_power_w": 5000,
        "battery_grid_target_soc": 80,
        "battery_target_soc": 100,
        "price_sensor": "sensor.price",
        "battery_load_profile_w": [2000.] * 24,
        "inverter_force_charge_enable_entity": "switch.charge",
        "inverter_force_charge_enable_engage_value": "on",
        "inverter_force_charge_enable_disengage_value": "off",
        "inverter_force_charge_power_entity": "number.charge_power",
        "grid_charge_engage_min_duration_minutes": 5,
        **overrides,
    }, inverter_ctl=controller)
    now = dt_util.now()
    states = {
        "sensor.soc": State("sensor.soc", "10"),
        "sensor.load_power": State("sensor.load_power", "2000", {"unit_of_measurement": "W"}),
        "sensor.pv_power": State("sensor.pv_power", "0", {"unit_of_measurement": "W"}),
        "sensor.price": State("sensor.price", ".1"),
        "sensor.forecast": State("sensor.forecast", "0"),
        "number.charge_power": State("number.charge_power", "2500", {"min": 0, "max": 5000, "step": .001, "unit_of_measurement": "W"}),
    }
    coordinator.hass.states.get.side_effect = states.get
    coordinator._forecast_entities = ["sensor.forecast"]
    coordinator._forecast_tomorrow_entities = []
    coordinator._forecast_data = ForecastData(0, [HourlyForecast(now, now + timedelta(hours=4), 0, 0)])
    tariff = TariffInfo(.1, 0, .2, .2, [
        TariffWindow(now, now + timedelta(hours=1), .1, True),
        TariffWindow(now + timedelta(hours=1), now + timedelta(hours=4), .5, False),
    ])
    if controller is not None:
        controller.confirmation_level = "physical"
        controller.verify_engaged = AsyncMock()
    return coordinator, states, tariff


@pytest.mark.asyncio
async def test_grid_charge_engages_when_forecast_plan_finds_useful_cheap_energy(
    coordinator_factory, mock_inverter_controller,
):
    coordinator, _, tariff = _grid_inputs(coordinator_factory, mock_inverter_controller)
    await coordinator._run_grid_charge_state_machine(tariff, None)
    try:
        mock_inverter_controller.engage.assert_awaited_once()
        assert mock_inverter_controller.engage.await_args.args[0] == pytest.approx(5000.0)
        assert coordinator._grid_charge_engaged
        assert coordinator._battery_grid_plan.estimated_savings > 0
        assert coordinator._battery_grid_plan.grid_target_soc < 80
    finally:
        await coordinator.async_stop_battery_controls()


@pytest.mark.asyncio
async def test_grid_charge_cheap_price_without_forecast_does_not_start(
    coordinator_factory, mock_inverter_controller,
):
    coordinator, _, tariff = _grid_inputs(coordinator_factory, mock_inverter_controller)
    coordinator._forecast_data = None
    await coordinator._run_grid_charge_state_machine(tariff, None)
    mock_inverter_controller.engage.assert_not_awaited()
    assert not coordinator._grid_charge_engaged


@pytest.mark.asyncio
async def test_grid_charge_verifies_existing_charge_without_reengaging(
    coordinator_factory, mock_inverter_controller,
):
    coordinator, _, tariff = _grid_inputs(coordinator_factory, mock_inverter_controller)
    await coordinator._run_grid_charge_state_machine(tariff, None)
    await coordinator._run_grid_charge_state_machine(tariff, None)
    mock_inverter_controller.engage.assert_awaited_once()
    mock_inverter_controller.verify_engaged.assert_awaited_once()
    await coordinator.async_stop_battery_controls()


@pytest.mark.asyncio
async def test_grid_charge_no_inverter_configured_no_calls(coordinator_factory):
    coordinator, _, tariff = _grid_inputs(coordinator_factory, None)
    await coordinator._run_grid_charge_state_machine(tariff, None)
    assert not coordinator._grid_charge_engaged


@pytest.mark.asyncio
async def test_grid_charge_price_rise_stops_without_minimum_run_delay(
    coordinator_factory, mock_inverter_controller,
):
    coordinator, states, tariff = _grid_inputs(coordinator_factory, mock_inverter_controller)
    await coordinator._run_grid_charge_state_machine(tariff, None)
    assert coordinator._grid_charge_engaged
    tariff.current_price = .3
    tariff.windows[0] = TariffWindow(tariff.windows[0].start, tariff.windows[0].end, .3, False)
    states["sensor.price"] = State("sensor.price", ".3")
    await coordinator._run_grid_charge_state_machine(tariff, None)
    mock_inverter_controller.disengage.assert_awaited_once()
    assert not coordinator._grid_charge_engaged


@pytest.mark.asyncio
async def test_grid_charge_stops_at_the_planned_soc_before_the_pv_target(
    coordinator_factory, mock_inverter_controller,
):
    coordinator, states, tariff = _grid_inputs(coordinator_factory, mock_inverter_controller)
    await coordinator._run_grid_charge_state_machine(tariff, None)
    target = coordinator._current_battery_slot().soc_end
    assert target < coordinator.config_entry.data["battery_target_soc"]
    states["sensor.soc"] = State("sensor.soc", str(target))
    await coordinator._run_grid_charge_state_machine(tariff, None)
    mock_inverter_controller.disengage.assert_awaited_once()
    assert not coordinator._grid_charge_engaged


@pytest.mark.asyncio
async def test_grid_charge_missing_soc_stops_without_minimum_run_delay(
    coordinator_factory, mock_inverter_controller,
):
    coordinator, states, tariff = _grid_inputs(coordinator_factory, mock_inverter_controller)
    await coordinator._run_grid_charge_state_machine(tariff, None)
    assert coordinator._grid_charge_engaged
    states["sensor.soc"] = State("sensor.soc", "unavailable")
    await coordinator._run_grid_charge_state_machine(tariff, None)
    mock_inverter_controller.disengage.assert_awaited_once()
    assert not coordinator._grid_charge_engaged


@pytest.mark.asyncio
async def test_grid_charge_manual_request_bypasses_price_gate_with_fresh_soc(
    coordinator_factory, mock_inverter_controller,
):
    coordinator, _, tariff = _grid_inputs(coordinator_factory, mock_inverter_controller, auto_battery_grid_charge=False)
    coordinator.force_charge = True
    tariff.current_price = .5
    coordinator._forecast_data = None
    await coordinator._run_grid_charge_state_machine(tariff, None)
    mock_inverter_controller.engage.assert_awaited_once_with(5000.0)
    await coordinator.async_stop_battery_controls()


@pytest.mark.asyncio
async def test_grid_charge_manual_request_off_stops_without_minimum_run_delay(
    coordinator_factory, mock_inverter_controller,
):
    coordinator, _, tariff = _grid_inputs(coordinator_factory, mock_inverter_controller, auto_battery_grid_charge=False)
    coordinator.force_charge = True
    await coordinator._run_grid_charge_state_machine(tariff, None)
    assert coordinator._grid_charge_engaged
    coordinator.force_charge = False
    await coordinator._run_grid_charge_state_machine(tariff, None)
    mock_inverter_controller.disengage.assert_awaited_once()
    assert not coordinator._grid_charge_engaged


@pytest.mark.asyncio
async def test_grid_charge_persisted_ownership_is_released_before_replanning(
    coordinator_factory, mock_inverter_controller,
):
    coordinator, _, tariff = _grid_inputs(coordinator_factory, mock_inverter_controller, _grid_charge_engaged=True)
    await coordinator._run_grid_charge_state_machine(tariff, None)
    mock_inverter_controller.disengage.assert_awaited_once()
    mock_inverter_controller.engage.assert_not_awaited()
    assert not coordinator._grid_charge_engaged


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["balanced", "battery_first", "appliance_first"])
async def test_grid_charge_independent_of_battery_strategy(
    strategy, coordinator_factory, mock_inverter_controller,
):
    coordinator, _, tariff = _grid_inputs(coordinator_factory, mock_inverter_controller, battery_strategy=strategy)
    await coordinator._run_grid_charge_state_machine(tariff, None)
    mock_inverter_controller.engage.assert_awaited_once()
    await coordinator.async_stop_battery_controls()


# ---------------------------------------------------------------------------
# _apply_battery_discharge_limit clamping
# ---------------------------------------------------------------------------


def _stub_state(min_value=None, max_value=None):
    from unittest.mock import MagicMock as _MM
    s = _MM()
    attrs = {}
    if min_value is not None:
        attrs["min"] = min_value
    if max_value is not None:
        attrs["max"] = max_value
    s.attributes = attrs
    return s


@pytest.mark.asyncio
async def test_discharge_limit_clamped_up_to_entity_min_when_below(coordinator_factory):
    """Optimizer requests 0W block; entity's min is 100W → must write 100W, not 0W.

    Reproduces the prod failure where input_number.set_sg_battery_max_discharge_power
    rejected 0.0 (range 100.0 - 5000.0) and the inverter kept discharging.
    """
    from custom_components.pv_excess_control.const import (
        CONF_BATTERY_MAX_DISCHARGE_ENTITY,
        CONF_BATTERY_MAX_DISCHARGE_DEFAULT,
    )
    from custom_components.pv_excess_control.models import BatteryDischargeAction

    coord = coordinator_factory(
        config_data={
            CONF_BATTERY_MAX_DISCHARGE_ENTITY: "input_number.set_sg_battery_max_discharge_power",
            CONF_BATTERY_MAX_DISCHARGE_DEFAULT: 5000.0,
        },
    )
    coord.hass.states.get = lambda eid: _stub_state(min_value=100.0, max_value=5000.0)

    await coord._apply_battery_discharge_limit(
        BatteryDischargeAction(should_limit=True, max_discharge_watts=0.0)
    )

    coord.hass.services.async_call.assert_awaited_once()
    args, kwargs = coord.hass.services.async_call.call_args
    payload = args[2] if len(args) > 2 else kwargs.get("service_data") or kwargs
    assert payload["value"] == 100.0
    assert coord._last_discharge_limit == 100.0


@pytest.mark.asyncio
async def test_discharge_limit_passes_through_when_within_entity_range(coordinator_factory):
    """A value inside [min, max] must not be modified."""
    from custom_components.pv_excess_control.const import (
        CONF_BATTERY_MAX_DISCHARGE_ENTITY,
        CONF_BATTERY_MAX_DISCHARGE_DEFAULT,
    )
    from custom_components.pv_excess_control.models import BatteryDischargeAction

    coord = coordinator_factory(
        config_data={
            CONF_BATTERY_MAX_DISCHARGE_ENTITY: "input_number.set_sg_battery_max_discharge_power",
            CONF_BATTERY_MAX_DISCHARGE_DEFAULT: 5000.0,
        },
    )
    coord.hass.states.get = lambda eid: _stub_state(min_value=100.0, max_value=5000.0)

    await coord._apply_battery_discharge_limit(
        BatteryDischargeAction(should_limit=True, max_discharge_watts=2500.0)
    )

    args, kwargs = coord.hass.services.async_call.call_args
    payload = args[2] if len(args) > 2 else kwargs.get("service_data") or kwargs
    assert payload["value"] == 2500.0


@pytest.mark.asyncio
async def test_discharge_limit_clamped_down_to_entity_max_when_above(coordinator_factory):
    """A value above the entity's max must be clamped down."""
    from custom_components.pv_excess_control.const import (
        CONF_BATTERY_MAX_DISCHARGE_ENTITY,
        CONF_BATTERY_MAX_DISCHARGE_DEFAULT,
    )
    from custom_components.pv_excess_control.models import BatteryDischargeAction

    coord = coordinator_factory(
        config_data={
            CONF_BATTERY_MAX_DISCHARGE_ENTITY: "input_number.set_sg_battery_max_discharge_power",
            CONF_BATTERY_MAX_DISCHARGE_DEFAULT: 5000.0,
        },
    )
    coord.hass.states.get = lambda eid: _stub_state(min_value=100.0, max_value=5000.0)

    await coord._apply_battery_discharge_limit(
        BatteryDischargeAction(should_limit=True, max_discharge_watts=9999.0)
    )

    args, kwargs = coord.hass.services.async_call.call_args
    payload = args[2] if len(args) > 2 else kwargs.get("service_data") or kwargs
    assert payload["value"] == 5000.0


@pytest.mark.asyncio
async def test_discharge_limit_no_clamp_when_state_unknown(coordinator_factory):
    """If the entity state is unavailable, write the requested value as-is (HA will reject if invalid)."""
    from custom_components.pv_excess_control.const import (
        CONF_BATTERY_MAX_DISCHARGE_ENTITY,
        CONF_BATTERY_MAX_DISCHARGE_DEFAULT,
    )
    from custom_components.pv_excess_control.models import BatteryDischargeAction

    coord = coordinator_factory(
        config_data={
            CONF_BATTERY_MAX_DISCHARGE_ENTITY: "input_number.set_sg_battery_max_discharge_power",
            CONF_BATTERY_MAX_DISCHARGE_DEFAULT: 5000.0,
        },
    )
    coord.hass.states.get = lambda eid: None

    await coord._apply_battery_discharge_limit(
        BatteryDischargeAction(should_limit=True, max_discharge_watts=0.0)
    )

    args, kwargs = coord.hass.services.async_call.call_args
    payload = args[2] if len(args) > 2 else kwargs.get("service_data") or kwargs
    assert payload["value"] == 0.0
