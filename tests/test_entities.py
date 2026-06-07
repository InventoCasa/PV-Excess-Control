"""Tests for PV Excess Control entity platform files.

Covers: switch.py, number.py, binary_sensor.py, select.py
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.pv_excess_control.const import (
    BatteryStrategy,
    DOMAIN,
    MANUFACTURER,
    MAX_PRIORITY,
    MIN_PRIORITY,
)
from custom_components.pv_excess_control.models import ApplianceState, PowerState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_coordinator(
    enabled: bool = True,
    force_charge: bool = False,
    battery_strategy: str = BatteryStrategy.BALANCED,
    appliance_enabled: dict | None = None,
    appliance_overrides: dict | None = None,
    appliance_priorities: dict | None = None,
    appliance_min_daily_runtime: dict | None = None,
    appliance_max_daily_runtime: dict | None = None,
    coordinator_data: dict | None = None,
) -> MagicMock:
    """Return a mock PvExcessCoordinator."""
    coord = MagicMock()
    coord.config_entry = MagicMock()
    coord.config_entry.entry_id = "test_entry_id"

    # Control properties
    coord.enabled = enabled
    coord.force_charge = force_charge
    coord.battery_strategy = battery_strategy
    coord.appliance_enabled = appliance_enabled if appliance_enabled is not None else {}
    coord.appliance_overrides = appliance_overrides if appliance_overrides is not None else {}
    coord.appliance_priorities = appliance_priorities if appliance_priorities is not None else {}
    coord.appliance_min_daily_runtime = (
        appliance_min_daily_runtime if appliance_min_daily_runtime is not None else {}
    )
    coord.appliance_max_daily_runtime = (
        appliance_max_daily_runtime if appliance_max_daily_runtime is not None else {}
    )

    # async_request_refresh returns a coroutine (kept for compatibility)
    coord.async_request_refresh = AsyncMock()
    # async_write_ha_state is used by entities to push state immediately
    coord._async_write_ha_state = MagicMock()

    # coordinator.data
    coord.data = coordinator_data or {}

    return coord


def _make_power_state(excess_power: float = 0.0) -> PowerState:
    return PowerState(
        pv_production=1000.0,
        grid_export=max(excess_power, 0.0),
        grid_import=0.0,
        load_power=0.0,
        excess_power=excess_power,
        battery_soc=None,
        battery_power=None,
        ev_soc=None,
        timestamp=datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc),
    )


def _make_appliance_state(appliance_id: str, is_on: bool = False) -> ApplianceState:
    return ApplianceState(
        appliance_id=appliance_id,
        is_on=is_on,
        current_power=0.0,
        current_amperage=None,
        runtime_today=timedelta(),
        energy_today=0.0,
        last_state_change=None,
        ev_connected=None,
    )


# ---------------------------------------------------------------------------
# Switch entity tests
# ---------------------------------------------------------------------------

class TestSwitchEntities:
    """Tests for switch.py entities."""

    def test_master_switch_on_off_reads_coordinator_enabled(self):
        """ControlEnabledSwitch.is_on reflects coordinator.enabled."""
        from custom_components.pv_excess_control.switch import ControlEnabledSwitch

        coord = _make_coordinator(enabled=True)
        switch = ControlEnabledSwitch(coord)
        assert switch.is_on is True

        coord.enabled = False
        assert switch.is_on is False

    @pytest.mark.asyncio
    async def test_master_switch_turn_on(self):
        """Turning on the master switch sets coordinator.enabled = True."""
        from custom_components.pv_excess_control.switch import ControlEnabledSwitch

        coord = _make_coordinator(enabled=False)
        switch = ControlEnabledSwitch(coord)
        switch.async_write_ha_state = MagicMock()

        await switch.async_turn_on()

        assert coord.enabled is True
        switch.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_master_switch_turn_off(self):
        """Turning off the master switch sets coordinator.enabled = False."""
        from custom_components.pv_excess_control.switch import ControlEnabledSwitch

        coord = _make_coordinator(enabled=True)
        switch = ControlEnabledSwitch(coord)
        switch.async_write_ha_state = MagicMock()

        await switch.async_turn_off()

        assert coord.enabled is False
        switch.async_write_ha_state.assert_called_once()

    def test_force_charge_switch_reads_coordinator(self):
        """ForceChargeSwitch.is_on reflects coordinator.force_charge."""
        from custom_components.pv_excess_control.switch import ForceChargeSwitch

        coord = _make_coordinator(force_charge=False)
        switch = ForceChargeSwitch(coord)
        assert switch.is_on is False

        coord.force_charge = True
        assert switch.is_on is True

    @pytest.mark.asyncio
    async def test_force_charge_switch_turn_on(self):
        """Turning on force charge sets coordinator.force_charge = True."""
        from custom_components.pv_excess_control.switch import ForceChargeSwitch

        coord = _make_coordinator(force_charge=False)
        switch = ForceChargeSwitch(coord)
        switch.async_write_ha_state = MagicMock()

        await switch.async_turn_on()

        assert coord.force_charge is True
        switch.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_force_charge_switch_turn_off(self):
        """Turning off force charge sets coordinator.force_charge = False."""
        from custom_components.pv_excess_control.switch import ForceChargeSwitch

        coord = _make_coordinator(force_charge=True)
        switch = ForceChargeSwitch(coord)
        switch.async_write_ha_state = MagicMock()

        await switch.async_turn_off()

        assert coord.force_charge is False
        switch.async_write_ha_state.assert_called_once()

    def test_appliance_enable_switch_defaults_true(self):
        """ApplianceEnabledSwitch.is_on defaults True when not set."""
        from custom_components.pv_excess_control.switch import ApplianceEnabledSwitch

        coord = _make_coordinator(appliance_enabled={})
        switch = ApplianceEnabledSwitch(coord, "app_1", "Washing Machine")
        # Default is True when key absent
        assert switch.is_on is True

    @pytest.mark.asyncio
    async def test_appliance_enable_switch_turn_off(self):
        """ApplianceEnabledSwitch.async_turn_off sets enabled[id] = False."""
        from custom_components.pv_excess_control.switch import ApplianceEnabledSwitch

        coord = _make_coordinator(appliance_enabled={"app_1": True})
        switch = ApplianceEnabledSwitch(coord, "app_1", "Washing Machine")
        switch.async_write_ha_state = MagicMock()

        await switch.async_turn_off()

        assert coord.appliance_enabled["app_1"] is False
        switch.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_appliance_enable_switch_turn_on(self):
        """ApplianceEnabledSwitch.async_turn_on sets enabled[id] = True."""
        from custom_components.pv_excess_control.switch import ApplianceEnabledSwitch

        coord = _make_coordinator(appliance_enabled={"app_1": False})
        switch = ApplianceEnabledSwitch(coord, "app_1", "Washing Machine")
        switch.async_write_ha_state = MagicMock()

        await switch.async_turn_on()

        assert coord.appliance_enabled["app_1"] is True
        switch.async_write_ha_state.assert_called_once()

    def test_appliance_override_switch_defaults_false(self):
        """ApplianceOverrideSwitch.is_on defaults False when not set."""
        from custom_components.pv_excess_control.switch import ApplianceOverrideSwitch

        coord = _make_coordinator(appliance_overrides={})
        switch = ApplianceOverrideSwitch(coord, "app_1", "Washing Machine")
        assert switch.is_on is False

    @pytest.mark.asyncio
    async def test_appliance_override_switch_turn_on(self):
        """ApplianceOverrideSwitch.async_turn_on sets overrides[id] = True."""
        from custom_components.pv_excess_control.switch import ApplianceOverrideSwitch

        coord = _make_coordinator(appliance_overrides={})
        switch = ApplianceOverrideSwitch(coord, "app_1", "Washing Machine")
        switch.async_write_ha_state = MagicMock()

        await switch.async_turn_on()

        assert coord.appliance_overrides["app_1"] is True
        switch.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_appliance_override_switch_turn_off(self):
        """ApplianceOverrideSwitch.async_turn_off sets overrides[id] = False."""
        from custom_components.pv_excess_control.switch import ApplianceOverrideSwitch

        coord = _make_coordinator(appliance_overrides={"app_1": True})
        switch = ApplianceOverrideSwitch(coord, "app_1", "Washing Machine")
        switch.async_write_ha_state = MagicMock()

        await switch.async_turn_off()

        assert coord.appliance_overrides["app_1"] is False
        switch.async_write_ha_state.assert_called_once()

    def test_switch_unique_id(self):
        """Each switch has a unique entity ID based on entry_id."""
        from custom_components.pv_excess_control.switch import (
            ApplianceEnabledSwitch,
            ApplianceOverrideSwitch,
            ControlEnabledSwitch,
            ForceChargeSwitch,
        )

        coord = _make_coordinator()
        assert ControlEnabledSwitch(coord).unique_id == "test_entry_id_control_enabled"
        assert ForceChargeSwitch(coord).unique_id == "test_entry_id_force_charge"
        assert ApplianceEnabledSwitch(coord, "a1", "X").unique_id == "test_entry_id_a1_enabled"
        assert ApplianceOverrideSwitch(coord, "a1", "X").unique_id == "test_entry_id_a1_override"


# ---------------------------------------------------------------------------
# Number entity tests
# ---------------------------------------------------------------------------

class TestNumberEntities:
    """Tests for number.py entities."""

    def test_priority_number_range(self):
        """AppliancePriorityNumber has range 1-1000 step 1."""
        from custom_components.pv_excess_control.number import AppliancePriorityNumber

        coord = _make_coordinator()
        num = AppliancePriorityNumber(coord, "app_1", "Washing Machine")
        assert num.native_min_value == float(MIN_PRIORITY)
        assert num.native_max_value == float(MAX_PRIORITY)
        assert num.native_step == 1.0

    def test_priority_number_reads_coordinator(self):
        """AppliancePriorityNumber.native_value reads coordinator.appliance_priorities."""
        from custom_components.pv_excess_control.number import AppliancePriorityNumber

        coord = _make_coordinator(appliance_priorities={"app_1": 300})
        num = AppliancePriorityNumber(coord, "app_1", "Washing Machine")
        assert num.native_value == 300.0

    def test_priority_number_default_when_not_set(self):
        """AppliancePriorityNumber.native_value defaults to 500 when not in dict."""
        from custom_components.pv_excess_control.number import AppliancePriorityNumber

        coord = _make_coordinator(appliance_priorities={})
        num = AppliancePriorityNumber(coord, "app_1", "Washing Machine")
        assert num.native_value == 500.0

    @pytest.mark.asyncio
    async def test_priority_set_value(self):
        """Setting priority updates coordinator.appliance_priorities."""
        from custom_components.pv_excess_control.number import AppliancePriorityNumber

        coord = _make_coordinator(appliance_priorities={"app_1": 500})
        num = AppliancePriorityNumber(coord, "app_1", "Washing Machine")
        num.async_write_ha_state = MagicMock()

        await num.async_set_native_value(750.0)

        assert coord.appliance_priorities["app_1"] == 750
        num.async_write_ha_state.assert_called_once()

    def test_priority_number_unique_id(self):
        """AppliancePriorityNumber has expected unique_id."""
        from custom_components.pv_excess_control.number import AppliancePriorityNumber

        coord = _make_coordinator()
        num = AppliancePriorityNumber(coord, "app_1", "Washing Machine")
        assert num.unique_id == "test_entry_id_app_1_priority"

    def test_priority_number_name(self):
        """AppliancePriorityNumber name includes appliance name."""
        from custom_components.pv_excess_control.number import AppliancePriorityNumber

        coord = _make_coordinator()
        num = AppliancePriorityNumber(coord, "app_1", "Washing Machine")
        assert num.name == "Washing Machine Priority"

    def test_min_daily_runtime_resolution_prefers_runtime_dict(self):
        """When runtime dict has an override, it wins over sub_data."""
        appliance_min_daily_runtime = {"app_1": 45}
        sub_data = {"min_daily_runtime": 30}
        resolved = appliance_min_daily_runtime.get(
            "app_1", sub_data.get("min_daily_runtime")
        )
        assert resolved == 45

    def test_min_daily_runtime_resolution_falls_through_to_sub_data(self):
        """When runtime dict has no entry, sub_data wins."""
        appliance_min_daily_runtime: dict[str, int | None] = {}
        sub_data = {"min_daily_runtime": 30}
        resolved = appliance_min_daily_runtime.get(
            "app_1", sub_data.get("min_daily_runtime")
        )
        assert resolved == 30

    def test_min_daily_runtime_resolution_none_when_nothing_set(self):
        """When neither source has a value, resolves to None (disabled)."""
        appliance_min_daily_runtime: dict[str, int | None] = {}
        sub_data: dict = {}
        resolved = appliance_min_daily_runtime.get(
            "app_1", sub_data.get("min_daily_runtime")
        )
        assert resolved is None

    def test_min_daily_runtime_resolution_none_override_wins(self):
        """When runtime dict has an explicit None (user disabled via Number entity),
        it wins over a value still in sub_data."""
        appliance_min_daily_runtime = {"app_1": None}
        sub_data = {"min_daily_runtime": 30}
        resolved = appliance_min_daily_runtime.get(
            "app_1", sub_data.get("min_daily_runtime")
        )
        assert resolved is None

    def test_max_daily_runtime_resolution_prefers_runtime_dict(self):
        """When runtime dict has an override, it wins over sub_data."""
        appliance_max_daily_runtime = {"app_1": 240}
        sub_data = {"max_daily_runtime": 180}
        resolved = appliance_max_daily_runtime.get(
            "app_1", sub_data.get("max_daily_runtime")
        )
        assert resolved == 240

    def test_max_daily_runtime_resolution_falls_through_to_sub_data(self):
        """When runtime dict has no entry, sub_data wins."""
        appliance_max_daily_runtime: dict[str, int | None] = {}
        sub_data = {"max_daily_runtime": 180}
        resolved = appliance_max_daily_runtime.get(
            "app_1", sub_data.get("max_daily_runtime")
        )
        assert resolved == 180

    def test_max_daily_runtime_resolution_none_when_nothing_set(self):
        """When neither source has a value, resolves to None (disabled)."""
        appliance_max_daily_runtime: dict[str, int | None] = {}
        sub_data: dict = {}
        resolved = appliance_max_daily_runtime.get(
            "app_1", sub_data.get("max_daily_runtime")
        )
        assert resolved is None

    def test_max_daily_runtime_resolution_none_override_wins(self):
        """When runtime dict has an explicit None (user disabled via Number entity),
        it wins over a value still in sub_data."""
        appliance_max_daily_runtime = {"app_1": None}
        sub_data = {"max_daily_runtime": 180}
        resolved = appliance_max_daily_runtime.get(
            "app_1", sub_data.get("max_daily_runtime")
        )
        assert resolved is None

    def test_min_daily_runtime_number_range(self):
        """ApplianceMinDailyRuntimeNumber has range 0-1440 step 1, unit 'min'."""
        from custom_components.pv_excess_control.number import (
            ApplianceMinDailyRuntimeNumber,
        )

        coord = _make_coordinator()
        num = ApplianceMinDailyRuntimeNumber(coord, "app_1", "Pool Pump")
        assert num.native_min_value == 0.0
        assert num.native_max_value == 1440.0
        assert num.native_step == 1.0
        assert num.native_unit_of_measurement == "min"

    def test_min_daily_runtime_number_reads_runtime_dict(self):
        """native_value reads coordinator.appliance_min_daily_runtime."""
        from custom_components.pv_excess_control.number import (
            ApplianceMinDailyRuntimeNumber,
        )

        coord = _make_coordinator(appliance_min_daily_runtime={"app_1": 45})
        num = ApplianceMinDailyRuntimeNumber(coord, "app_1", "Pool Pump")
        assert num.native_value == 45.0

    def test_min_daily_runtime_number_returns_zero_when_none(self):
        """native_value returns 0.0 when the runtime-dict entry is None (disabled)."""
        from custom_components.pv_excess_control.number import (
            ApplianceMinDailyRuntimeNumber,
        )

        coord = _make_coordinator(appliance_min_daily_runtime={"app_1": None})
        num = ApplianceMinDailyRuntimeNumber(coord, "app_1", "Pool Pump")
        assert num.native_value == 0.0

    def test_min_daily_runtime_number_returns_zero_when_missing(self):
        """native_value returns 0.0 when no entry is present (unset)."""
        from custom_components.pv_excess_control.number import (
            ApplianceMinDailyRuntimeNumber,
        )

        coord = _make_coordinator(appliance_min_daily_runtime={})
        num = ApplianceMinDailyRuntimeNumber(coord, "app_1", "Pool Pump")
        assert num.native_value == 0.0

    def test_min_daily_runtime_number_unique_id(self):
        from custom_components.pv_excess_control.number import (
            ApplianceMinDailyRuntimeNumber,
        )

        coord = _make_coordinator()
        num = ApplianceMinDailyRuntimeNumber(coord, "app_1", "Pool Pump")
        assert num.unique_id == "test_entry_id_app_1_min_daily_runtime"

    def test_min_daily_runtime_number_name(self):
        from custom_components.pv_excess_control.number import (
            ApplianceMinDailyRuntimeNumber,
        )

        coord = _make_coordinator()
        num = ApplianceMinDailyRuntimeNumber(coord, "app_1", "Pool Pump")
        assert num.name == "Pool Pump Min Daily Runtime"

    def test_min_daily_runtime_number_icon(self):
        """ApplianceMinDailyRuntimeNumber uses the timer-sand icon."""
        from custom_components.pv_excess_control.number import (
            ApplianceMinDailyRuntimeNumber,
        )

        coord = _make_coordinator()
        num = ApplianceMinDailyRuntimeNumber(coord, "app_1", "Pool Pump")
        assert num.icon == "mdi:timer-sand"

    @pytest.mark.asyncio
    async def test_set_min_daily_runtime_writes_runtime_dict(self):
        """Setting a positive value updates the runtime dict with that int."""
        from custom_components.pv_excess_control.number import (
            ApplianceMinDailyRuntimeNumber,
        )

        coord = _make_coordinator(appliance_min_daily_runtime={})
        coord.config_entry.subentries = {}
        num = ApplianceMinDailyRuntimeNumber(coord, "app_1", "Pool Pump")
        num.async_write_ha_state = MagicMock()
        num.hass = MagicMock()

        await num.async_set_native_value(45.0)

        assert coord.appliance_min_daily_runtime["app_1"] == 45
        num.async_write_ha_state.assert_called()

    @pytest.mark.asyncio
    async def test_set_min_daily_runtime_zero_stores_none(self):
        """Setting 0 stores None (disabled sentinel)."""
        from custom_components.pv_excess_control.number import (
            ApplianceMinDailyRuntimeNumber,
        )

        coord = _make_coordinator(appliance_min_daily_runtime={"app_1": 60})
        coord.config_entry.subentries = {}
        num = ApplianceMinDailyRuntimeNumber(coord, "app_1", "Pool Pump")
        num.async_write_ha_state = MagicMock()
        num.hass = MagicMock()

        await num.async_set_native_value(0.0)

        assert coord.appliance_min_daily_runtime["app_1"] is None

    @pytest.mark.asyncio
    async def test_set_min_daily_runtime_persists_positive_to_subentry(self):
        """Setting a positive value calls async_update_subentry with the key set."""
        from custom_components.pv_excess_control.number import (
            ApplianceMinDailyRuntimeNumber,
        )

        coord = _make_coordinator(appliance_min_daily_runtime={})
        subentry = MagicMock()
        subentry.data = {"appliance_name": "Pool Pump"}
        coord.config_entry.subentries = {"app_1": subentry}
        num = ApplianceMinDailyRuntimeNumber(coord, "app_1", "Pool Pump")
        num.async_write_ha_state = MagicMock()
        num.hass = MagicMock()
        num.hass.config_entries.async_update_subentry = AsyncMock()

        await num.async_set_native_value(45.0)

        num.hass.config_entries.async_update_subentry.assert_awaited_once()
        _, call_kwargs = num.hass.config_entries.async_update_subentry.call_args
        assert call_kwargs["data"]["min_daily_runtime"] == 45

    @pytest.mark.asyncio
    async def test_set_min_daily_runtime_zero_pops_key_from_subentry(self):
        """Setting 0 calls async_update_subentry with the key removed (not set to 0)."""
        from custom_components.pv_excess_control.number import (
            ApplianceMinDailyRuntimeNumber,
        )

        coord = _make_coordinator(appliance_min_daily_runtime={"app_1": 60})
        subentry = MagicMock()
        subentry.data = {"appliance_name": "Pool Pump", "min_daily_runtime": 60}
        coord.config_entry.subentries = {"app_1": subentry}
        num = ApplianceMinDailyRuntimeNumber(coord, "app_1", "Pool Pump")
        num.async_write_ha_state = MagicMock()
        num.hass = MagicMock()
        num.hass.config_entries.async_update_subentry = AsyncMock()

        await num.async_set_native_value(0.0)

        num.hass.config_entries.async_update_subentry.assert_awaited_once()
        _, call_kwargs = num.hass.config_entries.async_update_subentry.call_args
        assert "min_daily_runtime" not in call_kwargs["data"]

    def test_max_daily_runtime_number_range(self):
        from custom_components.pv_excess_control.number import (
            ApplianceMaxDailyRuntimeNumber,
        )

        coord = _make_coordinator()
        num = ApplianceMaxDailyRuntimeNumber(coord, "app_1", "Pool Pump")
        assert num.native_min_value == 0.0
        assert num.native_max_value == 1440.0
        assert num.native_step == 1.0
        assert num.native_unit_of_measurement == "min"

    def test_max_daily_runtime_number_reads_runtime_dict(self):
        from custom_components.pv_excess_control.number import (
            ApplianceMaxDailyRuntimeNumber,
        )

        coord = _make_coordinator(appliance_max_daily_runtime={"app_1": 240})
        num = ApplianceMaxDailyRuntimeNumber(coord, "app_1", "Pool Pump")
        assert num.native_value == 240.0

    def test_max_daily_runtime_number_returns_zero_when_none(self):
        from custom_components.pv_excess_control.number import (
            ApplianceMaxDailyRuntimeNumber,
        )

        coord = _make_coordinator(appliance_max_daily_runtime={"app_1": None})
        num = ApplianceMaxDailyRuntimeNumber(coord, "app_1", "Pool Pump")
        assert num.native_value == 0.0

    def test_max_daily_runtime_number_returns_zero_when_missing(self):
        from custom_components.pv_excess_control.number import (
            ApplianceMaxDailyRuntimeNumber,
        )

        coord = _make_coordinator(appliance_max_daily_runtime={})
        num = ApplianceMaxDailyRuntimeNumber(coord, "app_1", "Pool Pump")
        assert num.native_value == 0.0

    def test_max_daily_runtime_number_unique_id(self):
        from custom_components.pv_excess_control.number import (
            ApplianceMaxDailyRuntimeNumber,
        )

        coord = _make_coordinator()
        num = ApplianceMaxDailyRuntimeNumber(coord, "app_1", "Pool Pump")
        assert num.unique_id == "test_entry_id_app_1_max_daily_runtime"

    def test_max_daily_runtime_number_name(self):
        from custom_components.pv_excess_control.number import (
            ApplianceMaxDailyRuntimeNumber,
        )

        coord = _make_coordinator()
        num = ApplianceMaxDailyRuntimeNumber(coord, "app_1", "Pool Pump")
        assert num.name == "Pool Pump Max Daily Runtime"

    def test_max_daily_runtime_number_icon(self):
        """ApplianceMaxDailyRuntimeNumber uses the timer-alert icon (not the min one)."""
        from custom_components.pv_excess_control.number import (
            ApplianceMaxDailyRuntimeNumber,
        )

        coord = _make_coordinator()
        num = ApplianceMaxDailyRuntimeNumber(coord, "app_1", "Pool Pump")
        assert num.icon == "mdi:timer-alert-outline"

    @pytest.mark.asyncio
    async def test_set_max_daily_runtime_writes_runtime_dict(self):
        from custom_components.pv_excess_control.number import (
            ApplianceMaxDailyRuntimeNumber,
        )

        coord = _make_coordinator(appliance_max_daily_runtime={})
        coord.config_entry.subentries = {}
        num = ApplianceMaxDailyRuntimeNumber(coord, "app_1", "Pool Pump")
        num.async_write_ha_state = MagicMock()
        num.hass = MagicMock()

        await num.async_set_native_value(240.0)

        assert coord.appliance_max_daily_runtime["app_1"] == 240
        num.async_write_ha_state.assert_called()

    @pytest.mark.asyncio
    async def test_set_max_daily_runtime_zero_stores_none(self):
        from custom_components.pv_excess_control.number import (
            ApplianceMaxDailyRuntimeNumber,
        )

        coord = _make_coordinator(appliance_max_daily_runtime={"app_1": 120})
        coord.config_entry.subentries = {}
        num = ApplianceMaxDailyRuntimeNumber(coord, "app_1", "Pool Pump")
        num.async_write_ha_state = MagicMock()
        num.hass = MagicMock()

        await num.async_set_native_value(0.0)

        assert coord.appliance_max_daily_runtime["app_1"] is None

    @pytest.mark.asyncio
    async def test_set_max_daily_runtime_persists_positive_to_subentry(self):
        from custom_components.pv_excess_control.number import (
            ApplianceMaxDailyRuntimeNumber,
        )

        coord = _make_coordinator(appliance_max_daily_runtime={})
        subentry = MagicMock()
        subentry.data = {"appliance_name": "Pool Pump"}
        coord.config_entry.subentries = {"app_1": subentry}
        num = ApplianceMaxDailyRuntimeNumber(coord, "app_1", "Pool Pump")
        num.async_write_ha_state = MagicMock()
        num.hass = MagicMock()
        num.hass.config_entries.async_update_subentry = AsyncMock()

        await num.async_set_native_value(240.0)

        num.hass.config_entries.async_update_subentry.assert_awaited_once()
        _, call_kwargs = num.hass.config_entries.async_update_subentry.call_args
        assert call_kwargs["data"]["max_daily_runtime"] == 240

    @pytest.mark.asyncio
    async def test_set_max_daily_runtime_zero_pops_key_from_subentry(self):
        from custom_components.pv_excess_control.number import (
            ApplianceMaxDailyRuntimeNumber,
        )

        coord = _make_coordinator(appliance_max_daily_runtime={"app_1": 120})
        subentry = MagicMock()
        subentry.data = {"appliance_name": "Pool Pump", "max_daily_runtime": 120}
        coord.config_entry.subentries = {"app_1": subentry}
        num = ApplianceMaxDailyRuntimeNumber(coord, "app_1", "Pool Pump")
        num.async_write_ha_state = MagicMock()
        num.hass = MagicMock()
        num.hass.config_entries.async_update_subentry = AsyncMock()

        await num.async_set_native_value(0.0)

        num.hass.config_entries.async_update_subentry.assert_awaited_once()
        _, call_kwargs = num.hass.config_entries.async_update_subentry.call_args
        assert "max_daily_runtime" not in call_kwargs["data"]

    @pytest.mark.asyncio
    async def test_set_min_clamps_to_max_when_above(self):
        """Setting min above current max silently clamps min down to max."""
        from custom_components.pv_excess_control.number import (
            ApplianceMinDailyRuntimeNumber,
        )

        coord = _make_coordinator(
            appliance_min_daily_runtime={"app_1": 30},
            appliance_max_daily_runtime={"app_1": 60},
        )
        coord.config_entry.subentries = {}
        num = ApplianceMinDailyRuntimeNumber(coord, "app_1", "Pool Pump")
        num.async_write_ha_state = MagicMock()
        num.hass = MagicMock()

        await num.async_set_native_value(120.0)

        assert coord.appliance_min_daily_runtime["app_1"] == 60

    @pytest.mark.asyncio
    async def test_set_min_with_max_none_no_clamp(self):
        """When max is None (unlimited), min is not clamped."""
        from custom_components.pv_excess_control.number import (
            ApplianceMinDailyRuntimeNumber,
        )

        coord = _make_coordinator(
            appliance_min_daily_runtime={},
            appliance_max_daily_runtime={"app_1": None},
        )
        coord.config_entry.subentries = {}
        num = ApplianceMinDailyRuntimeNumber(coord, "app_1", "Pool Pump")
        num.async_write_ha_state = MagicMock()
        num.hass = MagicMock()

        await num.async_set_native_value(500.0)

        assert coord.appliance_min_daily_runtime["app_1"] == 500

    @pytest.mark.asyncio
    async def test_set_min_with_max_missing_no_clamp(self):
        """When max has no entry anywhere, min is not clamped (treated as unlimited)."""
        from custom_components.pv_excess_control.number import (
            ApplianceMinDailyRuntimeNumber,
        )

        coord = _make_coordinator(
            appliance_min_daily_runtime={},
            appliance_max_daily_runtime={},
        )
        coord.config_entry.subentries = {}
        num = ApplianceMinDailyRuntimeNumber(coord, "app_1", "Pool Pump")
        num.async_write_ha_state = MagicMock()
        num.hass = MagicMock()

        await num.async_set_native_value(500.0)

        assert coord.appliance_min_daily_runtime["app_1"] == 500

    @pytest.mark.asyncio
    async def test_set_max_below_min_lowers_min(self):
        """Setting max below current min lowers min to match, then writes max."""
        from custom_components.pv_excess_control.number import (
            ApplianceMaxDailyRuntimeNumber,
        )

        coord = _make_coordinator(
            appliance_min_daily_runtime={"app_1": 120},
            appliance_max_daily_runtime={"app_1": 180},
        )
        coord.config_entry.subentries = {}
        num = ApplianceMaxDailyRuntimeNumber(coord, "app_1", "Pool Pump")
        num.async_write_ha_state = MagicMock()
        num.hass = MagicMock()

        await num.async_set_native_value(60.0)

        assert coord.appliance_min_daily_runtime["app_1"] == 60
        assert coord.appliance_max_daily_runtime["app_1"] == 60

    @pytest.mark.asyncio
    async def test_set_max_zero_does_not_touch_min(self):
        """Setting max=0 (disable) with min=120: min stays 120 (None = unlimited)."""
        from custom_components.pv_excess_control.number import (
            ApplianceMaxDailyRuntimeNumber,
        )

        coord = _make_coordinator(
            appliance_min_daily_runtime={"app_1": 120},
            appliance_max_daily_runtime={"app_1": 180},
        )
        coord.config_entry.subentries = {}
        num = ApplianceMaxDailyRuntimeNumber(coord, "app_1", "Pool Pump")
        num.async_write_ha_state = MagicMock()
        num.hass = MagicMock()

        await num.async_set_native_value(0.0)

        assert coord.appliance_min_daily_runtime["app_1"] == 120
        assert coord.appliance_max_daily_runtime["app_1"] is None

    @pytest.mark.asyncio
    async def test_set_max_above_min_no_change_to_min(self):
        """Setting max above current min leaves min alone."""
        from custom_components.pv_excess_control.number import (
            ApplianceMaxDailyRuntimeNumber,
        )

        coord = _make_coordinator(
            appliance_min_daily_runtime={"app_1": 60},
            appliance_max_daily_runtime={"app_1": 120},
        )
        coord.config_entry.subentries = {}
        num = ApplianceMaxDailyRuntimeNumber(coord, "app_1", "Pool Pump")
        num.async_write_ha_state = MagicMock()
        num.hass = MagicMock()

        await num.async_set_native_value(240.0)

        assert coord.appliance_min_daily_runtime["app_1"] == 60
        assert coord.appliance_max_daily_runtime["app_1"] == 240

    @pytest.mark.asyncio
    async def test_set_max_with_min_missing_no_sibling_lower(self):
        """When no min is set anywhere, _effective_min returns None and sibling-lowering is skipped."""
        from custom_components.pv_excess_control.number import (
            ApplianceMaxDailyRuntimeNumber,
        )

        coord = _make_coordinator(
            appliance_min_daily_runtime={},
            appliance_max_daily_runtime={},
        )
        coord.config_entry.subentries = {}
        num = ApplianceMaxDailyRuntimeNumber(coord, "app_1", "Pool Pump")
        num.async_write_ha_state = MagicMock()
        num.hass = MagicMock()

        await num.async_set_native_value(60.0)

        assert "app_1" not in coord.appliance_min_daily_runtime
        assert coord.appliance_max_daily_runtime["app_1"] == 60

    @pytest.mark.asyncio
    async def test_set_max_below_min_persists_lowered_min_to_subentry(self):
        """Sibling-lowering calls async_update_subentry with the min key updated to the new lower value."""
        from custom_components.pv_excess_control.number import (
            ApplianceMaxDailyRuntimeNumber,
        )

        coord = _make_coordinator(
            appliance_min_daily_runtime={"app_1": 120},
            appliance_max_daily_runtime={"app_1": 180},
        )
        subentry = MagicMock()
        subentry.data = {
            "appliance_name": "Pool Pump",
            "min_daily_runtime": 120,
            "max_daily_runtime": 180,
        }
        coord.config_entry.subentries = {"app_1": subentry}
        num = ApplianceMaxDailyRuntimeNumber(coord, "app_1", "Pool Pump")
        num.async_write_ha_state = MagicMock()
        num.hass = MagicMock()

        # Simulate HA mutating subentry.data in place after each persist so
        # the second persist reads the lowered min, mirroring real behaviour.
        async def _fake_update(entry, sub, *, data):
            sub.data = data

        num.hass.config_entries.async_update_subentry = AsyncMock(side_effect=_fake_update)

        await num.async_set_native_value(60.0)

        # Called twice: once for min sibling, once for max self
        assert num.hass.config_entries.async_update_subentry.await_count == 2
        # The two calls, in order: min first, then max
        min_call_kwargs = num.hass.config_entries.async_update_subentry.call_args_list[0].kwargs
        max_call_kwargs = num.hass.config_entries.async_update_subentry.call_args_list[1].kwargs
        assert min_call_kwargs["data"]["min_daily_runtime"] == 60
        # Second call must carry the already-lowered min (guards against a
        # bug where the max persist reads stale subentry.data).
        assert max_call_kwargs["data"]["min_daily_runtime"] == 60
        assert max_call_kwargs["data"]["max_daily_runtime"] == 60

    @pytest.mark.asyncio
    async def test_async_setup_entry_adds_three_entities_per_subentry(self):
        """async_setup_entry adds priority, min, and max entities for each subentry."""
        from custom_components.pv_excess_control.number import (
            ApplianceMaxDailyRuntimeNumber,
            ApplianceMinDailyRuntimeNumber,
            AppliancePriorityNumber,
            async_setup_entry,
        )

        added: list[tuple] = []  # (entities, subentry_id)
        def _add(entities, update_before_add=False, config_subentry_id=None):
            added.append((list(entities), config_subentry_id))

        hass = MagicMock()
        coord = _make_coordinator()
        config_entry = MagicMock()
        config_entry.entry_id = "test_entry_id"
        subentry = MagicMock()
        subentry.data = {"appliance_name": "Pool Pump"}
        config_entry.subentries = {"app_1": subentry}

        hass.data = {"pv_excess_control": {config_entry.entry_id: coord}}

        await async_setup_entry(hass, config_entry, _add)

        all_entities = [e for entities, _ in added for e in entities]
        classes = {type(e) for e in all_entities}
        assert AppliancePriorityNumber in classes
        assert ApplianceMinDailyRuntimeNumber in classes
        assert ApplianceMaxDailyRuntimeNumber in classes
        assert len(all_entities) == 3
        # Per-appliance entities must be registered under their subentry
        subentry_calls = [(entities, sid) for entities, sid in added if sid == "app_1"]
        assert len(subentry_calls) == 1
        assert len(subentry_calls[0][0]) == 3


# ---------------------------------------------------------------------------
# Binary sensor entity tests
# ---------------------------------------------------------------------------

class TestBinarySensorEntities:
    """Tests for binary_sensor.py entities."""

    def test_excess_available_when_excess_positive(self):
        """ExcessAvailableBinarySensor is ON when excess_power > 0."""
        from custom_components.pv_excess_control.binary_sensor import (
            ExcessAvailableBinarySensor,
        )

        ps = _make_power_state(excess_power=500.0)
        coord = _make_coordinator(coordinator_data={"power_state": ps})
        sensor = ExcessAvailableBinarySensor(coord)
        assert sensor.is_on is True

    def test_excess_available_when_excess_zero(self):
        """ExcessAvailableBinarySensor is OFF when excess_power == 0."""
        from custom_components.pv_excess_control.binary_sensor import (
            ExcessAvailableBinarySensor,
        )

        ps = _make_power_state(excess_power=0.0)
        coord = _make_coordinator(coordinator_data={"power_state": ps})
        sensor = ExcessAvailableBinarySensor(coord)
        assert sensor.is_on is False

    def test_excess_available_when_excess_negative(self):
        """ExcessAvailableBinarySensor is OFF when excess_power < 0."""
        from custom_components.pv_excess_control.binary_sensor import (
            ExcessAvailableBinarySensor,
        )

        ps = _make_power_state(excess_power=-100.0)
        coord = _make_coordinator(coordinator_data={"power_state": ps})
        sensor = ExcessAvailableBinarySensor(coord)
        assert sensor.is_on is False

    def test_excess_available_when_no_power_state(self):
        """ExcessAvailableBinarySensor is OFF when power_state is None."""
        from custom_components.pv_excess_control.binary_sensor import (
            ExcessAvailableBinarySensor,
        )

        coord = _make_coordinator(coordinator_data={"power_state": None})
        sensor = ExcessAvailableBinarySensor(coord)
        assert sensor.is_on is False

    def test_appliance_active_when_on(self):
        """ApplianceActiveBinarySensor is ON when appliance is_on."""
        from custom_components.pv_excess_control.binary_sensor import (
            ApplianceActiveBinarySensor,
        )

        app_state = _make_appliance_state("app_1", is_on=True)
        coord = _make_coordinator(
            coordinator_data={"appliance_states": {"app_1": app_state}}
        )
        sensor = ApplianceActiveBinarySensor(coord, "app_1", "Washing Machine")
        assert sensor.is_on is True

    def test_appliance_active_when_off(self):
        """ApplianceActiveBinarySensor is OFF when appliance is not on."""
        from custom_components.pv_excess_control.binary_sensor import (
            ApplianceActiveBinarySensor,
        )

        app_state = _make_appliance_state("app_1", is_on=False)
        coord = _make_coordinator(
            coordinator_data={"appliance_states": {"app_1": app_state}}
        )
        sensor = ApplianceActiveBinarySensor(coord, "app_1", "Washing Machine")
        assert sensor.is_on is False

    def test_appliance_active_when_state_missing(self):
        """ApplianceActiveBinarySensor is OFF when appliance_state not found."""
        from custom_components.pv_excess_control.binary_sensor import (
            ApplianceActiveBinarySensor,
        )

        coord = _make_coordinator(coordinator_data={"appliance_states": {}})
        sensor = ApplianceActiveBinarySensor(coord, "app_1", "Washing Machine")
        assert sensor.is_on is False

    def test_excess_sensor_unique_id(self):
        """ExcessAvailableBinarySensor has expected unique_id."""
        from custom_components.pv_excess_control.binary_sensor import (
            ExcessAvailableBinarySensor,
        )

        coord = _make_coordinator()
        sensor = ExcessAvailableBinarySensor(coord)
        assert sensor.unique_id == "test_entry_id_excess_available"

    def test_appliance_active_unique_id(self):
        """ApplianceActiveBinarySensor has expected unique_id."""
        from custom_components.pv_excess_control.binary_sensor import (
            ApplianceActiveBinarySensor,
        )

        coord = _make_coordinator()
        sensor = ApplianceActiveBinarySensor(coord, "app_1", "Dishwasher")
        assert sensor.unique_id == "test_entry_id_app_1_active"


# ---------------------------------------------------------------------------
# Select entity tests
# ---------------------------------------------------------------------------

class TestSelectEntities:
    """Tests for select.py entities."""

    def test_battery_strategy_options(self):
        """BatteryStrategySelect has exactly 3 strategy options."""
        from custom_components.pv_excess_control.select import BatteryStrategySelect

        coord = _make_coordinator()
        sel = BatteryStrategySelect(coord)
        assert len(sel.options) == 3
        assert BatteryStrategy.BATTERY_FIRST in sel.options
        assert BatteryStrategy.APPLIANCE_FIRST in sel.options
        assert BatteryStrategy.BALANCED in sel.options

    def test_battery_strategy_current_option_reads_coordinator(self):
        """BatteryStrategySelect.current_option reflects coordinator.battery_strategy."""
        from custom_components.pv_excess_control.select import BatteryStrategySelect

        coord = _make_coordinator(battery_strategy=BatteryStrategy.BATTERY_FIRST)
        sel = BatteryStrategySelect(coord)
        assert sel.current_option == BatteryStrategy.BATTERY_FIRST

    @pytest.mark.asyncio
    async def test_battery_strategy_select(self):
        """Selecting an option updates coordinator.battery_strategy."""
        from custom_components.pv_excess_control.select import BatteryStrategySelect

        coord = _make_coordinator(battery_strategy=BatteryStrategy.BALANCED)
        sel = BatteryStrategySelect(coord)
        sel.async_write_ha_state = MagicMock()

        await sel.async_select_option(BatteryStrategy.APPLIANCE_FIRST)

        assert coord.battery_strategy == BatteryStrategy.APPLIANCE_FIRST
        sel.async_write_ha_state.assert_called_once()

    def test_battery_strategy_unique_id(self):
        """BatteryStrategySelect has expected unique_id."""
        from custom_components.pv_excess_control.select import BatteryStrategySelect

        coord = _make_coordinator()
        sel = BatteryStrategySelect(coord)
        assert sel.unique_id == "test_entry_id_battery_strategy"

    def test_battery_strategy_name(self):
        """BatteryStrategySelect has expected name."""
        from custom_components.pv_excess_control.select import BatteryStrategySelect

        coord = _make_coordinator()
        sel = BatteryStrategySelect(coord)
        assert sel.name == "Battery Strategy"


# ---------------------------------------------------------------------------
# Subentry association tests
# ---------------------------------------------------------------------------

class TestSubentryAssociation:
    """Per-appliance entities must carry _attr_config_subentry_id so HA ties
    them to the correct subentry (and removes them when it is deleted)."""

    def test_appliance_switch_has_config_subentry_id(self):
        from custom_components.pv_excess_control.switch import ApplianceEnabledSwitch
        coord = _make_coordinator()
        sw = ApplianceEnabledSwitch(coord, "sub_abc", "Pump")
        assert sw._attr_config_subentry_id == "sub_abc"

    def test_appliance_override_switch_has_config_subentry_id(self):
        from custom_components.pv_excess_control.switch import ApplianceOverrideSwitch
        coord = _make_coordinator()
        sw = ApplianceOverrideSwitch(coord, "sub_abc", "Pump")
        assert sw._attr_config_subentry_id == "sub_abc"

    def test_system_switch_has_no_config_subentry_id(self):
        from custom_components.pv_excess_control.switch import ControlEnabledSwitch
        coord = _make_coordinator()
        sw = ControlEnabledSwitch(coord)
        assert getattr(sw, "_attr_config_subentry_id", None) is None

    def test_priority_number_has_config_subentry_id(self):
        from custom_components.pv_excess_control.number import AppliancePriorityNumber
        coord = _make_coordinator()
        n = AppliancePriorityNumber(coord, "sub_abc", "Pump")
        assert n._attr_config_subentry_id == "sub_abc"

    def test_min_runtime_number_has_config_subentry_id(self):
        from custom_components.pv_excess_control.number import ApplianceMinDailyRuntimeNumber
        coord = _make_coordinator()
        n = ApplianceMinDailyRuntimeNumber(coord, "sub_abc", "Pump")
        assert n._attr_config_subentry_id == "sub_abc"

    def test_max_runtime_number_has_config_subentry_id(self):
        from custom_components.pv_excess_control.number import ApplianceMaxDailyRuntimeNumber
        coord = _make_coordinator()
        n = ApplianceMaxDailyRuntimeNumber(coord, "sub_abc", "Pump")
        assert n._attr_config_subentry_id == "sub_abc"

    def test_appliance_active_sensor_has_config_subentry_id(self):
        from custom_components.pv_excess_control.binary_sensor import ApplianceActiveBinarySensor
        coord = _make_coordinator()
        bs = ApplianceActiveBinarySensor(coord, "sub_abc", "Pump")
        assert bs._attr_config_subentry_id == "sub_abc"

    def test_excess_binary_sensor_has_no_config_subentry_id(self):
        from custom_components.pv_excess_control.binary_sensor import ExcessAvailableBinarySensor
        coord = _make_coordinator()
        bs = ExcessAvailableBinarySensor(coord)
        assert getattr(bs, "_attr_config_subentry_id", None) is None
