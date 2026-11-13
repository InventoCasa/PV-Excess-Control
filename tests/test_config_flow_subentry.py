"""Tests for PV Excess Control appliance subentry config flow.

Since ConfigSubentryFlow may not be available in the test environment's HA
version, we mock the subentry base class methods (async_show_form,
async_create_entry, _get_reconfigure_subentry) and test the flow handler
directly.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
import voluptuous as vol

from custom_components.pv_excess_control.const import (
    CONF_ACTUAL_POWER_ENTITY,
    CONF_ALLOW_GRID_SUPPLEMENT,
    CONF_APPLIANCE_ENTITY,
    CONF_APPLIANCE_NAME,
    CONF_APPLIANCE_PRIORITY,
    CONF_BATTERY_DISCHARGE_OVERRIDE,
    CONF_CURRENT_ENTITY,
    CONF_DYNAMIC_CURRENT,
    CONF_EV_CONNECTED_ENTITY,
    CONF_EV_SOC_ENTITY,
    CONF_HELPER_ONLY,
    CONF_IS_BIG_CONSUMER,
    CONF_MAX_CURRENT,
    CONF_MAX_DAILY_RUNTIME,
    CONF_MAX_GRID_POWER,
    CONF_MIN_CURRENT,
    CONF_MIN_DAILY_RUNTIME,
    CONF_NOMINAL_POWER,
    CONF_ON_ONLY,
    CONF_PHASES,
    CONF_REQUIRES_APPLIANCE,
    CONF_SCHEDULE_DEADLINE,
    CONF_SWITCH_INTERVAL,
    DEFAULT_SWITCH_INTERVAL,
    DOMAIN,
)
from custom_components.pv_excess_control.config_flow import (
    ApplianceSubentryFlowHandler,
    PvExcessControlConfigFlow,
    SUBENTRY_TYPE_APPLIANCE,
    _appliance_basic_schema,
    _appliance_constraints_schema,
    _appliance_current_schema,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_subentry_flow() -> ApplianceSubentryFlowHandler:
    """Create an ApplianceSubentryFlowHandler with mocked HA internals.

    Since ConfigSubentryFlow may not be available, we mock the methods that
    the flow handler calls (async_show_form, async_create_entry).
    """
    flow = ApplianceSubentryFlowHandler()

    # Mock async_show_form to return a dict similar to HA's FlowResult
    def mock_show_form(*, step_id, data_schema, errors=None, last_step=None, **kwargs):
        return {
            "type": "form",
            "step_id": step_id,
            "data_schema": data_schema,
            "errors": errors or {},
            "last_step": last_step,
        }

    flow.async_show_form = mock_show_form

    # Mock async_create_entry to return a dict similar to HA's FlowResult
    def mock_create_entry(*, title, data, **kwargs):
        return {
            "type": "create_entry",
            "title": title,
            "data": data,
        }

    flow.async_create_entry = mock_create_entry

    return flow


def _schema_has_key(result: dict, key_name: str) -> bool:
    """Check if a form schema contains a specific key."""
    schema = result.get("data_schema")
    if schema is None:
        return False
    for key in schema.schema:
        actual_key = key.schema if isinstance(key, vol.Marker) else key
        if str(actual_key) == key_name:
            return True
    return False


# ---------------------------------------------------------------------------
# Minimal valid inputs for each step
# ---------------------------------------------------------------------------

VALID_BASIC_INPUT: dict[str, Any] = {
    CONF_APPLIANCE_NAME: "Heat Pump",
    CONF_APPLIANCE_ENTITY: "switch.heat_pump",
    CONF_APPLIANCE_PRIORITY: 100,
    CONF_NOMINAL_POWER: 2000,
    CONF_PHASES: "1",
}

VALID_CURRENT_INPUT_DISABLED: dict[str, Any] = {
    CONF_DYNAMIC_CURRENT: False,
}

VALID_CURRENT_INPUT_ENABLED: dict[str, Any] = {
    CONF_DYNAMIC_CURRENT: True,
    CONF_CURRENT_ENTITY: "number.charger_current",
    CONF_MIN_CURRENT: 6.0,
    CONF_MAX_CURRENT: 16.0,
}

VALID_CONSTRAINTS_INPUT: dict[str, Any] = {
    CONF_SWITCH_INTERVAL: DEFAULT_SWITCH_INTERVAL,
    CONF_ON_ONLY: False,
    CONF_ALLOW_GRID_SUPPLEMENT: False,
    CONF_IS_BIG_CONSUMER: False,
}


# ---------------------------------------------------------------------------
# Tests: async_get_supported_subentry_types
# ---------------------------------------------------------------------------


class TestSubentryTypes:
    """Tests for subentry type registration on the main config flow."""

    def test_supported_subentry_types(self):
        """Main config flow advertises the appliance subentry type."""
        mock_entry = MagicMock()
        result = PvExcessControlConfigFlow.async_get_supported_subentry_types(
            mock_entry
        )
        assert SUBENTRY_TYPE_APPLIANCE in result
        assert result[SUBENTRY_TYPE_APPLIANCE] is ApplianceSubentryFlowHandler


# ---------------------------------------------------------------------------
# Tests: Schema builder functions
# ---------------------------------------------------------------------------


class TestSchemaBuilders:
    """Tests for the appliance schema builder functions."""

    def test_basic_schema_has_required_fields(self):
        """Basic schema includes name, entity, priority, power, phases."""
        schema = _appliance_basic_schema()
        keys = [
            str(k.schema) if isinstance(k, vol.Marker) else str(k)
            for k in schema.schema
        ]
        assert CONF_APPLIANCE_NAME in keys
        assert CONF_APPLIANCE_ENTITY in keys
        assert CONF_APPLIANCE_PRIORITY in keys
        assert CONF_NOMINAL_POWER in keys
        assert CONF_PHASES in keys
        assert CONF_ACTUAL_POWER_ENTITY in keys

    def test_basic_schema_uses_defaults(self):
        """Basic schema uses provided defaults."""
        defaults = {CONF_APPLIANCE_NAME: "My Appliance", CONF_APPLIANCE_PRIORITY: 200}
        schema = _appliance_basic_schema(defaults)
        # Verify the schema builds without error with defaults
        for key in schema.schema:
            actual_key = key.schema if isinstance(key, vol.Marker) else key
            if str(actual_key) == CONF_APPLIANCE_NAME:
                assert key.default() == "My Appliance"
            if str(actual_key) == CONF_APPLIANCE_PRIORITY:
                assert key.default() == 200

    def test_current_schema_has_required_fields(self):
        """Current schema includes dynamic_current, current_entity, etc."""
        schema = _appliance_current_schema()
        keys = [
            str(k.schema) if isinstance(k, vol.Marker) else str(k)
            for k in schema.schema
        ]
        assert CONF_DYNAMIC_CURRENT in keys
        assert CONF_CURRENT_ENTITY in keys
        assert CONF_MIN_CURRENT in keys
        assert CONF_MAX_CURRENT in keys
        assert CONF_EV_SOC_ENTITY in keys
        assert CONF_EV_CONNECTED_ENTITY in keys

    def test_constraints_schema_has_required_fields(self):
        """Constraints schema includes all constraint, grid, and big consumer fields."""
        schema = _appliance_constraints_schema()
        keys = [
            str(k.schema) if isinstance(k, vol.Marker) else str(k)
            for k in schema.schema
        ]
        assert CONF_SWITCH_INTERVAL in keys
        assert CONF_ON_ONLY in keys
        assert CONF_MIN_DAILY_RUNTIME in keys
        assert CONF_MAX_DAILY_RUNTIME in keys
        assert CONF_SCHEDULE_DEADLINE in keys
        assert CONF_ALLOW_GRID_SUPPLEMENT in keys
        assert CONF_MAX_GRID_POWER in keys
        assert CONF_IS_BIG_CONSUMER in keys
        assert CONF_BATTERY_DISCHARGE_OVERRIDE in keys


# ---------------------------------------------------------------------------
# Tests: Step 1 - Basic Info + Power Profile (user step)
# ---------------------------------------------------------------------------


class TestStepUser:
    """Tests for the appliance basic info step."""

    @pytest.mark.asyncio
    async def test_shows_form(self):
        """User step shows form with basic info fields."""
        flow = _make_subentry_flow()
        result = await flow.async_step_user(user_input=None)

        assert result["type"] == "form"
        assert result["step_id"] == "user"
        assert _schema_has_key(result, CONF_APPLIANCE_NAME)
        assert _schema_has_key(result, CONF_APPLIANCE_ENTITY)
        assert _schema_has_key(result, CONF_NOMINAL_POWER)

    @pytest.mark.asyncio
    async def test_rejects_empty_name(self):
        """Empty appliance name is rejected."""
        flow = _make_subentry_flow()
        result = await flow.async_step_user(
            user_input={
                CONF_APPLIANCE_NAME: "",
                CONF_APPLIANCE_ENTITY: "switch.test",
                CONF_NOMINAL_POWER: 1000,
                CONF_PHASES: "1",
                CONF_APPLIANCE_PRIORITY: 500,
            }
        )

        assert result["type"] == "form"
        assert result["step_id"] == "user"
        assert result["errors"][CONF_APPLIANCE_NAME] == "missing_name"

    @pytest.mark.asyncio
    async def test_rejects_missing_entity(self):
        """Missing appliance entity is rejected."""
        flow = _make_subentry_flow()
        result = await flow.async_step_user(
            user_input={
                CONF_APPLIANCE_NAME: "Test",
                CONF_APPLIANCE_ENTITY: "",
                CONF_NOMINAL_POWER: 1000,
                CONF_PHASES: "1",
                CONF_APPLIANCE_PRIORITY: 500,
            }
        )

        assert result["type"] == "form"
        assert result["step_id"] == "user"
        assert result["errors"][CONF_APPLIANCE_ENTITY] == "missing_entity"

    @pytest.mark.asyncio
    async def test_rejects_zero_power(self):
        """Zero nominal power is rejected."""
        flow = _make_subentry_flow()
        result = await flow.async_step_user(
            user_input={
                CONF_APPLIANCE_NAME: "Test",
                CONF_APPLIANCE_ENTITY: "switch.test",
                CONF_NOMINAL_POWER: 0,
                CONF_PHASES: "1",
                CONF_APPLIANCE_PRIORITY: 500,
            }
        )

        assert result["type"] == "form"
        assert result["step_id"] == "user"
        assert result["errors"][CONF_NOMINAL_POWER] == "invalid_power"

    @pytest.mark.asyncio
    async def test_accepts_valid_input(self):
        """Valid basic input advances to current step."""
        flow = _make_subentry_flow()
        result = await flow.async_step_user(user_input=dict(VALID_BASIC_INPUT))

        assert result["type"] == "form"
        assert result["step_id"] == "current"
        # Verify data was stored
        assert flow._data[CONF_APPLIANCE_NAME] == "Heat Pump"
        assert flow._data[CONF_APPLIANCE_ENTITY] == "switch.heat_pump"
        assert flow._data[CONF_PHASES] == 1  # Converted from "1" to int
        assert flow._data[CONF_APPLIANCE_PRIORITY] == 100

    @pytest.mark.asyncio
    async def test_phases_converted_to_int(self):
        """Phases string is converted to integer."""
        flow = _make_subentry_flow()
        input_data = dict(VALID_BASIC_INPUT)
        input_data[CONF_PHASES] = "3"
        await flow.async_step_user(user_input=input_data)

        assert flow._data[CONF_PHASES] == 3
        assert isinstance(flow._data[CONF_PHASES], int)

    @pytest.mark.asyncio
    async def test_whitespace_name_rejected(self):
        """Name with only whitespace is rejected."""
        flow = _make_subentry_flow()
        result = await flow.async_step_user(
            user_input={
                CONF_APPLIANCE_NAME: "   ",
                CONF_APPLIANCE_ENTITY: "switch.test",
                CONF_NOMINAL_POWER: 1000,
                CONF_PHASES: "1",
                CONF_APPLIANCE_PRIORITY: 500,
            }
        )

        assert result["errors"][CONF_APPLIANCE_NAME] == "missing_name"


# ---------------------------------------------------------------------------
# Tests: Step 2 - Dynamic Current + EV Settings
# ---------------------------------------------------------------------------


class TestStepCurrent:
    """Tests for the dynamic current step."""

    @pytest.mark.asyncio
    async def test_shows_form(self):
        """Current step shows form with dynamic current fields."""
        flow = _make_subentry_flow()
        flow._data = dict(VALID_BASIC_INPUT)
        result = await flow.async_step_current(user_input=None)

        assert result["type"] == "form"
        assert result["step_id"] == "current"
        assert _schema_has_key(result, CONF_DYNAMIC_CURRENT)
        assert _schema_has_key(result, CONF_CURRENT_ENTITY)

    @pytest.mark.asyncio
    async def test_disabled_dynamic_current_advances(self):
        """Disabled dynamic current advances to constraints step."""
        flow = _make_subentry_flow()
        flow._data = dict(VALID_BASIC_INPUT)
        result = await flow.async_step_current(
            user_input=dict(VALID_CURRENT_INPUT_DISABLED)
        )

        assert result["type"] == "form"
        assert result["step_id"] == "constraints"
        assert flow._data[CONF_DYNAMIC_CURRENT] is False

    @pytest.mark.asyncio
    async def test_enabled_without_entity_rejected(self):
        """Dynamic current enabled without current entity is rejected."""
        flow = _make_subentry_flow()
        flow._data = dict(VALID_BASIC_INPUT)
        result = await flow.async_step_current(
            user_input={
                CONF_DYNAMIC_CURRENT: True,
                CONF_MIN_CURRENT: 6.0,
                CONF_MAX_CURRENT: 16.0,
            }
        )

        assert result["type"] == "form"
        assert result["step_id"] == "current"
        assert result["errors"][CONF_CURRENT_ENTITY] == "missing_current_entity"

    @pytest.mark.asyncio
    async def test_invalid_current_range_rejected(self):
        """Min current >= max current is rejected."""
        flow = _make_subentry_flow()
        flow._data = dict(VALID_BASIC_INPUT)
        result = await flow.async_step_current(
            user_input={
                CONF_DYNAMIC_CURRENT: True,
                CONF_CURRENT_ENTITY: "number.charger_current",
                CONF_MIN_CURRENT: 16.0,
                CONF_MAX_CURRENT: 6.0,
            }
        )

        assert result["type"] == "form"
        assert result["step_id"] == "current"
        assert result["errors"][CONF_MIN_CURRENT] == "invalid_current_range"

    @pytest.mark.asyncio
    async def test_equal_current_rejected(self):
        """Equal min and max current is rejected."""
        flow = _make_subentry_flow()
        flow._data = dict(VALID_BASIC_INPUT)
        result = await flow.async_step_current(
            user_input={
                CONF_DYNAMIC_CURRENT: True,
                CONF_CURRENT_ENTITY: "number.charger_current",
                CONF_MIN_CURRENT: 10.0,
                CONF_MAX_CURRENT: 10.0,
            }
        )

        assert result["type"] == "form"
        assert result["errors"][CONF_MIN_CURRENT] == "invalid_current_range"

    @pytest.mark.asyncio
    async def test_enabled_with_valid_data_advances(self):
        """Valid dynamic current config advances to constraints step."""
        flow = _make_subentry_flow()
        flow._data = dict(VALID_BASIC_INPUT)
        result = await flow.async_step_current(
            user_input=dict(VALID_CURRENT_INPUT_ENABLED)
        )

        assert result["type"] == "form"
        assert result["step_id"] == "constraints"
        assert flow._data[CONF_DYNAMIC_CURRENT] is True
        assert flow._data[CONF_CURRENT_ENTITY] == "number.charger_current"

    @pytest.mark.asyncio
    async def test_ev_fields_stored(self):
        """EV-related fields are properly stored."""
        flow = _make_subentry_flow()
        flow._data = dict(VALID_BASIC_INPUT)
        ev_input = dict(VALID_CURRENT_INPUT_ENABLED)
        ev_input[CONF_EV_SOC_ENTITY] = "sensor.ev_soc"
        ev_input[CONF_EV_CONNECTED_ENTITY] = "binary_sensor.ev_connected"
        await flow.async_step_current(user_input=ev_input)

        assert flow._data[CONF_EV_SOC_ENTITY] == "sensor.ev_soc"
        assert flow._data[CONF_EV_CONNECTED_ENTITY] == "binary_sensor.ev_connected"


# ---------------------------------------------------------------------------
# Tests: Step 3 - Constraints + Grid + Big Consumer
# ---------------------------------------------------------------------------


class TestStepConstraints:
    """Tests for the constraints step."""

    @pytest.mark.asyncio
    async def test_shows_form(self):
        """Constraints step shows form with all constraint fields."""
        flow = _make_subentry_flow()
        flow._data = {**VALID_BASIC_INPUT, **VALID_CURRENT_INPUT_DISABLED}
        result = await flow.async_step_constraints(user_input=None)

        assert result["type"] == "form"
        assert result["step_id"] == "constraints"
        assert _schema_has_key(result, CONF_SWITCH_INTERVAL)
        assert _schema_has_key(result, CONF_ON_ONLY)
        assert _schema_has_key(result, CONF_IS_BIG_CONSUMER)

    @pytest.mark.asyncio
    async def test_creates_entry(self):
        """Valid constraints input creates entry."""
        flow = _make_subentry_flow()
        flow._data = {**VALID_BASIC_INPUT, **VALID_CURRENT_INPUT_DISABLED}
        result = await flow.async_step_constraints(
            user_input=dict(VALID_CONSTRAINTS_INPUT)
        )

        assert result["type"] == "create_entry"
        assert result["title"] == "Heat Pump"
        # Verify all data is present
        assert result["data"][CONF_APPLIANCE_NAME] == "Heat Pump"
        assert result["data"][CONF_APPLIANCE_ENTITY] == "switch.heat_pump"
        assert result["data"][CONF_NOMINAL_POWER] == 2000
        assert result["data"][CONF_ON_ONLY] is False

    @pytest.mark.asyncio
    async def test_invalid_runtime_range_rejected(self):
        """Min runtime > max runtime is rejected."""
        flow = _make_subentry_flow()
        flow._data = {**VALID_BASIC_INPUT, **VALID_CURRENT_INPUT_DISABLED}
        input_data = dict(VALID_CONSTRAINTS_INPUT)
        input_data[CONF_MIN_DAILY_RUNTIME] = 120
        input_data[CONF_MAX_DAILY_RUNTIME] = 60
        result = await flow.async_step_constraints(user_input=input_data)

        assert result["type"] == "form"
        assert result["step_id"] == "constraints"
        assert result["errors"][CONF_MIN_DAILY_RUNTIME] == "invalid_runtime_range"

    @pytest.mark.asyncio
    async def test_grid_supplement_without_power_accepted(self):
        """Grid supplement enabled without max grid power is accepted (falls back to nominal_power)."""
        flow = _make_subentry_flow()
        flow._data = {**VALID_BASIC_INPUT, **VALID_CURRENT_INPUT_DISABLED}
        input_data = dict(VALID_CONSTRAINTS_INPUT)
        input_data[CONF_ALLOW_GRID_SUPPLEMENT] = True
        # No CONF_MAX_GRID_POWER - optimizer uses nominal_power as fallback
        result = await flow.async_step_constraints(user_input=input_data)

        assert result["type"] == "create_entry"
        assert result["data"][CONF_ALLOW_GRID_SUPPLEMENT] is True

    @pytest.mark.asyncio
    async def test_grid_supplement_with_power_accepted(self):
        """Grid supplement with max grid power creates entry."""
        flow = _make_subentry_flow()
        flow._data = {**VALID_BASIC_INPUT, **VALID_CURRENT_INPUT_DISABLED}
        input_data = dict(VALID_CONSTRAINTS_INPUT)
        input_data[CONF_ALLOW_GRID_SUPPLEMENT] = True
        input_data[CONF_MAX_GRID_POWER] = 500
        result = await flow.async_step_constraints(user_input=input_data)

        assert result["type"] == "create_entry"
        assert result["data"][CONF_ALLOW_GRID_SUPPLEMENT] is True
        assert result["data"][CONF_MAX_GRID_POWER] == 500

    @pytest.mark.asyncio
    async def test_big_consumer_with_discharge_override(self):
        """Big consumer with battery discharge override is accepted."""
        flow = _make_subentry_flow()
        flow._data = {**VALID_BASIC_INPUT, **VALID_CURRENT_INPUT_DISABLED}
        input_data = dict(VALID_CONSTRAINTS_INPUT)
        input_data[CONF_IS_BIG_CONSUMER] = True
        input_data[CONF_BATTERY_DISCHARGE_OVERRIDE] = 3000
        result = await flow.async_step_constraints(user_input=input_data)

        assert result["type"] == "create_entry"
        assert result["data"][CONF_IS_BIG_CONSUMER] is True
        assert result["data"][CONF_BATTERY_DISCHARGE_OVERRIDE] == 3000

    @pytest.mark.asyncio
    async def test_valid_runtime_range_accepted(self):
        """Valid min <= max runtime is accepted."""
        flow = _make_subentry_flow()
        flow._data = {**VALID_BASIC_INPUT, **VALID_CURRENT_INPUT_DISABLED}
        input_data = dict(VALID_CONSTRAINTS_INPUT)
        input_data[CONF_MIN_DAILY_RUNTIME] = 60
        input_data[CONF_MAX_DAILY_RUNTIME] = 120
        result = await flow.async_step_constraints(user_input=input_data)

        assert result["type"] == "create_entry"
        assert result["data"][CONF_MIN_DAILY_RUNTIME] == 60
        assert result["data"][CONF_MAX_DAILY_RUNTIME] == 120

    @pytest.mark.asyncio
    async def test_equal_runtime_accepted(self):
        """Equal min and max runtime is accepted."""
        flow = _make_subentry_flow()
        flow._data = {**VALID_BASIC_INPUT, **VALID_CURRENT_INPUT_DISABLED}
        input_data = dict(VALID_CONSTRAINTS_INPUT)
        input_data[CONF_MIN_DAILY_RUNTIME] = 60
        input_data[CONF_MAX_DAILY_RUNTIME] = 60
        result = await flow.async_step_constraints(user_input=input_data)

        assert result["type"] == "create_entry"

    @pytest.mark.asyncio
    async def test_schedule_deadline_stored(self):
        """Schedule deadline is properly stored in output data."""
        flow = _make_subentry_flow()
        flow._data = {**VALID_BASIC_INPUT, **VALID_CURRENT_INPUT_DISABLED}
        input_data = dict(VALID_CONSTRAINTS_INPUT)
        input_data[CONF_SCHEDULE_DEADLINE] = "18:00"
        result = await flow.async_step_constraints(user_input=input_data)

        assert result["type"] == "create_entry"
        assert result["data"][CONF_SCHEDULE_DEADLINE] == "18:00"

    @pytest.mark.asyncio
    async def test_helper_only_with_requires_appliance_rejected(self):
        """helper_only=True AND requires_appliance set is rejected."""
        flow = _make_subentry_flow()
        flow._data = {**VALID_BASIC_INPUT, **VALID_CURRENT_INPUT_DISABLED}
        input_data = dict(VALID_CONSTRAINTS_INPUT)
        input_data[CONF_HELPER_ONLY] = True
        input_data[CONF_REQUIRES_APPLIANCE] = "some-subentry-id"
        result = await flow.async_step_constraints(user_input=input_data)

        assert result["type"] == "form"
        assert result["step_id"] == "constraints"
        assert result["errors"][CONF_HELPER_ONLY] == "helper_only_with_requires"


# ---------------------------------------------------------------------------
# Tests: Full flow (all 3 steps)
# ---------------------------------------------------------------------------


class TestFullFlow:
    """Tests for the complete 3-step appliance subentry flow."""

    @pytest.mark.asyncio
    async def test_full_flow_simple_appliance(self):
        """Full flow for a simple on/off appliance with no dynamic current."""
        flow = _make_subentry_flow()

        # Step 1: Basic info
        result = await flow.async_step_user(user_input=dict(VALID_BASIC_INPUT))
        assert result["step_id"] == "current"

        # Step 2: No dynamic current
        result = await flow.async_step_current(
            user_input=dict(VALID_CURRENT_INPUT_DISABLED)
        )
        assert result["step_id"] == "constraints"

        # Step 3: Constraints
        result = await flow.async_step_constraints(
            user_input=dict(VALID_CONSTRAINTS_INPUT)
        )
        assert result["type"] == "create_entry"
        assert result["title"] == "Heat Pump"
        data = result["data"]
        assert data[CONF_APPLIANCE_NAME] == "Heat Pump"
        assert data[CONF_APPLIANCE_ENTITY] == "switch.heat_pump"
        assert data[CONF_NOMINAL_POWER] == 2000
        assert data[CONF_PHASES] == 1
        assert data[CONF_DYNAMIC_CURRENT] is False

    @pytest.mark.asyncio
    async def test_full_flow_ev_charger(self):
        """Full flow for an EV charger with dynamic current and EV sensors."""
        flow = _make_subentry_flow()

        # Step 1: Basic info
        basic = {
            CONF_APPLIANCE_NAME: "EV Charger",
            CONF_APPLIANCE_ENTITY: "switch.ev_charger",
            CONF_APPLIANCE_PRIORITY: 300,
            CONF_NOMINAL_POWER: 11000,
            CONF_PHASES: "3",
            CONF_ACTUAL_POWER_ENTITY: "sensor.ev_charger_power",
        }
        result = await flow.async_step_user(user_input=basic)
        assert result["step_id"] == "current"

        # Step 2: Dynamic current with EV sensors
        current = {
            CONF_DYNAMIC_CURRENT: True,
            CONF_CURRENT_ENTITY: "number.ev_charger_current",
            CONF_MIN_CURRENT: 6.0,
            CONF_MAX_CURRENT: 32.0,
            CONF_EV_SOC_ENTITY: "sensor.ev_battery_soc",
            CONF_EV_CONNECTED_ENTITY: "binary_sensor.ev_connected",
        }
        result = await flow.async_step_current(user_input=current)
        assert result["step_id"] == "constraints"

        # Step 3: Constraints with grid supplement
        constraints = {
            CONF_SWITCH_INTERVAL: 60,
            CONF_ON_ONLY: False,
            CONF_MIN_DAILY_RUNTIME: 30,
            CONF_ALLOW_GRID_SUPPLEMENT: True,
            CONF_MAX_GRID_POWER: 2000,
            CONF_IS_BIG_CONSUMER: True,
            CONF_BATTERY_DISCHARGE_OVERRIDE: 5000,
        }
        result = await flow.async_step_constraints(user_input=constraints)
        assert result["type"] == "create_entry"
        assert result["title"] == "EV Charger"

        data = result["data"]
        assert data[CONF_PHASES] == 3
        assert data[CONF_DYNAMIC_CURRENT] is True
        assert data[CONF_CURRENT_ENTITY] == "number.ev_charger_current"
        assert data[CONF_MAX_CURRENT] == 32.0
        assert data[CONF_EV_SOC_ENTITY] == "sensor.ev_battery_soc"
        assert data[CONF_EV_CONNECTED_ENTITY] == "binary_sensor.ev_connected"
        assert data[CONF_ALLOW_GRID_SUPPLEMENT] is True
        assert data[CONF_MAX_GRID_POWER] == 2000
        assert data[CONF_IS_BIG_CONSUMER] is True

    @pytest.mark.asyncio
    async def test_full_flow_data_keys_match_coordinator(self):
        """Output data keys match what coordinator._get_appliance_configs expects."""
        flow = _make_subentry_flow()

        await flow.async_step_user(user_input=dict(VALID_BASIC_INPUT))
        await flow.async_step_current(
            user_input=dict(VALID_CURRENT_INPUT_DISABLED)
        )
        result = await flow.async_step_constraints(
            user_input=dict(VALID_CONSTRAINTS_INPUT)
        )

        data = result["data"]
        # These are all the keys the coordinator reads from subentry.data
        expected_keys = {
            CONF_APPLIANCE_NAME,
            CONF_APPLIANCE_ENTITY,
            CONF_APPLIANCE_PRIORITY,
            CONF_NOMINAL_POWER,
            CONF_PHASES,
            CONF_DYNAMIC_CURRENT,
            CONF_SWITCH_INTERVAL,
            CONF_ON_ONLY,
            CONF_ALLOW_GRID_SUPPLEMENT,
            CONF_IS_BIG_CONSUMER,
        }
        # All expected keys must be present in the data
        for key in expected_keys:
            assert key in data, f"Missing key: {key}"


# ---------------------------------------------------------------------------
# Tests: Reconfigure flow
# ---------------------------------------------------------------------------


class TestReconfigure:
    """Tests for the appliance reconfigure flow."""

    def _make_reconfigure_flow(
        self, existing_data: dict[str, Any]
    ) -> ApplianceSubentryFlowHandler:
        """Create a flow with mocked _get_reconfigure_subentry."""
        flow = _make_subentry_flow()
        subentry = MagicMock()
        subentry.data = existing_data
        subentry.subentry_id = "mock_subentry_id"
        flow._get_reconfigure_subentry = MagicMock(return_value=subentry)
        # Mock HA reconfigure methods
        mock_entry = MagicMock()
        flow._get_entry = MagicMock(return_value=mock_entry)
        def mock_update_and_abort(entry, subentry, **kwargs):
            return {"type": "abort", "reason": "reconfigure_successful",
                    "title": kwargs.get("title", ""), "data": kwargs.get("data", {})}
        flow.async_update_and_abort = MagicMock(side_effect=mock_update_and_abort)
        return flow

    @pytest.mark.asyncio
    async def test_reconfigure_loads_existing_data(self):
        """Reconfigure step loads data from existing subentry."""
        existing = {
            CONF_APPLIANCE_NAME: "Old Pump",
            CONF_APPLIANCE_ENTITY: "switch.old_pump",
            CONF_APPLIANCE_PRIORITY: 200,
            CONF_NOMINAL_POWER: 1500,
            CONF_PHASES: 1,
        }
        flow = self._make_reconfigure_flow(existing)
        result = await flow.async_step_reconfigure(user_input=None)

        assert result["type"] == "form"
        assert result["step_id"] == "reconfigure_basic"
        # Data should be pre-loaded
        assert flow._data[CONF_APPLIANCE_NAME] == "Old Pump"
        assert flow._data[CONF_PHASES] == "1"  # Converted back to string

    @pytest.mark.asyncio
    async def test_reconfigure_phases_converted_to_string(self):
        """Existing phases int is converted to string for the selector."""
        existing = {
            CONF_APPLIANCE_NAME: "EV",
            CONF_APPLIANCE_ENTITY: "switch.ev",
            CONF_PHASES: 3,
            CONF_NOMINAL_POWER: 11000,
        }
        flow = self._make_reconfigure_flow(existing)
        await flow.async_step_reconfigure(user_input=None)
        assert flow._data[CONF_PHASES] == "3"

    @pytest.mark.asyncio
    async def test_reconfigure_basic_validation(self):
        """Reconfigure basic step validates the same as user step."""
        existing = {
            CONF_APPLIANCE_NAME: "Old Pump",
            CONF_APPLIANCE_ENTITY: "switch.old_pump",
            CONF_APPLIANCE_PRIORITY: 200,
            CONF_NOMINAL_POWER: 1500,
            CONF_PHASES: 1,
        }
        flow = self._make_reconfigure_flow(existing)
        # Enter reconfigure to load data
        await flow.async_step_reconfigure(user_input=None)

        # Submit with empty name - should fail
        result = await flow.async_step_reconfigure_basic(
            user_input={
                CONF_APPLIANCE_NAME: "",
                CONF_APPLIANCE_ENTITY: "switch.new",
                CONF_NOMINAL_POWER: 1000,
                CONF_PHASES: "1",
                CONF_APPLIANCE_PRIORITY: 500,
            }
        )
        assert result["errors"][CONF_APPLIANCE_NAME] == "missing_name"

    @pytest.mark.asyncio
    async def test_reconfigure_full_flow(self):
        """Complete reconfigure flow updates all data and creates entry."""
        existing = {
            CONF_APPLIANCE_NAME: "Old Pump",
            CONF_APPLIANCE_ENTITY: "switch.old_pump",
            CONF_APPLIANCE_PRIORITY: 200,
            CONF_NOMINAL_POWER: 1500,
            CONF_PHASES: 1,
            CONF_DYNAMIC_CURRENT: False,
            CONF_SWITCH_INTERVAL: DEFAULT_SWITCH_INTERVAL,
            CONF_ON_ONLY: False,
            CONF_ALLOW_GRID_SUPPLEMENT: False,
            CONF_IS_BIG_CONSUMER: False,
        }
        flow = self._make_reconfigure_flow(existing)

        # Step 1: Load existing data
        await flow.async_step_reconfigure(user_input=None)

        # Step 2: Update basic info
        result = await flow.async_step_reconfigure_basic(
            user_input={
                CONF_APPLIANCE_NAME: "New Pump",
                CONF_APPLIANCE_ENTITY: "switch.new_pump",
                CONF_APPLIANCE_PRIORITY: 100,
                CONF_NOMINAL_POWER: 2000,
                CONF_PHASES: "2",
            }
        )
        assert result["step_id"] == "reconfigure_current"

        # Step 3: Update current settings
        result = await flow.async_step_reconfigure_current(
            user_input={CONF_DYNAMIC_CURRENT: False}
        )
        assert result["step_id"] == "reconfigure_constraints"

        # Step 4: Update constraints
        result = await flow.async_step_reconfigure_constraints(
            user_input={
                CONF_SWITCH_INTERVAL: 120,
                CONF_ON_ONLY: True,
                CONF_ALLOW_GRID_SUPPLEMENT: False,
                CONF_IS_BIG_CONSUMER: False,
            }
        )
        assert result["type"] == "abort"
        assert result["reason"] == "reconfigure_successful"
        # Verify the update was called with correct data
        flow.async_update_and_abort.assert_called_once()
        call_kwargs = flow.async_update_and_abort.call_args
        updated_data = call_kwargs.kwargs.get("data") or call_kwargs[1].get("data", {})
        assert updated_data[CONF_APPLIANCE_NAME] == "New Pump"
        assert updated_data[CONF_APPLIANCE_ENTITY] == "switch.new_pump"
        assert updated_data[CONF_NOMINAL_POWER] == 2000
        assert updated_data[CONF_PHASES] == 2
        assert updated_data[CONF_ON_ONLY] is True

    @pytest.mark.asyncio
    async def test_reconfigure_current_validation(self):
        """Reconfigure current step validates the same as current step."""
        existing = {
            CONF_APPLIANCE_NAME: "EV",
            CONF_APPLIANCE_ENTITY: "switch.ev",
            CONF_APPLIANCE_PRIORITY: 300,
            CONF_NOMINAL_POWER: 11000,
            CONF_PHASES: 3,
            CONF_DYNAMIC_CURRENT: True,
            CONF_CURRENT_ENTITY: "number.ev_current",
            CONF_MIN_CURRENT: 6.0,
            CONF_MAX_CURRENT: 32.0,
        }
        flow = self._make_reconfigure_flow(existing)
        await flow.async_step_reconfigure(user_input=None)
        await flow.async_step_reconfigure_basic(
            user_input={
                CONF_APPLIANCE_NAME: "EV",
                CONF_APPLIANCE_ENTITY: "switch.ev",
                CONF_APPLIANCE_PRIORITY: 300,
                CONF_NOMINAL_POWER: 11000,
                CONF_PHASES: "3",
            }
        )

        # Invalid current range
        result = await flow.async_step_reconfigure_current(
            user_input={
                CONF_DYNAMIC_CURRENT: True,
                CONF_CURRENT_ENTITY: "number.ev_current",
                CONF_MIN_CURRENT: 20.0,
                CONF_MAX_CURRENT: 10.0,
            }
        )
        assert result["errors"][CONF_MIN_CURRENT] == "invalid_current_range"

    @pytest.mark.asyncio
    async def test_reconfigure_constraints_validation(self):
        """Reconfigure constraints step validates properly."""
        existing = {
            CONF_APPLIANCE_NAME: "Pump",
            CONF_APPLIANCE_ENTITY: "switch.pump",
            CONF_APPLIANCE_PRIORITY: 500,
            CONF_NOMINAL_POWER: 1000,
            CONF_PHASES: 1,
        }
        flow = self._make_reconfigure_flow(existing)
        await flow.async_step_reconfigure(user_input=None)
        await flow.async_step_reconfigure_basic(
            user_input={
                CONF_APPLIANCE_NAME: "Pump",
                CONF_APPLIANCE_ENTITY: "switch.pump",
                CONF_APPLIANCE_PRIORITY: 500,
                CONF_NOMINAL_POWER: 1000,
                CONF_PHASES: "1",
            }
        )
        await flow.async_step_reconfigure_current(
            user_input={CONF_DYNAMIC_CURRENT: False}
        )

        # Invalid runtime range
        result = await flow.async_step_reconfigure_constraints(
            user_input={
                CONF_SWITCH_INTERVAL: DEFAULT_SWITCH_INTERVAL,
                CONF_ON_ONLY: False,
                CONF_MIN_DAILY_RUNTIME: 200,
                CONF_MAX_DAILY_RUNTIME: 100,
                CONF_ALLOW_GRID_SUPPLEMENT: False,
                CONF_IS_BIG_CONSUMER: False,
            }
        )
        assert result["errors"][CONF_MIN_DAILY_RUNTIME] == "invalid_runtime_range"

    @pytest.mark.asyncio
    async def test_helper_only_with_requires_appliance_rejected_reconfigure(self):
        """helper_only=True AND requires_appliance set is rejected in reconfigure flow."""
        existing = {
            CONF_APPLIANCE_NAME: "Pump",
            CONF_APPLIANCE_ENTITY: "switch.pump",
            CONF_APPLIANCE_PRIORITY: 500,
            CONF_NOMINAL_POWER: 1000,
            CONF_PHASES: 1,
        }
        flow = self._make_reconfigure_flow(existing)
        await flow.async_step_reconfigure(user_input=None)
        await flow.async_step_reconfigure_basic(
            user_input={
                CONF_APPLIANCE_NAME: "Pump",
                CONF_APPLIANCE_ENTITY: "switch.pump",
                CONF_APPLIANCE_PRIORITY: 500,
                CONF_NOMINAL_POWER: 1000,
                CONF_PHASES: "1",
            }
        )
        await flow.async_step_reconfigure_current(
            user_input={CONF_DYNAMIC_CURRENT: False}
        )

        result = await flow.async_step_reconfigure_constraints(
            user_input={
                CONF_SWITCH_INTERVAL: DEFAULT_SWITCH_INTERVAL,
                CONF_ON_ONLY: False,
                CONF_MIN_DAILY_RUNTIME: 0,
                CONF_MAX_DAILY_RUNTIME: 0,
                CONF_ALLOW_GRID_SUPPLEMENT: False,
                CONF_IS_BIG_CONSUMER: False,
                CONF_HELPER_ONLY: True,
                CONF_REQUIRES_APPLIANCE: "some-subentry-id",
            }
        )

        assert result["type"] == "form"
        assert result["step_id"] == "reconfigure_constraints"
        assert result["errors"][CONF_HELPER_ONLY] == "helper_only_with_requires"


# ---------------------------------------------------------------------------
# Tests: Edge cases and data integrity
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Tests for edge cases and data integrity."""

    @pytest.mark.asyncio
    async def test_optional_fields_absent(self):
        """Optional fields can be absent from the output data."""
        flow = _make_subentry_flow()

        await flow.async_step_user(user_input=dict(VALID_BASIC_INPUT))
        await flow.async_step_current(
            user_input=dict(VALID_CURRENT_INPUT_DISABLED)
        )
        result = await flow.async_step_constraints(
            user_input=dict(VALID_CONSTRAINTS_INPUT)
        )

        data = result["data"]
        # Optional fields should not cause errors when absent
        assert data.get(CONF_ACTUAL_POWER_ENTITY) is None or CONF_ACTUAL_POWER_ENTITY in data
        assert data.get(CONF_EV_SOC_ENTITY) is None or CONF_EV_SOC_ENTITY not in data

    @pytest.mark.asyncio
    async def test_negative_power_rejected(self):
        """Negative nominal power is rejected."""
        flow = _make_subentry_flow()
        result = await flow.async_step_user(
            user_input={
                CONF_APPLIANCE_NAME: "Test",
                CONF_APPLIANCE_ENTITY: "switch.test",
                CONF_NOMINAL_POWER: -100,
                CONF_PHASES: "1",
                CONF_APPLIANCE_PRIORITY: 500,
            }
        )

        assert result["errors"][CONF_NOMINAL_POWER] == "invalid_power"

    @pytest.mark.asyncio
    async def test_data_accumulates_across_steps(self):
        """Data from all steps accumulates in the final output."""
        flow = _make_subentry_flow()

        # Step 1
        basic_input = {
            CONF_APPLIANCE_NAME: "Washer",
            CONF_APPLIANCE_ENTITY: "switch.washer",
            CONF_APPLIANCE_PRIORITY: 700,
            CONF_NOMINAL_POWER: 2200,
            CONF_PHASES: "1",
        }
        await flow.async_step_user(user_input=basic_input)

        # Step 2
        current_input = {
            CONF_DYNAMIC_CURRENT: False,
        }
        await flow.async_step_current(user_input=current_input)

        # Step 3
        constraints_input = {
            CONF_SWITCH_INTERVAL: 600,
            CONF_ON_ONLY: True,
            CONF_MIN_DAILY_RUNTIME: 30,
            CONF_MAX_DAILY_RUNTIME: 120,
            CONF_ALLOW_GRID_SUPPLEMENT: False,
            CONF_IS_BIG_CONSUMER: False,
        }
        result = await flow.async_step_constraints(user_input=constraints_input)

        data = result["data"]
        # Check data from step 1
        assert data[CONF_APPLIANCE_NAME] == "Washer"
        assert data[CONF_NOMINAL_POWER] == 2200
        # Check data from step 2
        assert data[CONF_DYNAMIC_CURRENT] is False
        # Check data from step 3
        assert data[CONF_SWITCH_INTERVAL] == 600
        assert data[CONF_ON_ONLY] is True
        assert data[CONF_MIN_DAILY_RUNTIME] == 30
        assert data[CONF_MAX_DAILY_RUNTIME] == 120

    @pytest.mark.asyncio
    async def test_title_uses_appliance_name(self):
        """Entry title uses the appliance name."""
        flow = _make_subentry_flow()

        await flow.async_step_user(
            user_input={
                CONF_APPLIANCE_NAME: "Solar Water Heater",
                CONF_APPLIANCE_ENTITY: "switch.water_heater",
                CONF_APPLIANCE_PRIORITY: 500,
                CONF_NOMINAL_POWER: 3000,
                CONF_PHASES: "1",
            }
        )
        await flow.async_step_current(
            user_input={CONF_DYNAMIC_CURRENT: False}
        )
        # First submit triggers big consumer warning (3000W >= threshold)
        result = await flow.async_step_constraints(
            user_input=dict(VALID_CONSTRAINTS_INPUT)
        )
        assert result["type"] == "form"
        assert result["errors"][CONF_IS_BIG_CONSUMER] == "suggest_big_consumer"

        # Second submit accepts the choice
        result = await flow.async_step_constraints(
            user_input=dict(VALID_CONSTRAINTS_INPUT)
        )
        assert result["title"] == "Solar Water Heater"


