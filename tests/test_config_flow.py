"""Tests for PV Excess Control config flow.

Uses unittest.mock to simulate the Home Assistant config flow machinery.
Tests verify that each step collects the right data, validates inputs,
shows conditional fields, and that the full flow creates a correct entry.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol

from custom_components.pv_excess_control.const import (
    CONF_ALLOW_GRID_CHARGING,
    CONF_BATTERY_CAPACITY,
    CONF_BATTERY_CHARGE_PRICE_THRESHOLD,
    CONF_BATTERY_MAX_CHARGE_POWER_W,
    CONF_BATTERY_MAX_DISCHARGE_DEFAULT,
    CONF_BATTERY_MAX_DISCHARGE_ENTITY,
    CONF_BATTERY_POWER,
    CONF_BATTERY_SOC,
    CONF_BATTERY_STRATEGY,
    CONF_BATTERY_TARGET_SOC,
    CONF_BATTERY_TARGET_TIME,
    CONF_BATTERY_TRICKLE_CHARGE_POWER_W,
    CONF_CHEAP_PRICE_THRESHOLD,
    CONF_CONTROLLER_INTERVAL,
    CONF_DYNAMIC_BATTERY_CHARGE_ENABLED,
    CONF_EXPORT_LIMIT,
    CONF_FEED_IN_TARIFF,
    CONF_FORECAST_PROVIDER,
    CONF_FORECAST_SENSOR,
    CONF_GRID_EXPORT,
    CONF_GRID_VOLTAGE,
    CONF_IMPORT_EXPORT,
    CONF_INVERTER_BATTERY_MAX_CHARGE_POWER_ENTITY,
    CONF_INVERTER_TYPE,
    CONF_LOAD_POWER,
    CONF_PLANNER_INTERVAL,
    CONF_PRICE_SENSOR,
    CONF_PV_POWER,
    CONF_TARIFF_PROVIDER,
    DEFAULT_BATTERY_TRICKLE_CHARGE_POWER_W,
    DEFAULT_CONTROLLER_INTERVAL,
    DEFAULT_GRID_VOLTAGE,
    DEFAULT_PLANNER_INTERVAL,
    DOMAIN,
    BatteryStrategy,
    ForecastProvider,
    InverterType,
    TariffProvider,
)
from custom_components.pv_excess_control.config_flow import (
    PvExcessControlConfigFlow,
    _sensor_schema,
    _energy_schema,
    _forecast_schema,
    BATTERY_SCHEMA,
    SETTINGS_SCHEMA,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_flow() -> PvExcessControlConfigFlow:
    """Create a PvExcessControlConfigFlow with mocked HA internals."""
    flow = PvExcessControlConfigFlow()
    flow.hass = MagicMock()
    flow.handler = DOMAIN
    flow.flow_id = "test_flow_id"
    flow.context = {"source": "user"}
    # Singleton check: no existing entries by default
    flow._async_current_entries = MagicMock(return_value=[])
    return flow


def _extract_form_keys(result: dict) -> list[str]:
    """Extract data field keys from a form result."""
    schema = result["data_schema"]
    if schema is None:
        return []
    return [
        str(key) if not isinstance(key, vol.Marker) else str(key.schema)
        for key in schema.schema
    ]


def _schema_has_key(result: dict, key_name: str) -> bool:
    """Check if a form schema contains a specific key."""
    schema = result["data_schema"]
    if schema is None:
        return False
    for key in schema.schema:
        actual_key = key.schema if isinstance(key, vol.Marker) else key
        if str(actual_key) == key_name:
            return True
    return False


# ---------------------------------------------------------------------------
# Tests: Step 1 - Inverter Setup (user step)
# ---------------------------------------------------------------------------


class TestStepUser:
    """Tests for the inverter setup step (step_id='user')."""

    @pytest.mark.asyncio
    async def test_step_user_shows_form(self):
        """First step shows form with inverter_type and grid_voltage fields."""
        flow = _make_flow()
        result = await flow.async_step_user(user_input=None)

        assert result["type"] == "form"
        assert result["step_id"] == "user"
        assert _schema_has_key(result, CONF_INVERTER_TYPE)
        assert _schema_has_key(result, CONF_GRID_VOLTAGE)

    @pytest.mark.asyncio
    async def test_step_user_accepts_valid_input(self):
        """Valid input advances to the sensors step."""
        flow = _make_flow()
        result = await flow.async_step_user(
            user_input={
                CONF_INVERTER_TYPE: InverterType.HYBRID,
                CONF_GRID_VOLTAGE: 230,
            }
        )

        # Should advance to sensors step
        assert result["type"] == "form"
        assert result["step_id"] == "sensors"
        assert flow.data[CONF_INVERTER_TYPE] == InverterType.HYBRID
        assert flow.data[CONF_GRID_VOLTAGE] == 230

    @pytest.mark.asyncio
    async def test_step_user_rejects_invalid_voltage_low(self):
        """Voltage below 100V is rejected."""
        flow = _make_flow()
        result = await flow.async_step_user(
            user_input={
                CONF_INVERTER_TYPE: InverterType.STANDARD,
                CONF_GRID_VOLTAGE: 50,
            }
        )

        assert result["type"] == "form"
        assert result["step_id"] == "user"
        assert result["errors"][CONF_GRID_VOLTAGE] == "invalid_voltage"

    @pytest.mark.asyncio
    async def test_step_user_rejects_invalid_voltage_high(self):
        """Voltage above 500V is rejected."""
        flow = _make_flow()
        result = await flow.async_step_user(
            user_input={
                CONF_INVERTER_TYPE: InverterType.STANDARD,
                CONF_GRID_VOLTAGE: 600,
            }
        )

        assert result["type"] == "form"
        assert result["step_id"] == "user"
        assert result["errors"][CONF_GRID_VOLTAGE] == "invalid_voltage"

    @pytest.mark.asyncio
    async def test_step_user_standard_inverter(self):
        """Standard inverter type is accepted."""
        flow = _make_flow()
        result = await flow.async_step_user(
            user_input={
                CONF_INVERTER_TYPE: InverterType.STANDARD,
                CONF_GRID_VOLTAGE: 120,
            }
        )

        assert result["step_id"] == "sensors"
        assert flow.data[CONF_INVERTER_TYPE] == InverterType.STANDARD


# ---------------------------------------------------------------------------
# Tests: Step 2 - Sensor Mapping
# ---------------------------------------------------------------------------


class TestStepSensors:
    """Tests for the sensor mapping step (step_id='sensors')."""

    @pytest.mark.asyncio
    async def test_step_sensors_hybrid_shows_battery_fields(self):
        """Hybrid inverter shows battery-specific sensor fields."""
        flow = _make_flow()
        flow.data[CONF_INVERTER_TYPE] = InverterType.HYBRID

        result = await flow.async_step_sensors(user_input=None)

        assert result["type"] == "form"
        assert result["step_id"] == "sensors"
        assert _schema_has_key(result, CONF_PV_POWER)
        assert _schema_has_key(result, CONF_BATTERY_SOC)
        assert _schema_has_key(result, CONF_BATTERY_POWER)
        assert _schema_has_key(result, CONF_BATTERY_CAPACITY)

    @pytest.mark.asyncio
    async def test_step_sensors_standard_hides_battery_fields(self):
        """Standard inverter does not show battery sensor fields."""
        flow = _make_flow()
        flow.data[CONF_INVERTER_TYPE] = InverterType.STANDARD

        result = await flow.async_step_sensors(user_input=None)

        assert result["type"] == "form"
        assert result["step_id"] == "sensors"
        assert _schema_has_key(result, CONF_PV_POWER)
        assert not _schema_has_key(result, CONF_BATTERY_SOC)
        assert not _schema_has_key(result, CONF_BATTERY_POWER)
        assert not _schema_has_key(result, CONF_BATTERY_CAPACITY)

    @pytest.mark.asyncio
    async def test_step_sensors_requires_grid_or_load_sensor(self):
        """At least one of grid_export, import_export, or load_power must be provided."""
        flow = _make_flow()
        flow.data[CONF_INVERTER_TYPE] = InverterType.STANDARD

        result = await flow.async_step_sensors(
            user_input={
                CONF_PV_POWER: "sensor.pv_power",
                # None of grid_export, import_export_power, or load_power provided
            }
        )

        assert result["type"] == "form"
        assert result["step_id"] == "sensors"
        assert result["errors"]["base"] == "no_grid_sensor"

    @pytest.mark.asyncio
    async def test_step_sensors_accepts_pv_plus_load(self):
        """PV + Load Power alone is accepted (coordinator uses pv - load)."""
        flow = _make_flow()
        flow.data[CONF_INVERTER_TYPE] = InverterType.STANDARD

        result = await flow.async_step_sensors(
            user_input={
                CONF_PV_POWER: "sensor.pv_power",
                CONF_LOAD_POWER: "sensor.load_power",
            }
        )

        assert result["step_id"] == "energy"
        assert flow.data[CONF_PV_POWER] == "sensor.pv_power"
        assert flow.data[CONF_LOAD_POWER] == "sensor.load_power"
        assert CONF_GRID_EXPORT not in flow.data
        assert CONF_IMPORT_EXPORT not in flow.data

    @pytest.mark.asyncio
    async def test_step_sensors_accepts_grid_export(self):
        """Grid export sensor is accepted."""
        flow = _make_flow()
        flow.data[CONF_INVERTER_TYPE] = InverterType.STANDARD

        result = await flow.async_step_sensors(
            user_input={
                CONF_PV_POWER: "sensor.pv_power",
                CONF_GRID_EXPORT: "sensor.grid_export",
            }
        )

        assert result["step_id"] == "energy"
        assert flow.data[CONF_PV_POWER] == "sensor.pv_power"
        assert flow.data[CONF_GRID_EXPORT] == "sensor.grid_export"

    @pytest.mark.asyncio
    async def test_step_sensors_accepts_combined_sensor(self):
        """Combined import/export sensor is accepted."""
        flow = _make_flow()
        flow.data[CONF_INVERTER_TYPE] = InverterType.STANDARD

        result = await flow.async_step_sensors(
            user_input={
                CONF_PV_POWER: "sensor.pv_power",
                CONF_IMPORT_EXPORT: "sensor.grid_combined",
            }
        )

        assert result["step_id"] == "energy"
        assert flow.data[CONF_IMPORT_EXPORT] == "sensor.grid_combined"

    @pytest.mark.asyncio
    async def test_step_sensors_hybrid_with_battery(self):
        """Hybrid sensors with battery fields advance to energy step."""
        flow = _make_flow()
        flow.data[CONF_INVERTER_TYPE] = InverterType.HYBRID

        result = await flow.async_step_sensors(
            user_input={
                CONF_PV_POWER: "sensor.pv_power",
                CONF_GRID_EXPORT: "sensor.grid_export",
                CONF_LOAD_POWER: "sensor.load_power",
                CONF_BATTERY_SOC: "sensor.battery_soc",
                CONF_BATTERY_POWER: "sensor.battery_power",
                CONF_BATTERY_CAPACITY: 10.0,
            }
        )

        assert result["step_id"] == "energy"
        assert flow.data[CONF_BATTERY_SOC] == "sensor.battery_soc"
        assert flow.data[CONF_BATTERY_CAPACITY] == 10.0


# ---------------------------------------------------------------------------
# Tests: Step 3 - Energy Pricing
# ---------------------------------------------------------------------------


class TestStepEnergy:
    """Tests for the energy pricing step (step_id='energy')."""

    @pytest.mark.asyncio
    async def test_step_energy_shows_form(self):
        """Energy step shows tariff provider and feed-in tariff."""
        flow = _make_flow()
        flow.data[CONF_INVERTER_TYPE] = InverterType.STANDARD

        result = await flow.async_step_energy(user_input=None)

        assert result["type"] == "form"
        assert result["step_id"] == "energy"
        assert _schema_has_key(result, CONF_TARIFF_PROVIDER)
        assert _schema_has_key(result, CONF_FEED_IN_TARIFF)

    @pytest.mark.asyncio
    async def test_step_energy_none_provider(self):
        """None tariff provider advances without price sensor."""
        flow = _make_flow()
        flow.data[CONF_INVERTER_TYPE] = InverterType.STANDARD

        result = await flow.async_step_energy(
            user_input={
                CONF_TARIFF_PROVIDER: TariffProvider.NONE,
                CONF_FEED_IN_TARIFF: 0.08,
            }
        )

        assert result["step_id"] == "forecast"
        assert flow.data[CONF_TARIFF_PROVIDER] == TariffProvider.NONE
        assert flow.data[CONF_FEED_IN_TARIFF] == 0.08

    @pytest.mark.asyncio
    async def test_step_energy_tibber_provider(self):
        """Tibber provider requires a price sensor entity to advance."""
        flow = _make_flow()
        flow.data[CONF_INVERTER_TYPE] = InverterType.STANDARD

        # Without price sensor, should show error
        result = await flow.async_step_energy(
            user_input={
                CONF_TARIFF_PROVIDER: TariffProvider.TIBBER,
                CONF_FEED_IN_TARIFF: 0.0,
            }
        )
        assert result["type"] == "form"
        assert result["step_id"] == "energy"
        assert result["errors"][CONF_PRICE_SENSOR] == "missing_price_sensor"

        # With price sensor, should advance to forecast
        result = await flow.async_step_energy(
            user_input={
                CONF_TARIFF_PROVIDER: TariffProvider.TIBBER,
                CONF_PRICE_SENSOR: "sensor.tibber_price",
                CONF_FEED_IN_TARIFF: 0.0,
            }
        )

        assert result["step_id"] == "forecast"
        assert flow.data[CONF_TARIFF_PROVIDER] == TariffProvider.TIBBER
        assert flow.data[CONF_PRICE_SENSOR] == "sensor.tibber_price"

    @pytest.mark.asyncio
    async def test_step_energy_generic_requires_sensor(self):
        """Generic tariff provider requires a price sensor entity."""
        flow = _make_flow()
        flow.data[CONF_INVERTER_TYPE] = InverterType.STANDARD

        result = await flow.async_step_energy(
            user_input={
                CONF_TARIFF_PROVIDER: TariffProvider.GENERIC,
                CONF_FEED_IN_TARIFF: 0.0,
                # No price_sensor provided
            }
        )

        assert result["type"] == "form"
        assert result["step_id"] == "energy"
        assert result["errors"][CONF_PRICE_SENSOR] == "missing_price_sensor"

    @pytest.mark.asyncio
    async def test_step_energy_generic_with_sensor(self):
        """Generic provider with price sensor advances."""
        flow = _make_flow()
        flow.data[CONF_INVERTER_TYPE] = InverterType.STANDARD

        result = await flow.async_step_energy(
            user_input={
                CONF_TARIFF_PROVIDER: TariffProvider.GENERIC,
                CONF_PRICE_SENSOR: "sensor.electricity_price",
                CONF_FEED_IN_TARIFF: 0.05,
            }
        )

        assert result["step_id"] == "forecast"
        assert flow.data[CONF_PRICE_SENSOR] == "sensor.electricity_price"


# ---------------------------------------------------------------------------
# Tests: Step 4 - Solar Forecast
# ---------------------------------------------------------------------------


class TestStepForecast:
    """Tests for the solar forecast step (step_id='forecast')."""

    @pytest.mark.asyncio
    async def test_step_forecast_shows_form(self):
        """Forecast step shows provider selector."""
        flow = _make_flow()
        flow.data[CONF_INVERTER_TYPE] = InverterType.STANDARD

        result = await flow.async_step_forecast(user_input=None)

        assert result["type"] == "form"
        assert result["step_id"] == "forecast"
        assert _schema_has_key(result, CONF_FORECAST_PROVIDER)

    @pytest.mark.asyncio
    async def test_step_forecast_none_standard_goes_to_settings(self):
        """None forecast with standard inverter skips battery, goes to settings."""
        flow = _make_flow()
        flow.data[CONF_INVERTER_TYPE] = InverterType.STANDARD

        result = await flow.async_step_forecast(
            user_input={CONF_FORECAST_PROVIDER: ForecastProvider.NONE}
        )

        assert result["step_id"] == "settings"

    @pytest.mark.asyncio
    async def test_step_forecast_none_hybrid_goes_to_battery(self):
        """None forecast with hybrid inverter goes to battery step."""
        flow = _make_flow()
        flow.data[CONF_INVERTER_TYPE] = InverterType.HYBRID

        result = await flow.async_step_forecast(
            user_input={CONF_FORECAST_PROVIDER: ForecastProvider.NONE}
        )

        assert result["step_id"] == "battery"

    @pytest.mark.asyncio
    async def test_step_forecast_solcast(self):
        """Solcast provider requires a forecast sensor entity to advance."""
        flow = _make_flow()
        flow.data[CONF_INVERTER_TYPE] = InverterType.STANDARD

        # Without forecast sensor, should show error
        result = await flow.async_step_forecast(
            user_input={CONF_FORECAST_PROVIDER: ForecastProvider.SOLCAST}
        )
        assert result["type"] == "form"
        assert result["step_id"] == "forecast"
        assert result["errors"][CONF_FORECAST_SENSOR] == "missing_forecast_sensor"

        # With forecast sensor, should advance to settings
        result = await flow.async_step_forecast(
            user_input={
                CONF_FORECAST_PROVIDER: ForecastProvider.SOLCAST,
                CONF_FORECAST_SENSOR: "sensor.solcast_pv_forecast_forecast_remaining_today",
            }
        )

        assert result["step_id"] == "settings"
        assert flow.data[CONF_FORECAST_PROVIDER] == ForecastProvider.SOLCAST
        assert flow.data[CONF_FORECAST_SENSOR] == "sensor.solcast_pv_forecast_forecast_remaining_today"

    @pytest.mark.asyncio
    async def test_step_forecast_generic_requires_sensor(self):
        """Generic forecast provider requires a forecast sensor."""
        flow = _make_flow()
        flow.data[CONF_INVERTER_TYPE] = InverterType.STANDARD

        result = await flow.async_step_forecast(
            user_input={CONF_FORECAST_PROVIDER: ForecastProvider.GENERIC}
        )

        assert result["type"] == "form"
        assert result["step_id"] == "forecast"
        assert (
            result["errors"][CONF_FORECAST_SENSOR] == "missing_forecast_sensor"
        )

    @pytest.mark.asyncio
    async def test_step_forecast_generic_with_sensor(self):
        """Generic provider with forecast sensor advances."""
        flow = _make_flow()
        flow.data[CONF_INVERTER_TYPE] = InverterType.STANDARD

        result = await flow.async_step_forecast(
            user_input={
                CONF_FORECAST_PROVIDER: ForecastProvider.GENERIC,
                CONF_FORECAST_SENSOR: "sensor.solar_forecast",
            }
        )

        assert result["step_id"] == "settings"
        assert flow.data[CONF_FORECAST_SENSOR] == "sensor.solar_forecast"


# ---------------------------------------------------------------------------
# Tests: Step 5 - Battery Strategy
# ---------------------------------------------------------------------------


class TestStepBattery:
    """Tests for the battery strategy step (step_id='battery')."""

    @pytest.mark.asyncio
    async def test_step_battery_shows_form(self):
        """Battery step shows strategy, target SoC, time, and grid charging."""
        flow = _make_flow()
        flow.data[CONF_INVERTER_TYPE] = InverterType.HYBRID

        result = await flow.async_step_battery(user_input=None)

        assert result["type"] == "form"
        assert result["step_id"] == "battery"
        assert _schema_has_key(result, CONF_BATTERY_STRATEGY)
        assert _schema_has_key(result, CONF_BATTERY_TARGET_SOC)
        assert _schema_has_key(result, CONF_BATTERY_TARGET_TIME)
        assert _schema_has_key(result, CONF_ALLOW_GRID_CHARGING)

    @pytest.mark.asyncio
    async def test_step_battery_accepts_valid_input(self):
        """Valid battery configuration advances to settings."""
        flow = _make_flow()
        flow.data[CONF_INVERTER_TYPE] = InverterType.HYBRID

        result = await flow.async_step_battery(
            user_input={
                CONF_BATTERY_STRATEGY: BatteryStrategy.BALANCED,
                CONF_BATTERY_TARGET_SOC: 80,
                CONF_BATTERY_TARGET_TIME: "16:00",
                CONF_ALLOW_GRID_CHARGING: False,
            }
        )

        assert result["step_id"] == "settings"
        assert flow.data[CONF_BATTERY_STRATEGY] == BatteryStrategy.BALANCED
        assert flow.data[CONF_BATTERY_TARGET_SOC] == 80

    @pytest.mark.asyncio
    async def test_step_battery_with_discharge_entity(self):
        """Battery step accepts optional discharge entity."""
        flow = _make_flow()
        flow.data[CONF_INVERTER_TYPE] = InverterType.HYBRID

        result = await flow.async_step_battery(
            user_input={
                CONF_BATTERY_STRATEGY: BatteryStrategy.BATTERY_FIRST,
                CONF_BATTERY_TARGET_SOC: 90,
                CONF_BATTERY_TARGET_TIME: "14:00",
                CONF_ALLOW_GRID_CHARGING: True,
                CONF_BATTERY_MAX_DISCHARGE_ENTITY: "number.battery_discharge",
                CONF_BATTERY_MAX_DISCHARGE_DEFAULT: 5000,
            }
        )

        assert result["step_id"] == "settings"
        assert flow.data[CONF_BATTERY_MAX_DISCHARGE_ENTITY] == "number.battery_discharge"
        assert flow.data[CONF_BATTERY_MAX_DISCHARGE_DEFAULT] == 5000

    @pytest.mark.asyncio
    async def test_step_battery_invalid_soc(self):
        """SOC outside 0-100 range is rejected."""
        flow = _make_flow()
        flow.data[CONF_INVERTER_TYPE] = InverterType.HYBRID

        result = await flow.async_step_battery(
            user_input={
                CONF_BATTERY_STRATEGY: BatteryStrategy.BALANCED,
                CONF_BATTERY_TARGET_SOC: 150,
                CONF_BATTERY_TARGET_TIME: "16:00",
                CONF_ALLOW_GRID_CHARGING: False,
            }
        )

        assert result["type"] == "form"
        assert result["step_id"] == "battery"
        assert result["errors"][CONF_BATTERY_TARGET_SOC] == "invalid_soc"


# ---------------------------------------------------------------------------
# Tests: Step 6 - Global Settings
# ---------------------------------------------------------------------------


class TestStepSettings:
    """Tests for the global settings step (step_id='settings')."""

    @pytest.mark.asyncio
    async def test_step_settings_shows_form(self):
        """Settings step shows export limit and interval selectors."""
        flow = _make_flow()

        result = await flow.async_step_settings(user_input=None)

        assert result["type"] == "form"
        assert result["step_id"] == "settings"
        assert _schema_has_key(result, CONF_EXPORT_LIMIT)
        assert _schema_has_key(result, CONF_CONTROLLER_INTERVAL)
        assert _schema_has_key(result, CONF_PLANNER_INTERVAL)

    @pytest.mark.asyncio
    async def test_step_settings_creates_entry(self):
        """Valid settings input creates the config entry."""
        flow = _make_flow()
        flow.data = {
            CONF_INVERTER_TYPE: InverterType.STANDARD,
            CONF_GRID_VOLTAGE: 230,
            CONF_PV_POWER: "sensor.pv_power",
            CONF_GRID_EXPORT: "sensor.grid_export",
        }

        result = await flow.async_step_settings(
            user_input={
                CONF_EXPORT_LIMIT: 0,
                CONF_CONTROLLER_INTERVAL: "30",
                CONF_PLANNER_INTERVAL: "900",
            }
        )

        assert result["type"] == "create_entry"
        assert result["title"] == "PV Excess Control"
        assert result["data"][CONF_INVERTER_TYPE] == InverterType.STANDARD
        assert result["data"][CONF_CONTROLLER_INTERVAL] == 30
        assert result["data"][CONF_PLANNER_INTERVAL] == 900

    @pytest.mark.asyncio
    async def test_step_settings_converts_intervals_to_int(self):
        """String interval values are converted to integers."""
        flow = _make_flow()
        flow.data = {CONF_INVERTER_TYPE: InverterType.STANDARD}

        result = await flow.async_step_settings(
            user_input={
                CONF_EXPORT_LIMIT: 5000,
                CONF_CONTROLLER_INTERVAL: "60",
                CONF_PLANNER_INTERVAL: "1800",
            }
        )

        assert result["type"] == "create_entry"
        assert result["data"][CONF_CONTROLLER_INTERVAL] == 60
        assert result["data"][CONF_PLANNER_INTERVAL] == 1800
        assert result["data"][CONF_EXPORT_LIMIT] == 5000


# ---------------------------------------------------------------------------
# Tests: Full flow - Hybrid
# ---------------------------------------------------------------------------


class TestFullFlowHybrid:
    """Test complete config flow for a hybrid inverter setup."""

    @pytest.mark.asyncio
    async def test_full_flow_hybrid(self):
        """Full flow for hybrid inverter goes through all 6 steps."""
        flow = _make_flow()

        # Step 1: Inverter setup
        result = await flow.async_step_user(
            user_input={
                CONF_INVERTER_TYPE: InverterType.HYBRID,
                CONF_GRID_VOLTAGE: 230,
            }
        )
        assert result["step_id"] == "sensors"

        # Step 2: Sensor mapping (with battery fields)
        result = await flow.async_step_sensors(
            user_input={
                CONF_PV_POWER: "sensor.pv_power",
                CONF_GRID_EXPORT: "sensor.grid_export",
                CONF_LOAD_POWER: "sensor.load_power",
                CONF_BATTERY_SOC: "sensor.battery_soc",
                CONF_BATTERY_POWER: "sensor.battery_power",
                CONF_BATTERY_CAPACITY: 10.0,
            }
        )
        assert result["step_id"] == "energy"

        # Step 3: Energy pricing
        result = await flow.async_step_energy(
            user_input={
                CONF_TARIFF_PROVIDER: TariffProvider.TIBBER,
                CONF_PRICE_SENSOR: "sensor.tibber_price",
                CONF_FEED_IN_TARIFF: 0.08,
                CONF_CHEAP_PRICE_THRESHOLD: 0.15,
                CONF_BATTERY_CHARGE_PRICE_THRESHOLD: 0.10,
            }
        )
        assert result["step_id"] == "forecast"

        # Step 4: Solar forecast
        result = await flow.async_step_forecast(
            user_input={
                CONF_FORECAST_PROVIDER: ForecastProvider.SOLCAST,
                CONF_FORECAST_SENSOR: "sensor.solcast_pv_forecast_forecast_remaining_today",
            }
        )
        assert result["step_id"] == "battery"

        # Step 5: Battery strategy
        result = await flow.async_step_battery(
            user_input={
                CONF_BATTERY_STRATEGY: BatteryStrategy.BALANCED,
                CONF_BATTERY_TARGET_SOC: 80,
                CONF_BATTERY_TARGET_TIME: "16:00",
                CONF_ALLOW_GRID_CHARGING: False,
            }
        )
        assert result["step_id"] == "settings"

        # Step 6: Global settings
        result = await flow.async_step_settings(
            user_input={
                CONF_EXPORT_LIMIT: 0,
                CONF_CONTROLLER_INTERVAL: "30",
                CONF_PLANNER_INTERVAL: "900",
            }
        )

        assert result["type"] == "create_entry"
        assert result["title"] == "PV Excess Control"

        # Verify all data is present
        data = result["data"]
        assert data[CONF_INVERTER_TYPE] == InverterType.HYBRID
        assert data[CONF_GRID_VOLTAGE] == 230
        assert data[CONF_PV_POWER] == "sensor.pv_power"
        assert data[CONF_GRID_EXPORT] == "sensor.grid_export"
        assert data[CONF_LOAD_POWER] == "sensor.load_power"
        assert data[CONF_BATTERY_SOC] == "sensor.battery_soc"
        assert data[CONF_BATTERY_POWER] == "sensor.battery_power"
        assert data[CONF_BATTERY_CAPACITY] == 10.0
        assert data[CONF_TARIFF_PROVIDER] == TariffProvider.TIBBER
        assert data[CONF_FEED_IN_TARIFF] == 0.08
        assert data[CONF_CHEAP_PRICE_THRESHOLD] == 0.15
        assert data[CONF_BATTERY_CHARGE_PRICE_THRESHOLD] == 0.10
        assert data[CONF_FORECAST_PROVIDER] == ForecastProvider.SOLCAST
        assert data[CONF_BATTERY_STRATEGY] == BatteryStrategy.BALANCED
        assert data[CONF_BATTERY_TARGET_SOC] == 80
        assert data[CONF_BATTERY_TARGET_TIME] == "16:00"
        assert data[CONF_ALLOW_GRID_CHARGING] is False
        assert data[CONF_EXPORT_LIMIT] == 0
        assert data[CONF_CONTROLLER_INTERVAL] == 30
        assert data[CONF_PLANNER_INTERVAL] == 900


# ---------------------------------------------------------------------------
# Tests: Full flow - Standard
# ---------------------------------------------------------------------------


class TestFullFlowStandard:
    """Test complete config flow for a standard inverter setup."""

    @pytest.mark.asyncio
    async def test_full_flow_standard(self):
        """Full flow for standard inverter skips battery step (5 steps total)."""
        flow = _make_flow()

        # Step 1: Inverter setup
        result = await flow.async_step_user(
            user_input={
                CONF_INVERTER_TYPE: InverterType.STANDARD,
                CONF_GRID_VOLTAGE: 120,
            }
        )
        assert result["step_id"] == "sensors"

        # Step 2: Sensor mapping (no battery fields)
        result = await flow.async_step_sensors(
            user_input={
                CONF_PV_POWER: "sensor.pv_power",
                CONF_IMPORT_EXPORT: "sensor.grid_combined",
            }
        )
        assert result["step_id"] == "energy"

        # Step 3: Energy pricing (no tariff)
        result = await flow.async_step_energy(
            user_input={
                CONF_TARIFF_PROVIDER: TariffProvider.NONE,
                CONF_FEED_IN_TARIFF: 0.0,
            }
        )
        assert result["step_id"] == "forecast"

        # Step 4: Solar forecast (none)
        result = await flow.async_step_forecast(
            user_input={CONF_FORECAST_PROVIDER: ForecastProvider.NONE}
        )
        # Standard inverter: should skip battery and go to settings
        assert result["step_id"] == "settings"

        # Step 5 (final): Global settings
        result = await flow.async_step_settings(
            user_input={
                CONF_EXPORT_LIMIT: 10000,
                CONF_CONTROLLER_INTERVAL: "15",
                CONF_PLANNER_INTERVAL: "1800",
            }
        )

        assert result["type"] == "create_entry"
        assert result["title"] == "PV Excess Control"

        data = result["data"]
        assert data[CONF_INVERTER_TYPE] == InverterType.STANDARD
        assert data[CONF_GRID_VOLTAGE] == 120
        assert data[CONF_PV_POWER] == "sensor.pv_power"
        assert data[CONF_IMPORT_EXPORT] == "sensor.grid_combined"
        assert data[CONF_TARIFF_PROVIDER] == TariffProvider.NONE
        assert data[CONF_FORECAST_PROVIDER] == ForecastProvider.NONE
        assert data[CONF_EXPORT_LIMIT] == 10000
        assert data[CONF_CONTROLLER_INTERVAL] == 15
        assert data[CONF_PLANNER_INTERVAL] == 1800
        # Battery fields should NOT be present
        assert CONF_BATTERY_SOC not in data
        assert CONF_BATTERY_STRATEGY not in data


# ---------------------------------------------------------------------------
# Tests: Validation errors
# ---------------------------------------------------------------------------


class TestValidationErrors:
    """Tests for validation error handling across steps."""

    @pytest.mark.asyncio
    async def test_voltage_boundary_100(self):
        """100V is the minimum valid voltage."""
        flow = _make_flow()
        result = await flow.async_step_user(
            user_input={
                CONF_INVERTER_TYPE: InverterType.STANDARD,
                CONF_GRID_VOLTAGE: 100,
            }
        )
        # 100 is valid (boundary)
        assert result["step_id"] == "sensors"

    @pytest.mark.asyncio
    async def test_voltage_boundary_500(self):
        """500V is the maximum valid voltage."""
        flow = _make_flow()
        result = await flow.async_step_user(
            user_input={
                CONF_INVERTER_TYPE: InverterType.STANDARD,
                CONF_GRID_VOLTAGE: 500,
            }
        )
        # 500 is valid (boundary)
        assert result["step_id"] == "sensors"

    @pytest.mark.asyncio
    async def test_voltage_boundary_99_invalid(self):
        """99V is below minimum."""
        flow = _make_flow()
        result = await flow.async_step_user(
            user_input={
                CONF_INVERTER_TYPE: InverterType.STANDARD,
                CONF_GRID_VOLTAGE: 99,
            }
        )
        assert result["errors"][CONF_GRID_VOLTAGE] == "invalid_voltage"

    @pytest.mark.asyncio
    async def test_voltage_boundary_501_invalid(self):
        """501V is above maximum."""
        flow = _make_flow()
        result = await flow.async_step_user(
            user_input={
                CONF_INVERTER_TYPE: InverterType.STANDARD,
                CONF_GRID_VOLTAGE: 501,
            }
        )
        assert result["errors"][CONF_GRID_VOLTAGE] == "invalid_voltage"

    @pytest.mark.asyncio
    async def test_soc_boundary_0(self):
        """0% SoC is valid."""
        flow = _make_flow()
        flow.data[CONF_INVERTER_TYPE] = InverterType.HYBRID

        result = await flow.async_step_battery(
            user_input={
                CONF_BATTERY_STRATEGY: BatteryStrategy.BALANCED,
                CONF_BATTERY_TARGET_SOC: 0,
                CONF_BATTERY_TARGET_TIME: "16:00",
                CONF_ALLOW_GRID_CHARGING: False,
            }
        )
        assert result["step_id"] == "settings"

    @pytest.mark.asyncio
    async def test_soc_boundary_100(self):
        """100% SoC is valid."""
        flow = _make_flow()
        flow.data[CONF_INVERTER_TYPE] = InverterType.HYBRID

        result = await flow.async_step_battery(
            user_input={
                CONF_BATTERY_STRATEGY: BatteryStrategy.BALANCED,
                CONF_BATTERY_TARGET_SOC: 100,
                CONF_BATTERY_TARGET_TIME: "16:00",
                CONF_ALLOW_GRID_CHARGING: False,
            }
        )
        assert result["step_id"] == "settings"

    @pytest.mark.asyncio
    async def test_soc_negative_invalid(self):
        """Negative SoC is invalid."""
        flow = _make_flow()
        flow.data[CONF_INVERTER_TYPE] = InverterType.HYBRID

        result = await flow.async_step_battery(
            user_input={
                CONF_BATTERY_STRATEGY: BatteryStrategy.BALANCED,
                CONF_BATTERY_TARGET_SOC: -1,
                CONF_BATTERY_TARGET_TIME: "16:00",
                CONF_ALLOW_GRID_CHARGING: False,
            }
        )
        assert result["errors"][CONF_BATTERY_TARGET_SOC] == "invalid_soc"

    @pytest.mark.asyncio
    async def test_sensors_both_grid_sensors_accepted(self):
        """Providing both grid export and combined sensor is accepted."""
        flow = _make_flow()
        flow.data[CONF_INVERTER_TYPE] = InverterType.STANDARD

        result = await flow.async_step_sensors(
            user_input={
                CONF_PV_POWER: "sensor.pv_power",
                CONF_GRID_EXPORT: "sensor.grid_export",
                CONF_IMPORT_EXPORT: "sensor.grid_combined",
            }
        )
        # Both provided is fine - at least one is present
        assert result["step_id"] == "energy"


# ---------------------------------------------------------------------------
# Tests: Schema helpers
# ---------------------------------------------------------------------------


class TestSchemaHelpers:
    """Tests for schema builder helper functions."""

    def test_sensor_schema_standard(self):
        """Standard schema does not include battery fields."""
        schema = _sensor_schema(is_hybrid=False)
        key_names = [
            k.schema if isinstance(k, vol.Marker) else k
            for k in schema.schema
        ]
        assert CONF_PV_POWER in key_names
        assert CONF_GRID_EXPORT in key_names
        assert CONF_IMPORT_EXPORT in key_names
        assert CONF_LOAD_POWER in key_names
        assert CONF_BATTERY_SOC not in key_names
        assert CONF_BATTERY_POWER not in key_names
        assert CONF_BATTERY_CAPACITY not in key_names

    def test_sensor_schema_hybrid(self):
        """Hybrid schema includes battery fields."""
        schema = _sensor_schema(is_hybrid=True)
        key_names = [
            k.schema if isinstance(k, vol.Marker) else k
            for k in schema.schema
        ]
        assert CONF_PV_POWER in key_names
        assert CONF_BATTERY_SOC in key_names
        assert CONF_BATTERY_POWER in key_names
        assert CONF_BATTERY_CAPACITY in key_names

    def test_energy_schema_none_provider(self):
        """None provider schema has tariff_provider, thresholds, and feed_in_tariff."""
        schema = _energy_schema(TariffProvider.NONE)
        key_names = [
            k.schema if isinstance(k, vol.Marker) else k
            for k in schema.schema
        ]
        assert CONF_TARIFF_PROVIDER in key_names
        assert CONF_FEED_IN_TARIFF in key_names
        # Threshold fields are always shown regardless of provider
        assert CONF_CHEAP_PRICE_THRESHOLD in key_names
        assert CONF_BATTERY_CHARGE_PRICE_THRESHOLD in key_names
        # No price sensor field for NONE
        assert CONF_PRICE_SENSOR not in key_names

    def test_energy_schema_generic_provider(self):
        """Generic provider schema includes price sensor and thresholds."""
        schema = _energy_schema(TariffProvider.GENERIC)
        key_names = [
            k.schema if isinstance(k, vol.Marker) else k
            for k in schema.schema
        ]
        assert CONF_TARIFF_PROVIDER in key_names
        assert CONF_PRICE_SENSOR in key_names
        assert CONF_CHEAP_PRICE_THRESHOLD in key_names
        assert CONF_BATTERY_CHARGE_PRICE_THRESHOLD in key_names
        assert CONF_FEED_IN_TARIFF in key_names

    def test_energy_schema_tibber_provider(self):
        """Tibber provider schema has thresholds and price sensor."""
        schema = _energy_schema(TariffProvider.TIBBER)
        key_names = [
            k.schema if isinstance(k, vol.Marker) else k
            for k in schema.schema
        ]
        assert CONF_TARIFF_PROVIDER in key_names
        assert CONF_PRICE_SENSOR in key_names
        assert CONF_CHEAP_PRICE_THRESHOLD in key_names

    def test_forecast_schema_none(self):
        """None forecast schema has provider but no sensor."""
        schema = _forecast_schema(ForecastProvider.NONE)
        key_names = [
            k.schema if isinstance(k, vol.Marker) else k
            for k in schema.schema
        ]
        assert CONF_FORECAST_PROVIDER in key_names
        assert CONF_FORECAST_SENSOR not in key_names

    def test_forecast_schema_generic(self):
        """Generic forecast schema has provider and sensor."""
        schema = _forecast_schema(ForecastProvider.GENERIC)
        key_names = [
            k.schema if isinstance(k, vol.Marker) else k
            for k in schema.schema
        ]
        assert CONF_FORECAST_PROVIDER in key_names
        assert CONF_FORECAST_SENSOR in key_names

    def test_battery_schema_keys(self):
        """Battery schema has all expected keys."""
        key_names = [
            k.schema if isinstance(k, vol.Marker) else k
            for k in BATTERY_SCHEMA.schema
        ]
        assert CONF_BATTERY_STRATEGY in key_names
        assert CONF_BATTERY_TARGET_SOC in key_names
        assert CONF_BATTERY_TARGET_TIME in key_names
        assert CONF_ALLOW_GRID_CHARGING in key_names
        assert CONF_BATTERY_MAX_DISCHARGE_ENTITY in key_names
        assert CONF_BATTERY_MAX_DISCHARGE_DEFAULT in key_names

    def test_settings_schema_keys(self):
        """Settings schema has all expected keys."""
        key_names = [
            k.schema if isinstance(k, vol.Marker) else k
            for k in SETTINGS_SCHEMA.schema
        ]
        assert CONF_EXPORT_LIMIT in key_names
        assert CONF_CONTROLLER_INTERVAL in key_names
        assert CONF_PLANNER_INTERVAL in key_names


# ---------------------------------------------------------------------------
# Tests: Options flow
# ---------------------------------------------------------------------------


class TestOptionsFlow:
    """Tests for the options flow handler."""

    def test_async_get_options_flow(self):
        """Config flow returns an options flow handler."""
        from custom_components.pv_excess_control.config_flow import (
            PvExcessControlOptionsFlow,
        )

        entry = MagicMock()
        entry.data = {
            CONF_INVERTER_TYPE: InverterType.HYBRID,
            CONF_GRID_VOLTAGE: 230,
        }

        handler = PvExcessControlConfigFlow.async_get_options_flow(entry)
        assert isinstance(handler, PvExcessControlOptionsFlow)

    @pytest.mark.asyncio
    async def test_options_flow_init_redirects_to_user(self):
        """Options flow init step redirects to user step."""
        from custom_components.pv_excess_control.config_flow import (
            PvExcessControlOptionsFlow,
        )

        entry = MagicMock()
        entry.data = {
            CONF_INVERTER_TYPE: InverterType.HYBRID,
            CONF_GRID_VOLTAGE: 230,
        }

        options_flow = PvExcessControlOptionsFlow()
        options_flow.hass = MagicMock()
        options_flow.handler = DOMAIN
        options_flow.flow_id = "test_options_flow"
        type(options_flow).config_entry = property(lambda self: entry)
        options_flow.data = dict(entry.data)

        result = await options_flow.async_step_init(user_input=None)

        assert result["type"] == "form"
        assert result["step_id"] == "user"


# ---------------------------------------------------------------------------
# Tests: Data accumulation
# ---------------------------------------------------------------------------


class TestDataAccumulation:
    """Tests that data is correctly accumulated across steps."""

    @pytest.mark.asyncio
    async def test_data_persists_across_steps(self):
        """Data from each step is preserved in subsequent steps."""
        flow = _make_flow()

        # After step 1
        await flow.async_step_user(
            user_input={
                CONF_INVERTER_TYPE: InverterType.STANDARD,
                CONF_GRID_VOLTAGE: 230,
            }
        )
        assert CONF_INVERTER_TYPE in flow.data
        assert CONF_GRID_VOLTAGE in flow.data

        # After step 2
        await flow.async_step_sensors(
            user_input={
                CONF_PV_POWER: "sensor.pv_power",
                CONF_GRID_EXPORT: "sensor.grid_export",
            }
        )
        # Previous data still present
        assert flow.data[CONF_INVERTER_TYPE] == InverterType.STANDARD
        assert flow.data[CONF_GRID_VOLTAGE] == 230
        # New data added
        assert flow.data[CONF_PV_POWER] == "sensor.pv_power"

        # After step 3
        await flow.async_step_energy(
            user_input={
                CONF_TARIFF_PROVIDER: TariffProvider.NONE,
                CONF_FEED_IN_TARIFF: 0.0,
            }
        )
        # All previous data still present
        assert flow.data[CONF_INVERTER_TYPE] == InverterType.STANDARD
        assert flow.data[CONF_PV_POWER] == "sensor.pv_power"
        assert flow.data[CONF_TARIFF_PROVIDER] == TariffProvider.NONE

    @pytest.mark.asyncio
    async def test_flow_initializes_empty_data(self):
        """Flow starts with empty data dict."""
        flow = _make_flow()
        assert flow.data == {}


# ---------------------------------------------------------------------------
# Tests for _validate_battery_section
# ---------------------------------------------------------------------------

from custom_components.pv_excess_control.const import (
    CONF_AUTO_BATTERY_GRID_CHARGE,
    CONF_BATTERY_GRID_CHARGE_POWER_W,
    CONF_INVERTER_FORCE_CHARGE_ENABLE_ENTITY,
    CONF_INVERTER_FORCE_CHARGE_ENABLE_ENGAGE_VALUE,
    CONF_INVERTER_FORCE_CHARGE_ENABLE_DISENGAGE_VALUE,
    CONF_INVERTER_FORCE_CHARGE_MODE_ENTITY,
    CONF_INVERTER_FORCE_CHARGE_MODE_ENGAGE_VALUE,
    CONF_INVERTER_FORCE_CHARGE_MODE_DISENGAGE_VALUE,
)


def test_auto_battery_grid_charge_requires_enable_entity_and_power_w():
    from custom_components.pv_excess_control.config_flow import _validate_battery_section
    bad = {
        CONF_AUTO_BATTERY_GRID_CHARGE: True,
        # missing CONF_INVERTER_FORCE_CHARGE_ENABLE_ENTITY
        CONF_BATTERY_GRID_CHARGE_POWER_W: 5000.0,
    }
    with pytest.raises(vol.Invalid):
        _validate_battery_section(bad)


def test_inverter_force_charge_engage_value_required_when_entity_set():
    from custom_components.pv_excess_control.config_flow import _validate_battery_section
    bad = {
        CONF_INVERTER_FORCE_CHARGE_ENABLE_ENTITY: "input_select.cmd",
        # missing engage/disengage values
    }
    with pytest.raises(vol.Invalid):
        _validate_battery_section(bad)


def test_inverter_force_charge_mode_values_required_when_mode_entity_set():
    from custom_components.pv_excess_control.config_flow import _validate_battery_section
    bad = {
        CONF_INVERTER_FORCE_CHARGE_ENABLE_ENTITY: "input_select.cmd",
        CONF_INVERTER_FORCE_CHARGE_ENABLE_ENGAGE_VALUE: "Forced charge",
        CONF_INVERTER_FORCE_CHARGE_ENABLE_DISENGAGE_VALUE: "Stop",
        CONF_INVERTER_FORCE_CHARGE_MODE_ENTITY: "input_select.mode",
        # missing mode engage/disengage values
    }
    with pytest.raises(vol.Invalid):
        _validate_battery_section(bad)


def test_validate_battery_section_passes_for_complete_three_step_config():
    from custom_components.pv_excess_control.config_flow import _validate_battery_section
    good = {
        CONF_AUTO_BATTERY_GRID_CHARGE: True,
        CONF_BATTERY_GRID_CHARGE_POWER_W: 5000.0,
        CONF_INVERTER_FORCE_CHARGE_ENABLE_ENTITY: "input_select.cmd",
        CONF_INVERTER_FORCE_CHARGE_ENABLE_ENGAGE_VALUE: "Forced charge",
        CONF_INVERTER_FORCE_CHARGE_ENABLE_DISENGAGE_VALUE: "Stop",
        CONF_INVERTER_FORCE_CHARGE_MODE_ENTITY: "input_select.mode",
        CONF_INVERTER_FORCE_CHARGE_MODE_ENGAGE_VALUE: "Forced",
        CONF_INVERTER_FORCE_CHARGE_MODE_DISENGAGE_VALUE: "Self",
    }
    # Should NOT raise
    _validate_battery_section(good)


# ---------------------------------------------------------------------------
# Tests: _validate_dynamic_charge_section
# ---------------------------------------------------------------------------

def _base_battery_input(extra: dict | None = None) -> dict:
    """Minimal valid battery-step input (dynamic charge disabled by default)."""
    base = {
        CONF_BATTERY_STRATEGY: BatteryStrategy.BALANCED,
        CONF_BATTERY_TARGET_SOC: 80,
        CONF_BATTERY_TARGET_TIME: "16:00",
        CONF_ALLOW_GRID_CHARGING: False,
        CONF_DYNAMIC_BATTERY_CHARGE_ENABLED: False,
    }
    if extra:
        base.update(extra)
    return base


class TestDynamicChargeValidation:
    """Tests for _validate_dynamic_charge_section via async_step_battery."""

    @pytest.mark.asyncio
    async def test_dynamic_charge_disabled_with_blanks_passes(self):
        """Feature off, blank optional fields → form accepted."""
        flow = _make_flow()
        flow.data[CONF_INVERTER_TYPE] = InverterType.HYBRID
        result = await flow.async_step_battery(
            user_input=_base_battery_input({
                CONF_DYNAMIC_BATTERY_CHARGE_ENABLED: False,
            })
        )
        assert result["step_id"] == "settings"
        assert not result.get("errors")

    @pytest.mark.asyncio
    async def test_dynamic_charge_enabled_missing_inverter_entity_fails(self):
        """Feature on, no inverter entity → error redisplays."""
        flow = _make_flow()
        flow.data[CONF_INVERTER_TYPE] = InverterType.HYBRID
        result = await flow.async_step_battery(
            user_input=_base_battery_input({
                CONF_DYNAMIC_BATTERY_CHARGE_ENABLED: True,
                # no CONF_INVERTER_BATTERY_MAX_CHARGE_POWER_ENTITY
                CONF_BATTERY_MAX_CHARGE_POWER_W: 5000,
                CONF_BATTERY_TRICKLE_CHARGE_POWER_W: 100,
                CONF_EXPORT_LIMIT: 6000,
            })
        )
        assert result["type"] == "form"
        assert result["step_id"] == "battery"
        assert result["errors"]["base"] == "missing_inverter_battery_charge_entity"

    @pytest.mark.asyncio
    async def test_dynamic_charge_max_below_trickle_fails(self):
        """max_w=100, trickle=200 → battery_max_charge_below_trickle error."""
        flow = _make_flow()
        flow.data[CONF_INVERTER_TYPE] = InverterType.HYBRID
        result = await flow.async_step_battery(
            user_input=_base_battery_input({
                CONF_DYNAMIC_BATTERY_CHARGE_ENABLED: True,
                CONF_INVERTER_BATTERY_MAX_CHARGE_POWER_ENTITY: "number.batt_charge",
                CONF_BATTERY_MAX_CHARGE_POWER_W: 100,
                CONF_BATTERY_TRICKLE_CHARGE_POWER_W: 200,
                CONF_EXPORT_LIMIT: 6000,
            })
        )
        assert result["type"] == "form"
        assert result["step_id"] == "battery"
        assert result["errors"]["base"] == "battery_max_charge_below_trickle"

    @pytest.mark.asyncio
    async def test_dynamic_charge_missing_max_power_fails(self):
        """Feature on, inverter entity set but no max power → error."""
        flow = _make_flow()
        flow.data[CONF_INVERTER_TYPE] = InverterType.HYBRID
        result = await flow.async_step_battery(
            user_input=_base_battery_input({
                CONF_DYNAMIC_BATTERY_CHARGE_ENABLED: True,
                CONF_INVERTER_BATTERY_MAX_CHARGE_POWER_ENTITY: "number.batt_charge",
                # no CONF_BATTERY_MAX_CHARGE_POWER_W
                CONF_BATTERY_TRICKLE_CHARGE_POWER_W: 100,
                CONF_EXPORT_LIMIT: 6000,
            })
        )
        assert result["type"] == "form"
        assert result["step_id"] == "battery"
        assert result["errors"]["base"] == "missing_battery_max_charge_power"

    @pytest.mark.asyncio
    async def test_dynamic_charge_missing_export_limit_fails(self):
        """Feature on, all present but export_limit=0 → error."""
        flow = _make_flow()
        flow.data[CONF_INVERTER_TYPE] = InverterType.HYBRID
        result = await flow.async_step_battery(
            user_input=_base_battery_input({
                CONF_DYNAMIC_BATTERY_CHARGE_ENABLED: True,
                CONF_INVERTER_BATTERY_MAX_CHARGE_POWER_ENTITY: "number.batt_charge",
                CONF_BATTERY_MAX_CHARGE_POWER_W: 5000,
                CONF_BATTERY_TRICKLE_CHARGE_POWER_W: 100,
                CONF_EXPORT_LIMIT: 0,
            })
        )
        assert result["type"] == "form"
        assert result["step_id"] == "battery"
        assert result["errors"]["base"] == "missing_export_limit_for_dynamic_charge"

    @pytest.mark.asyncio
    async def test_dynamic_charge_all_valid_passes(self):
        """Feature on with all required fields → form accepted."""
        flow = _make_flow()
        flow.data[CONF_INVERTER_TYPE] = InverterType.HYBRID
        result = await flow.async_step_battery(
            user_input=_base_battery_input({
                CONF_DYNAMIC_BATTERY_CHARGE_ENABLED: True,
                CONF_INVERTER_BATTERY_MAX_CHARGE_POWER_ENTITY: "number.batt_charge",
                CONF_BATTERY_MAX_CHARGE_POWER_W: 5000,
                CONF_BATTERY_TRICKLE_CHARGE_POWER_W: 100,
                CONF_EXPORT_LIMIT: 6000,
            })
        )
        assert result["step_id"] == "settings"
        assert not result.get("errors")
