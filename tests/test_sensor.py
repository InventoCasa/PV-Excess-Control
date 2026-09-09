"""Tests for PV Excess Control sensor entities.

Uses standard unittest.mock (no pytest-homeassistant-custom-component).
Validates entity metadata, value reading, and graceful handling of missing data.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.pv_excess_control.const import (
    CONF_APPLIANCE_NAME,
    DOMAIN,
)
from custom_components.pv_excess_control.models import (
    Action,
    ApplianceState,
    ControlDecision,
    Plan,
    PowerState,
    BatteryDischargeAction,
    BatteryTarget,
    BatteryStrategy,
)
from custom_components.pv_excess_control.sensor import (
    PvApplianceEnergySensor,
    PvAppliancePowerSensor,
    PvApplianceRuntimeSensor,
    PvApplianceStatusSensor,
    PvExcessPowerSensor,
    PvPlanConfidenceSensor,
    async_setup_entry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_power_state(excess_power: float = 500.0) -> PowerState:
    return PowerState(
        pv_production=3000.0,
        grid_export=1000.0,
        grid_import=0.0,
        load_power=2000.0,
        excess_power=excess_power,
        battery_soc=80.0,
        battery_power=None,
        ev_soc=None,
        timestamp=datetime.now(tz=timezone.utc),
    )


def _make_appliance_state(
    appliance_id: str = "sub_1",
    current_power: float = 1500.0,
    runtime_hours: float = 1.5,
    energy_today: float = 2.25,
) -> ApplianceState:
    return ApplianceState(
        appliance_id=appliance_id,
        is_on=True,
        current_power=current_power,
        current_amperage=None,
        runtime_today=timedelta(hours=runtime_hours),
        energy_today=energy_today,
        last_state_change=None,
        ev_connected=None,
    )


def _make_plan(confidence: float = 0.85) -> Plan:
    return Plan(
        created_at=datetime.now(tz=timezone.utc),
        horizon=timedelta(hours=24),
        entries=[],
        battery_target=BatteryTarget(
            target_soc=100.0,
            target_time=datetime.now(tz=timezone.utc) + timedelta(hours=8),
            strategy=BatteryStrategy.BALANCED,
        ),
        confidence=confidence,
    )


def _make_control_decision(appliance_id: str = "sub_1", reason: str = "excess available") -> ControlDecision:
    return ControlDecision(
        appliance_id=appliance_id,
        action=Action.ON,
        target_current=None,
        reason=reason,
        overrides_plan=False,
    )


def _make_coordinator_data(
    power_state: PowerState | None = None,
    appliance_states: dict | None = None,
    control_decisions: list | None = None,
    current_plan: Plan | None = None,
) -> dict[str, Any]:
    return {
        "power_state": power_state,
        "power_history": [power_state] if power_state else [],
        "current_plan": current_plan,
        "control_decisions": control_decisions or [],
        "battery_discharge_action": BatteryDischargeAction(should_limit=False),
        "appliance_states": appliance_states or {},
        "enabled": True,
    }


def _make_coordinator(data: dict[str, Any] | None = None) -> MagicMock:
    """Create a minimal mock coordinator."""
    coord = MagicMock()
    coord.data = data or _make_coordinator_data()
    coord.config_entry = MagicMock()
    coord.config_entry.entry_id = "test_entry_123"
    return coord


def _make_subentry(name: str) -> MagicMock:
    subentry = MagicMock()
    subentry.data = {CONF_APPLIANCE_NAME: name}
    return subentry


# ---------------------------------------------------------------------------
# Tests: async_setup_entry
# ---------------------------------------------------------------------------


class TestSensorSetup:
    """Tests for async_setup_entry."""

    @pytest.mark.asyncio
    async def test_setup_creates_system_sensors(self):
        """async_setup_entry creates system-level sensors (no subentries)."""
        coordinator = _make_coordinator()

        hass = MagicMock()
        hass.data = {DOMAIN: {"test_entry_123": coordinator}}

        config_entry = MagicMock()
        config_entry.entry_id = "test_entry_123"
        config_entry.subentries = {}

        added_entities = []

        def capture(entities, *args, **kwargs):
            added_entities.extend(entities)

        await async_setup_entry(hass, config_entry, capture)

        # System sensors: status, excess_power, plan_confidence
        assert len(added_entities) == 3
        names = [e._attr_name for e in added_entities]
        assert "Status" in names
        assert "Excess Power" in names
        assert "Plan Confidence" in names

    @pytest.mark.asyncio
    async def test_setup_creates_appliance_sensors(self):
        """Creates 5 per-appliance sensors for each subentry."""
        coordinator = _make_coordinator()

        hass = MagicMock()
        hass.data = {DOMAIN: {"test_entry_123": coordinator}}

        config_entry = MagicMock()
        config_entry.entry_id = "test_entry_123"
        config_entry.subentries = {
            "sub_1": _make_subentry("Heat Pump"),
            "sub_2": _make_subentry("Washing Machine"),
        }

        added_entities = []

        def capture(entities, *args, **kwargs):
            added_entities.extend(entities)

        await async_setup_entry(hass, config_entry, capture)

        # 3 system + 5 per appliance * 2 appliances = 13
        assert len(added_entities) == 13

    @pytest.mark.asyncio
    async def test_appliance_sensor_names_use_appliance_name(self):
        """Per-appliance sensor names include the appliance name."""
        coordinator = _make_coordinator()

        hass = MagicMock()
        hass.data = {DOMAIN: {"test_entry_123": coordinator}}

        config_entry = MagicMock()
        config_entry.entry_id = "test_entry_123"
        config_entry.subentries = {"sub_1": _make_subentry("Solar Charger")}

        added_entities = []

        def capture(entities, *args, **kwargs):
            added_entities.extend(entities)

        await async_setup_entry(hass, config_entry, capture)

        appliances = [e for e in added_entities if getattr(e, "_appliance_id", None) == "sub_1"]
        assert len(appliances) == 5
        assert {e.name for e in appliances} == {"Power", "Runtime Today", "Energy Today", "Activations Today", "Status"}
        assert all(e.device_info["name"] == "Solar Charger" for e in appliances)


# ---------------------------------------------------------------------------
# Tests: system sensors
# ---------------------------------------------------------------------------


class TestSystemSensors:
    """Tests for system-level sensor entities."""

    def test_excess_power_sensor_value(self):
        """Excess power sensor reads from coordinator power_state."""
        ps = _make_power_state(excess_power=1234.5)
        data = _make_coordinator_data(power_state=ps)
        coord = _make_coordinator(data=data)

        sensor = PvExcessPowerSensor(coord)
        assert sensor.native_value == 1234.5

    def test_excess_power_sensor_attributes(self):
        """Excess power sensor has correct device_class, state_class, unit."""
        from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
        from homeassistant.const import UnitOfPower

        coord = _make_coordinator()
        sensor = PvExcessPowerSensor(coord)

        assert sensor._attr_device_class == SensorDeviceClass.POWER
        assert sensor._attr_state_class == SensorStateClass.MEASUREMENT
        assert sensor._attr_native_unit_of_measurement == UnitOfPower.WATT

    def test_excess_power_sensor_handles_none_power_state(self):
        """Excess power sensor returns None when power_state is None."""
        data = _make_coordinator_data(power_state=None)
        coord = _make_coordinator(data=data)

        sensor = PvExcessPowerSensor(coord)
        assert sensor.native_value is None

    def test_excess_power_sensor_handles_none_coordinator_data(self):
        """Excess power sensor returns None when coordinator data is None."""
        coord = _make_coordinator(data=None)
        sensor = PvExcessPowerSensor(coord)
        assert sensor.native_value is None

    def test_plan_confidence_sensor_value(self):
        """Plan confidence sensor converts 0-1 float to percentage."""
        plan = _make_plan(confidence=0.75)
        data = _make_coordinator_data(current_plan=plan)
        coord = _make_coordinator(data=data)

        sensor = PvPlanConfidenceSensor(coord)
        assert sensor.native_value == 75.0

    def test_plan_confidence_sensor_no_plan(self):
        """Plan confidence sensor returns None when no plan exists."""
        data = _make_coordinator_data(current_plan=None)
        coord = _make_coordinator(data=data)

        sensor = PvPlanConfidenceSensor(coord)
        assert sensor.native_value is None

    def test_plan_confidence_sensor_attributes(self):
        """Plan confidence sensor has MEASUREMENT state_class and % unit."""
        from homeassistant.components.sensor import SensorStateClass
        from homeassistant.const import PERCENTAGE

        coord = _make_coordinator()
        sensor = PvPlanConfidenceSensor(coord)

        assert sensor._attr_state_class == SensorStateClass.MEASUREMENT
        assert sensor._attr_native_unit_of_measurement == PERCENTAGE

    def test_system_sensor_unique_id(self):
        """System sensor unique_id combines entry_id and suffix."""
        coord = _make_coordinator()
        sensor = PvExcessPowerSensor(coord)

        assert sensor._attr_unique_id == "test_entry_123_excess_power"

    def test_sensor_has_entity_name_true(self):
        """All sensors have has_entity_name=True."""
        coord = _make_coordinator()
        for sensor_cls in [PvExcessPowerSensor, PvPlanConfidenceSensor]:
            sensor = sensor_cls(coord)
            assert sensor._attr_has_entity_name is True

    def test_native_value_is_none_when_excess_power_is_none(self):
        """When PowerState.excess_power is None, native_value must return None
        so HA translates the entity state to STATE_UNKNOWN."""
        coord = _make_coordinator()
        coord.data = {
            "power_state": PowerState(
                pv_production=None,
                grid_export=None,
                grid_import=None,
                load_power=None,
                excess_power=None,
                battery_soc=None,
                battery_power=None,
                ev_soc=None,
                timestamp=datetime.now(),
            ),
            "power_history": [],
        }
        sensor = PvExcessPowerSensor(coord)
        assert sensor.native_value is None


# ---------------------------------------------------------------------------
# Tests: appliance sensors
# ---------------------------------------------------------------------------


class TestApplianceSensors:
    """Tests for per-appliance sensor entities."""

    def test_appliance_power_sensor_value(self):
        """Per-appliance power sensor reads from correct appliance state."""
        app_state = _make_appliance_state(appliance_id="sub_1", current_power=2200.0)
        data = _make_coordinator_data(appliance_states={"sub_1": app_state})
        coord = _make_coordinator(data=data)

        sensor = PvAppliancePowerSensor(coord, "sub_1", "Heat Pump")
        assert sensor.native_value == 2200.0

    def test_appliance_power_sensor_wrong_id_returns_none(self):
        """Power sensor for unknown appliance ID returns None."""
        data = _make_coordinator_data(appliance_states={})
        coord = _make_coordinator(data=data)

        sensor = PvAppliancePowerSensor(coord, "unknown_id", "Unknown")
        assert sensor.native_value is None

    def test_appliance_power_sensor_attributes(self):
        """Appliance power sensor has POWER device_class and MEASUREMENT state_class."""
        from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
        from homeassistant.const import UnitOfPower

        coord = _make_coordinator()
        sensor = PvAppliancePowerSensor(coord, "sub_1", "Heat Pump")

        assert sensor._attr_device_class == SensorDeviceClass.POWER
        assert sensor._attr_state_class == SensorStateClass.MEASUREMENT
        assert sensor._attr_native_unit_of_measurement == UnitOfPower.WATT

    def test_appliance_runtime_sensor_value(self):
        """Runtime sensor converts timedelta to hours."""
        app_state = _make_appliance_state(appliance_id="sub_1", runtime_hours=2.5)
        data = _make_coordinator_data(appliance_states={"sub_1": app_state})
        coord = _make_coordinator(data=data)

        sensor = PvApplianceRuntimeSensor(coord, "sub_1", "Heat Pump")
        assert sensor.native_value == pytest.approx(2.5, rel=1e-3)

    def test_appliance_runtime_sensor_is_measurement(self):
        """Runtime sensor has MEASUREMENT state_class (resets at midnight)."""
        from homeassistant.components.sensor import SensorStateClass

        coord = _make_coordinator()
        sensor = PvApplianceRuntimeSensor(coord, "sub_1", "Heat Pump")
        assert sensor._attr_state_class == SensorStateClass.MEASUREMENT

    def test_appliance_energy_sensor_value(self):
        """Energy sensor reads energy_today from appliance state."""
        app_state = _make_appliance_state(appliance_id="sub_1", energy_today=3.75)
        data = _make_coordinator_data(appliance_states={"sub_1": app_state})
        coord = _make_coordinator(data=data)

        sensor = PvApplianceEnergySensor(coord, "sub_1", "Heat Pump")
        assert sensor.native_value == 3.75

    def test_appliance_energy_sensor_attributes(self):
        """Energy sensor has ENERGY device_class and MEASUREMENT state_class (resets at midnight)."""
        from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
        from homeassistant.const import UnitOfEnergy

        coord = _make_coordinator()
        sensor = PvApplianceEnergySensor(coord, "sub_1", "Heat Pump")

        assert sensor._attr_device_class == SensorDeviceClass.ENERGY
        assert sensor._attr_state_class == SensorStateClass.TOTAL
        assert sensor._attr_native_unit_of_measurement == UnitOfEnergy.KILO_WATT_HOUR

    def test_appliance_status_sensor_value(self):
        """Status sensor shows decision reason for the appliance."""
        decision = _make_control_decision(appliance_id="sub_1", reason="excess available")
        data = _make_coordinator_data(control_decisions=[decision])
        coord = _make_coordinator(data=data)

        sensor = PvApplianceStatusSensor(coord, "sub_1", "Heat Pump")
        assert sensor.native_value == "excess available"

    def test_appliance_status_sensor_no_decision(self):
        """Status sensor returns None when no decision for this appliance."""
        data = _make_coordinator_data(control_decisions=[])
        coord = _make_coordinator(data=data)

        sensor = PvApplianceStatusSensor(coord, "sub_1", "Heat Pump")
        assert sensor.native_value is None

    def test_appliance_status_sensor_filters_by_id(self):
        """Status sensor only reads decisions for its own appliance ID."""
        decision_a = _make_control_decision(appliance_id="sub_1", reason="reason_a")
        decision_b = _make_control_decision(appliance_id="sub_2", reason="reason_b")
        data = _make_coordinator_data(control_decisions=[decision_a, decision_b])
        coord = _make_coordinator(data=data)

        sensor_a = PvApplianceStatusSensor(coord, "sub_1", "Heat Pump")
        sensor_b = PvApplianceStatusSensor(coord, "sub_2", "Dishwasher")

        assert sensor_a.native_value == "reason_a"
        assert sensor_b.native_value == "reason_b"

    def test_appliance_unique_id_includes_appliance_id(self):
        """Appliance sensor unique_id encodes both entry_id and appliance_id."""
        coord = _make_coordinator()
        sensor = PvAppliancePowerSensor(coord, "sub_1", "Heat Pump")
        assert sensor._attr_unique_id == "test_entry_123_appliance_sub_1_power"

    def test_appliance_sensor_handles_none_coordinator_data(self):
        """Appliance sensors return None when coordinator data is None."""
        coord = _make_coordinator(data=None)

        for sensor_cls in [PvAppliancePowerSensor, PvApplianceRuntimeSensor,
                           PvApplianceEnergySensor, PvApplianceStatusSensor]:
            sensor = sensor_cls(coord, "sub_1", "Heat Pump")
            assert sensor.native_value is None

    def test_multiple_appliance_sensors_independent(self):
        """Two appliances' sensors read from independent state entries."""
        state_1 = _make_appliance_state("sub_1", current_power=1000.0)
        state_2 = _make_appliance_state("sub_2", current_power=2000.0)
        data = _make_coordinator_data(
            appliance_states={"sub_1": state_1, "sub_2": state_2}
        )
        coord = _make_coordinator(data=data)

        sensor_1 = PvAppliancePowerSensor(coord, "sub_1", "Heat Pump")
        sensor_2 = PvAppliancePowerSensor(coord, "sub_2", "Dishwasher")

        assert sensor_1.native_value == 1000.0
        assert sensor_2.native_value == 2000.0