# ---------------------------------------------------------------------------
# Tests: cheap_grid_target_current field
# ---------------------------------------------------------------------------


class TestCheapGridTargetCurrent:
    """Tests for the cheap_grid_target_current field on the dynamic-current step."""

    @pytest.mark.asyncio
    async def test_cheap_grid_target_current_accepted_for_dynamic_appliance(self):
        """A dynamic-current appliance subentry accepts cheap_grid_target_current."""
        from custom_components.pv_excess_control.const import CONF_CHEAP_GRID_TARGET_CURRENT

        flow = _make_subentry_flow()
        flow._data = dict(VALID_BASIC_INPUT)

        # Submit the current step with dynamic current enabled and cheap_grid_target_current set
        current_input = dict(VALID_CURRENT_INPUT_ENABLED)
        current_input[CONF_CHEAP_GRID_TARGET_CURRENT] = 14.0

        result = await flow.async_step_current(user_input=current_input)

        # Should advance past the current step without errors
        assert result["type"] == "form"
        assert result["step_id"] == "constraints"
        assert flow._data[CONF_CHEAP_GRID_TARGET_CURRENT] == 14.0

    @pytest.mark.asyncio
    async def test_cheap_grid_target_current_ignored_for_non_dynamic_appliance(self):
        """A pre-filled cheap-window current must not block a non-dynamic appliance (#68)."""
        from custom_components.pv_excess_control.const import CONF_CHEAP_GRID_TARGET_CURRENT

        flow = _make_subentry_flow()
        flow._data = dict(VALID_BASIC_INPUT)
        flow._data[CONF_CHEAP_GRID_TARGET_CURRENT] = 16.0  # stale value from an earlier dynamic config

        result = await flow.async_step_current(
            user_input={
                CONF_DYNAMIC_CURRENT: False,
                CONF_CHEAP_GRID_TARGET_CURRENT: 14.0,
            }
        )

        assert result["type"] == "form"
        assert result["step_id"] == "constraints"
        assert CONF_CHEAP_GRID_TARGET_CURRENT not in flow._data

    @pytest.mark.asyncio
    async def test_cheap_grid_target_current_ignored_on_reconfigure_non_dynamic(self):
        """Reconfigure: switching dynamic current off with a pre-filled value proceeds (#68)."""
        from custom_components.pv_excess_control.const import CONF_CHEAP_GRID_TARGET_CURRENT

        flow = TestReconfigure._make_reconfigure_flow(None, {
            CONF_APPLIANCE_NAME: "Charger",
            CONF_APPLIANCE_ENTITY: "switch.charger",
            CONF_APPLIANCE_PRIORITY: 500,
            CONF_NOMINAL_POWER: 3680,
            CONF_PHASES: 1,
            CONF_DYNAMIC_CURRENT: True,
            CONF_CHEAP_GRID_TARGET_CURRENT: 16.0,
        })
        await flow.async_step_reconfigure(user_input=None)
        result = await flow.async_step_reconfigure_current(
            user_input={CONF_DYNAMIC_CURRENT: False, CONF_CHEAP_GRID_TARGET_CURRENT: 16.0}
        )

        assert result["type"] == "form"
        assert result["step_id"] == "reconfigure_constraints"
        assert CONF_CHEAP_GRID_TARGET_CURRENT not in flow._data