# ---------------------------------------------------------------------------
# Tests: edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Tests for edge cases and boundary conditions."""

    def test_plan_confidence_rounding(self):
        """Plan confidence is rounded to 1 decimal place."""
        plan = _make_plan(confidence=0.12345)
        data = _make_coordinator_data(current_plan=plan)
        coord = _make_coordinator(data=data)

        sensor = PvPlanConfidenceSensor(coord)
        assert sensor.native_value == 12.3

    def test_appliance_runtime_zero(self):
        """Zero runtime returns 0.0 hours."""
        app_state = _make_appliance_state("sub_1", runtime_hours=0.0)
        data = _make_coordinator_data(appliance_states={"sub_1": app_state})
        coord = _make_coordinator(data=data)

        sensor = PvApplianceRuntimeSensor(coord, "sub_1", "Heat Pump")
        assert sensor.native_value == 0.0

    def test_excess_power_negative(self):
        """Excess power can be negative (importing from grid)."""
        ps = _make_power_state(excess_power=-300.0)
        data = _make_coordinator_data(power_state=ps)
        coord = _make_coordinator(data=data)

        sensor = PvExcessPowerSensor(coord)
        assert sensor.native_value == -300.0

    def test_appliance_runtime_precision(self):
        """Runtime sensor preserves sub-hour precision (4 decimal places)."""
        # 90 minutes = 1.5 hours exactly
        app_state = _make_appliance_state("sub_1", runtime_hours=1.5)
        data = _make_coordinator_data(appliance_states={"sub_1": app_state})
        coord = _make_coordinator(data=data)

        sensor = PvApplianceRuntimeSensor(coord, "sub_1", "Charger")
        assert sensor.native_value == pytest.approx(1.5, rel=1e-4)
