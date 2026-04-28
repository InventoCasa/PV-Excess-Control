"""Config flow for PV Excess Control integration.

Multi-step config flow that collects:
1. Inverter setup (type, grid voltage)
2. Sensor mapping (PV, grid, load, battery sensors)
3. Energy pricing (tariff provider, thresholds)
4. Solar forecast (provider, sensor)
5. Battery strategy (only for hybrid inverters)
6. Global settings (export limit, intervals)

Also provides ApplianceSubentryFlow for managing appliances as subentries.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
    TimeSelector,
)

# ConfigSubentryFlow is available in HA 2025.x+
try:
    from homeassistant.config_entries import (
        ConfigSubentryFlow,
        SubentryFlowResult,
    )
except ImportError:
    # Fallback for older HA versions - define a base class stub
    ConfigSubentryFlow = None  # type: ignore[assignment, misc]
    SubentryFlowResult = dict  # type: ignore[assignment, misc]

from .const import (
    CONF_ACTUAL_POWER_ENTITY,
    CONF_ALLOW_GRID_CHARGING,
    CONF_ALLOW_GRID_SUPPLEMENT,
    CONF_APPLIANCE_ENTITY,
    CONF_APPLIANCE_NAME,
    CONF_APPLIANCE_PRIORITY,
    CONF_BATTERY_CAPACITY,
    CONF_BATTERY_CHARGE_PRICE_THRESHOLD,
    CONF_BATTERY_DISCHARGE_OVERRIDE,
    CONF_BATTERY_MAX_DISCHARGE_DEFAULT,
    CONF_BATTERY_MAX_DISCHARGE_ENTITY,
    CONF_MIN_BATTERY_SOC,
    CONF_BATTERY_CHARGE_POWER,
    CONF_BATTERY_DISCHARGE_POWER,
    CONF_BATTERY_POWER,
    CONF_BATTERY_SOC,
    CONF_BATTERY_STRATEGY,
    CONF_BATTERY_TARGET_SOC,
    CONF_BATTERY_TARGET_TIME,
    CONF_CHEAP_PRICE_THRESHOLD,
    CONF_CONTROLLER_INTERVAL,
    CONF_CURRENT_ENTITY,
    CONF_CURRENT_STEP,
    CONF_DYNAMIC_CURRENT,
    CONF_ENABLE_PREEMPTION,
    CONF_END_BEFORE,
    CONF_EV_CONNECTED_ENTITY,
    CONF_EV_SOC_ENTITY,
    CONF_EV_TARGET_SOC,
    CONF_EXPORT_LIMIT,
    CONF_FEED_IN_TARIFF,
    CONF_FEED_IN_TARIFF_SENSOR,
    CONF_FORECAST_PROVIDER,
    CONF_FORECAST_SENSOR,
    CONF_FORECAST_TOMORROW_SENSOR,
    CONF_GRID_EXPORT,
    CONF_GRID_VOLTAGE,
    CONF_IMPORT_EXPORT,
    CONF_INVERTER_TYPE,
    CONF_IS_BIG_CONSUMER,
    CONF_LOAD_POWER,
    CONF_MAX_CURRENT,
    CONF_MAX_DAILY_ACTIVATIONS,
    CONF_MAX_DAILY_RUNTIME,
    CONF_MAX_GRID_POWER,
    CONF_MIN_CURRENT,
    CONF_MIN_DAILY_RUNTIME,
    CONF_NOMINAL_POWER,
    CONF_NOTIFICATION_SERVICE,
    CONF_NOTIFY_APPLIANCE_OFF,
    CONF_NOTIFY_APPLIANCE_ON,
    CONF_NOTIFY_DAILY_SUMMARY,
    CONF_OFF_THRESHOLD,
    CONF_ON_ONLY,
    CONF_ON_THRESHOLD,
    CONF_PHASES,
    CONF_PLAN_INFLUENCE,
    CONF_PLANNER_INTERVAL,
    CONF_PROTECT_FROM_PREEMPTION,
    CONF_PRICE_SENSOR,
    CONF_PV_POWER,
    CONF_HELPER_ONLY,
    CONF_REQUIRES_APPLIANCE,
    CONF_SCHEDULE_DEADLINE,
    CONF_START_AFTER,
    CONF_AVERAGING_WINDOW,
    CONF_SWITCH_INTERVAL,
    CONF_TARIFF_PROVIDER,
    DEFAULT_CONTROLLER_INTERVAL,
    DEFAULT_GRID_VOLTAGE,
    DEFAULT_OFF_THRESHOLD,
    DEFAULT_PLANNER_INTERVAL,
    DEFAULT_SWITCH_INTERVAL,
    DOMAIN,
    MAX_CURRENT,
    MAX_PHASES,
    MAX_PRIORITY,
    MIN_CURRENT,
    MIN_PHASES,
    MIN_PRIORITY,
    BatteryStrategy,
    ForecastProvider,
    InverterType,
    PlanInfluence,
    TariffProvider,
)

_LOGGER = logging.getLogger(__name__)

SUBENTRY_TYPE_APPLIANCE = "appliance"

# Selector options for enums
INVERTER_TYPE_OPTIONS = [
    {"value": InverterType.STANDARD, "label": "Standard"},
    {"value": InverterType.HYBRID, "label": "Hybrid (with Battery)"},
]

TARIFF_PROVIDER_OPTIONS = [
    {"value": TariffProvider.NONE, "label": "None"},
    {"value": TariffProvider.GENERIC, "label": "Generic (sensor)"},
    {"value": TariffProvider.TIBBER, "label": "Tibber"},
    {"value": TariffProvider.AWATTAR, "label": "aWATTar"},
    {"value": TariffProvider.NORDPOOL, "label": "Nordpool"},
    {"value": TariffProvider.OCTOPUS, "label": "Octopus Energy"},
]

FORECAST_PROVIDER_OPTIONS = [
    {"value": ForecastProvider.NONE, "label": "None"},
    {"value": ForecastProvider.SOLCAST, "label": "Solcast"},
    {"value": ForecastProvider.FORECAST_SOLAR, "label": "Forecast.Solar"},
    {"value": ForecastProvider.GENERIC, "label": "Generic (sensor)"},
]

BATTERY_STRATEGY_OPTIONS = [
    {"value": BatteryStrategy.BATTERY_FIRST, "label": "Battery First"},
    {"value": BatteryStrategy.APPLIANCE_FIRST, "label": "Appliance First"},
    {"value": BatteryStrategy.BALANCED, "label": "Balanced"},
]

PLAN_INFLUENCE_OPTIONS = [
    {"value": "none", "label": "None (pure reactive)"},
    {"value": "light", "label": "Light (lower thresholds)"},
    {"value": "plan_follows", "label": "Plan follows (schedule-driven)"},
]

CONTROLLER_INTERVAL_OPTIONS = [
    {"value": "15", "label": "15 seconds"},
    {"value": "30", "label": "30 seconds"},
    {"value": "60", "label": "60 seconds"},
]

PLANNER_INTERVAL_OPTIONS = [
    {"value": "900", "label": "15 minutes"},
    {"value": "1800", "label": "30 minutes"},
]

# Entity selectors
SENSOR_ENTITY_SELECTOR = EntitySelector(
    EntitySelectorConfig(domain=["sensor", "input_number"])
)

NUMBER_ENTITY_SELECTOR = EntitySelector(
    EntitySelectorConfig(domain=["number", "input_number"])
)

SWITCH_ENTITY_SELECTOR = EntitySelector(
    EntitySelectorConfig(domain=["switch", "input_boolean", "light", "climate", "fan"])
)

BINARY_SENSOR_ENTITY_SELECTOR = EntitySelector(
    EntitySelectorConfig(domain="binary_sensor")
)

PHASE_OPTIONS = [
    {"value": "1", "label": "1 Phase"},
    {"value": "2", "label": "2 Phases"},
    {"value": "3", "label": "3 Phases"},
]


def _appliance_basic_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Build schema for appliance basic info + power profile step."""
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_APPLIANCE_NAME,
                default=d.get(CONF_APPLIANCE_NAME, ""),
            ): str,
            vol.Required(
                CONF_APPLIANCE_ENTITY,
                default=d.get(CONF_APPLIANCE_ENTITY),
            ): SWITCH_ENTITY_SELECTOR,
            vol.Required(
                CONF_APPLIANCE_PRIORITY,
                default=d.get(CONF_APPLIANCE_PRIORITY, 500),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=MIN_PRIORITY,
                    max=MAX_PRIORITY,
                    step=1,
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_NOMINAL_POWER,
                default=d.get(CONF_NOMINAL_POWER, 0),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=100000,
                    step=1,
                    unit_of_measurement="W",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_ACTUAL_POWER_ENTITY,
                description={"suggested_value": d.get(CONF_ACTUAL_POWER_ENTITY)},
            ): SENSOR_ENTITY_SELECTOR,
            vol.Required(
                CONF_PHASES,
                default=str(d.get(CONF_PHASES, 1)),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=PHASE_OPTIONS,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


def _appliance_current_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Build schema for dynamic current + EV settings step."""
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_DYNAMIC_CURRENT,
                default=d.get(CONF_DYNAMIC_CURRENT, False),
            ): BooleanSelector(),
            vol.Optional(
                CONF_CURRENT_ENTITY,
                description={"suggested_value": d.get(CONF_CURRENT_ENTITY)},
            ): NUMBER_ENTITY_SELECTOR,
            vol.Optional(
                CONF_MIN_CURRENT,
                default=d.get(CONF_MIN_CURRENT, 6.0),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=MIN_CURRENT,
                    max=MAX_CURRENT,
                    step=0.1,
                    unit_of_measurement="A",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_MAX_CURRENT,
                default=d.get(CONF_MAX_CURRENT, 16.0),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=MIN_CURRENT,
                    max=MAX_CURRENT,
                    step=0.1,
                    unit_of_measurement="A",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_CURRENT_STEP,
                default=d.get(CONF_CURRENT_STEP, 0.1),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0.1,
                    max=1.0,
                    step=0.1,
                    unit_of_measurement="A",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_EV_SOC_ENTITY,
                description={"suggested_value": d.get(CONF_EV_SOC_ENTITY)},
            ): SENSOR_ENTITY_SELECTOR,
            vol.Optional(
                CONF_EV_CONNECTED_ENTITY,
                description={"suggested_value": d.get(CONF_EV_CONNECTED_ENTITY)},
            ): BINARY_SENSOR_ENTITY_SELECTOR,
            vol.Optional(
                CONF_EV_TARGET_SOC,
                default=d.get(CONF_EV_TARGET_SOC, 100),
                description={"suggested_value": d.get(CONF_EV_TARGET_SOC)},
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=100,
                    step=1,
                    unit_of_measurement="%",
                    mode=NumberSelectorMode.SLIDER,
                )
            ),
        }
    )


def _appliance_constraints_schema(
    defaults: dict[str, Any] | None = None,
    available_appliances: dict[str, str] | None = None,
) -> vol.Schema:
    """Build schema for constraints + grid supplement + big consumer step."""
    d = defaults or {}
    schema_dict: dict[vol.Marker, Any] = {
        vol.Required(
            CONF_SWITCH_INTERVAL,
            default=d.get(CONF_SWITCH_INTERVAL, DEFAULT_SWITCH_INTERVAL),
        ): NumberSelector(
            NumberSelectorConfig(
                min=5,
                max=3600,
                step=1,
                unit_of_measurement="s",
                mode=NumberSelectorMode.BOX,
            )
        ),
        vol.Optional(
            CONF_AVERAGING_WINDOW,
            description={"suggested_value": d.get(CONF_AVERAGING_WINDOW)},
        ): NumberSelector(
            NumberSelectorConfig(
                min=30,
                max=1800,
                step=30,
                unit_of_measurement="s",
                mode=NumberSelectorMode.BOX,
            )
        ),
        vol.Required(
            CONF_ON_ONLY,
            default=d.get(CONF_ON_ONLY, False),
        ): BooleanSelector(),
        vol.Required(
            CONF_PROTECT_FROM_PREEMPTION,
            default=d.get(CONF_PROTECT_FROM_PREEMPTION, False),
        ): BooleanSelector(),
        vol.Optional(
            CONF_MIN_DAILY_RUNTIME,
            description={"suggested_value": d.get(CONF_MIN_DAILY_RUNTIME)},
        ): NumberSelector(
            NumberSelectorConfig(
                min=0,
                max=1440,
                step=1,
                unit_of_measurement="min",
                mode=NumberSelectorMode.BOX,
            )
        ),
        vol.Optional(
            CONF_MAX_DAILY_RUNTIME,
            description={"suggested_value": d.get(CONF_MAX_DAILY_RUNTIME)},
        ): NumberSelector(
            NumberSelectorConfig(
                min=0,
                max=1440,
                step=1,
                unit_of_measurement="min",
                mode=NumberSelectorMode.BOX,
            )
        ),
        vol.Optional(
            CONF_MAX_DAILY_ACTIVATIONS,
            description={"suggested_value": d.get(CONF_MAX_DAILY_ACTIVATIONS)},
        ): NumberSelector(
            NumberSelectorConfig(
                min=1,
                max=100,
                step=1,
                mode=NumberSelectorMode.BOX,
            )
        ),
        vol.Optional(
            CONF_ON_THRESHOLD,
            description={"suggested_value": d.get(CONF_ON_THRESHOLD)},
        ): NumberSelector(
            NumberSelectorConfig(
                min=0,
                max=1000,
                step=10,
                unit_of_measurement="W",
                mode=NumberSelectorMode.BOX,
            )
        ),
        vol.Optional(
            CONF_SCHEDULE_DEADLINE,
            description={"suggested_value": d.get(CONF_SCHEDULE_DEADLINE)},
        ): TimeSelector(),
        vol.Optional(
            CONF_START_AFTER,
            description={"suggested_value": d.get(CONF_START_AFTER)},
        ): TimeSelector(),
        vol.Optional(
            CONF_END_BEFORE,
            description={"suggested_value": d.get(CONF_END_BEFORE)},
        ): TimeSelector(),
        vol.Required(
            CONF_ALLOW_GRID_SUPPLEMENT,
            default=d.get(CONF_ALLOW_GRID_SUPPLEMENT, False),
        ): BooleanSelector(),
        vol.Optional(
            CONF_MAX_GRID_POWER,
            description={"suggested_value": d.get(CONF_MAX_GRID_POWER)},
        ): NumberSelector(
            NumberSelectorConfig(
                min=0,
                max=100000,
                step=1,
                unit_of_measurement="W",
                mode=NumberSelectorMode.BOX,
            )
        ),
        vol.Required(
            CONF_IS_BIG_CONSUMER,
            default=d.get(CONF_IS_BIG_CONSUMER, False),
        ): BooleanSelector(),
        vol.Optional(
            CONF_BATTERY_DISCHARGE_OVERRIDE,
            description={"suggested_value": d.get(CONF_BATTERY_DISCHARGE_OVERRIDE)},
        ): NumberSelector(
            NumberSelectorConfig(
                min=0,
                max=100000,
                step=1,
                unit_of_measurement="W",
                mode=NumberSelectorMode.BOX,
            )
        ),
        vol.Required(
            CONF_HELPER_ONLY,
            default=d.get(CONF_HELPER_ONLY, False),
        ): BooleanSelector(),
    }

    if available_appliances:
        options = [{"value": "", "label": "(None)"}] + [
            {"value": aid, "label": aname}
            for aid, aname in available_appliances.items()
        ]
        schema_dict[vol.Optional(
            CONF_REQUIRES_APPLIANCE,
            description={"suggested_value": d.get(CONF_REQUIRES_APPLIANCE, "")},
        )] = SelectSelector(
            SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
        )

    return vol.Schema(schema_dict)


def _sensor_schema(
    is_hybrid: bool, defaults: dict[str, Any] | None = None
) -> vol.Schema:
    """Build the sensor mapping schema.

    Shows battery fields only when inverter type is hybrid.
    """
    d = defaults or {}
    schema_dict: dict[vol.Marker, Any] = {
        vol.Required(CONF_PV_POWER, description={"suggested_value": d.get(CONF_PV_POWER)}): SENSOR_ENTITY_SELECTOR,
        vol.Optional(CONF_GRID_EXPORT, description={"suggested_value": d.get(CONF_GRID_EXPORT)}): SENSOR_ENTITY_SELECTOR,
        vol.Optional(CONF_IMPORT_EXPORT, description={"suggested_value": d.get(CONF_IMPORT_EXPORT)}): SENSOR_ENTITY_SELECTOR,
        vol.Optional(CONF_LOAD_POWER, description={"suggested_value": d.get(CONF_LOAD_POWER)}): SENSOR_ENTITY_SELECTOR,
    }

    if is_hybrid:
        schema_dict[vol.Required(CONF_BATTERY_SOC, description={"suggested_value": d.get(CONF_BATTERY_SOC)})] = SENSOR_ENTITY_SELECTOR
        # Battery power: either combined (positive=charging, negative=discharging)
        # or separate charge/discharge sensors — same pattern as grid import/export
        schema_dict[vol.Optional(CONF_BATTERY_POWER, description={"suggested_value": d.get(CONF_BATTERY_POWER)})] = SENSOR_ENTITY_SELECTOR
        schema_dict[vol.Optional(CONF_BATTERY_CHARGE_POWER, description={"suggested_value": d.get(CONF_BATTERY_CHARGE_POWER)})] = SENSOR_ENTITY_SELECTOR
        schema_dict[vol.Optional(CONF_BATTERY_DISCHARGE_POWER, description={"suggested_value": d.get(CONF_BATTERY_DISCHARGE_POWER)})] = SENSOR_ENTITY_SELECTOR
        schema_dict[vol.Required(CONF_BATTERY_CAPACITY, default=d.get(CONF_BATTERY_CAPACITY, 10.0))] = NumberSelector(
            NumberSelectorConfig(
                min=0.1, max=1000, step=0.1, unit_of_measurement="kWh",
                mode=NumberSelectorMode.BOX,
            )
        )

    return vol.Schema(schema_dict)


def _energy_schema(
    tariff_provider: str, defaults: dict[str, Any] | None = None
) -> vol.Schema:
    """Build the energy pricing schema.

    Shows price sensor for any non-None provider (named providers like Tibber,
    Octopus, etc. also need an entity to read prices from).
    """
    d = defaults or {}
    schema_dict: dict[vol.Marker, Any] = {
        vol.Required(
            CONF_TARIFF_PROVIDER,
            default=d.get(CONF_TARIFF_PROVIDER, TariffProvider.NONE),
        ): SelectSelector(
            SelectSelectorConfig(
                options=TARIFF_PROVIDER_OPTIONS,
                mode=SelectSelectorMode.DROPDOWN,
            )
        ),
    }

    if tariff_provider != TariffProvider.NONE:
        schema_dict[vol.Required(CONF_PRICE_SENSOR, description={"suggested_value": d.get(CONF_PRICE_SENSOR)})] = SENSOR_ENTITY_SELECTOR

    schema_dict[vol.Optional(
        CONF_CHEAP_PRICE_THRESHOLD,
        description={"suggested_value": d.get(CONF_CHEAP_PRICE_THRESHOLD)},
    )] = NumberSelector(
        NumberSelectorConfig(
            min=0, max=1000, step=0.01,
            mode=NumberSelectorMode.BOX,
        )
    )
    schema_dict[vol.Optional(
        CONF_BATTERY_CHARGE_PRICE_THRESHOLD,
        description={"suggested_value": d.get(CONF_BATTERY_CHARGE_PRICE_THRESHOLD)},
    )] = NumberSelector(
        NumberSelectorConfig(
            min=0, max=1000, step=0.01,
            mode=NumberSelectorMode.BOX,
        )
    )

    schema_dict[vol.Optional(
        CONF_FEED_IN_TARIFF,
        default=d.get(CONF_FEED_IN_TARIFF, 0.0),
    )] = NumberSelector(
        NumberSelectorConfig(
            min=0, max=1000, step=0.001,
            mode=NumberSelectorMode.BOX,
        )
    )

    schema_dict[vol.Optional(
        CONF_FEED_IN_TARIFF_SENSOR,
        description={"suggested_value": d.get(CONF_FEED_IN_TARIFF_SENSOR)},
    )] = EntitySelector(
        EntitySelectorConfig(domain=["sensor", "input_number"])
    )

    return vol.Schema(schema_dict)


def _forecast_schema(
    forecast_provider: str, defaults: dict[str, Any] | None = None
) -> vol.Schema:
    """Build the forecast schema.

    Shows forecast sensor for any non-None provider (named providers like
    Solcast, Forecast.Solar also need an entity to read forecast data from).
    """
    d = defaults or {}
    schema_dict: dict[vol.Marker, Any] = {
        vol.Required(
            CONF_FORECAST_PROVIDER,
            default=d.get(CONF_FORECAST_PROVIDER, ForecastProvider.NONE),
        ): SelectSelector(
            SelectSelectorConfig(
                options=FORECAST_PROVIDER_OPTIONS,
                mode=SelectSelectorMode.DROPDOWN,
            )
        ),
    }

    if forecast_provider != ForecastProvider.NONE:
        schema_dict[vol.Required(CONF_FORECAST_SENSOR, description={"suggested_value": d.get(CONF_FORECAST_SENSOR)})] = SENSOR_ENTITY_SELECTOR
        schema_dict[vol.Optional(CONF_FORECAST_TOMORROW_SENSOR, description={"suggested_value": d.get(CONF_FORECAST_TOMORROW_SENSOR)})] = SENSOR_ENTITY_SELECTOR

    return vol.Schema(schema_dict)


def _battery_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Build the battery strategy schema."""
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_BATTERY_STRATEGY,
                default=d.get(CONF_BATTERY_STRATEGY, BatteryStrategy.BALANCED),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=BATTERY_STRATEGY_OPTIONS,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_BATTERY_TARGET_SOC,
                default=d.get(CONF_BATTERY_TARGET_SOC, 80),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0, max=100, step=1, unit_of_measurement="%",
                    mode=NumberSelectorMode.SLIDER,
                )
            ),
            vol.Required(
                CONF_BATTERY_TARGET_TIME,
                default=d.get(CONF_BATTERY_TARGET_TIME, "16:00"),
            ): TimeSelector(),
            vol.Required(
                CONF_ALLOW_GRID_CHARGING,
                default=d.get(CONF_ALLOW_GRID_CHARGING, False),
            ): BooleanSelector(),
            vol.Optional(
                CONF_BATTERY_MAX_DISCHARGE_ENTITY,
                description={"suggested_value": d.get(CONF_BATTERY_MAX_DISCHARGE_ENTITY)},
            ): NUMBER_ENTITY_SELECTOR,
            vol.Optional(
                CONF_BATTERY_MAX_DISCHARGE_DEFAULT,
                description={"suggested_value": d.get(CONF_BATTERY_MAX_DISCHARGE_DEFAULT)},
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0, max=100000, step=1, unit_of_measurement="W",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_MIN_BATTERY_SOC,
                description={"suggested_value": d.get(CONF_MIN_BATTERY_SOC)},
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0, max=100, step=1, unit_of_measurement="%",
                    mode=NumberSelectorMode.SLIDER,
                )
            ),
        }
    )


# Keep module-level constant for backwards compatibility in tests
BATTERY_SCHEMA = _battery_schema()

def _settings_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Build the global settings schema."""
    d = defaults or {}
    return vol.Schema(
        {
            vol.Optional(
                CONF_EXPORT_LIMIT,
                default=d.get(CONF_EXPORT_LIMIT, 0),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0, max=100000, step=1, unit_of_measurement="W",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_OFF_THRESHOLD,
                default=d.get(CONF_OFF_THRESHOLD, DEFAULT_OFF_THRESHOLD),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=-500, max=0, step=10, unit_of_measurement="W",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_CONTROLLER_INTERVAL,
                default=str(d.get(CONF_CONTROLLER_INTERVAL, DEFAULT_CONTROLLER_INTERVAL)),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=CONTROLLER_INTERVAL_OPTIONS,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_PLANNER_INTERVAL,
                default=str(d.get(CONF_PLANNER_INTERVAL, DEFAULT_PLANNER_INTERVAL)),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=PLANNER_INTERVAL_OPTIONS,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_PLAN_INFLUENCE,
                default=d.get(CONF_PLAN_INFLUENCE, PlanInfluence.LIGHT),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=PLAN_INFLUENCE_OPTIONS,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_ENABLE_PREEMPTION,
                default=d.get(CONF_ENABLE_PREEMPTION, True),
            ): BooleanSelector(),
            vol.Optional(
                CONF_NOTIFICATION_SERVICE,
                description={"suggested_value": d.get(CONF_NOTIFICATION_SERVICE)},
            ): TextSelector(
                TextSelectorConfig(type=TextSelectorType.TEXT)
            ),
            vol.Required(
                CONF_NOTIFY_APPLIANCE_ON,
                default=d.get(CONF_NOTIFY_APPLIANCE_ON, True),
            ): BooleanSelector(),
            vol.Required(
                CONF_NOTIFY_APPLIANCE_OFF,
                default=d.get(CONF_NOTIFY_APPLIANCE_OFF, True),
            ): BooleanSelector(),
            vol.Required(
                CONF_NOTIFY_DAILY_SUMMARY,
                default=d.get(CONF_NOTIFY_DAILY_SUMMARY, True),
            ): BooleanSelector(),
        }
    )


# Keep module-level constant for backwards compatibility in tests
SETTINGS_SCHEMA = _settings_schema()


class PvExcessControlConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for PV Excess Control."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self.data: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Step 1: Inverter Setup
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the inverter setup step."""
        # Prevent multiple instances -- only one config entry is supported
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")

        errors: dict[str, str] = {}

        if user_input is not None:
            grid_voltage = user_input.get(CONF_GRID_VOLTAGE, DEFAULT_GRID_VOLTAGE)
            if not (100 <= grid_voltage <= 500):
                errors[CONF_GRID_VOLTAGE] = "invalid_voltage"
            if not errors:
                self.data.update(user_input)
                return await self.async_step_sensors()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_INVERTER_TYPE, default=InverterType.HYBRID
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=INVERTER_TYPE_OPTIONS,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(
                    CONF_GRID_VOLTAGE, default=DEFAULT_GRID_VOLTAGE
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=100, max=500, step=1, unit_of_measurement="V",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            last_step=False,
        )

    # ------------------------------------------------------------------
    # Step 2: Sensor Mapping
    # ------------------------------------------------------------------

    async def async_step_sensors(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the sensor mapping step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Validate: at least one of grid_export, import_export_power, or load_power.
            # PV + Load alone is a valid configuration — the coordinator uses
            # excess = pv - load when no grid sensor is configured.
            has_grid = bool(user_input.get(CONF_GRID_EXPORT))
            has_combined = bool(user_input.get(CONF_IMPORT_EXPORT))
            has_load = bool(user_input.get(CONF_LOAD_POWER))
            if not has_grid and not has_combined and not has_load:
                errors["base"] = "no_grid_sensor"

            # For hybrid, battery fields are required (enforced by schema)
            if not errors:
                self.data.update(user_input)
                # Clean optional sensor keys not present in user_input
                for key in [CONF_GRID_EXPORT, CONF_IMPORT_EXPORT, CONF_LOAD_POWER,
                            CONF_BATTERY_POWER, CONF_BATTERY_CHARGE_POWER, CONF_BATTERY_DISCHARGE_POWER]:
                    if key not in user_input:
                        self.data.pop(key, None)
                return await self.async_step_energy()

        is_hybrid = self.data.get(CONF_INVERTER_TYPE) == InverterType.HYBRID
        schema = _sensor_schema(is_hybrid, defaults=self.data)

        return self.async_show_form(
            step_id="sensors",
            data_schema=schema,
            errors=errors,
            last_step=False,
        )

    # ------------------------------------------------------------------
    # Step 3: Energy Pricing
    # ------------------------------------------------------------------

    async def async_step_energy(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the energy pricing step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            tariff = user_input.get(CONF_TARIFF_PROVIDER, TariffProvider.NONE)

            # Any non-None provider requires a price sensor entity
            if tariff != TariffProvider.NONE and not user_input.get(
                CONF_PRICE_SENSOR
            ):
                errors[CONF_PRICE_SENSOR] = "missing_price_sensor"

            if not errors:
                self.data.update(user_input)
                # Clean optional energy keys not present in user_input
                for key in [CONF_FEED_IN_TARIFF_SENSOR]:
                    if key not in user_input:
                        self.data.pop(key, None)
                return await self.async_step_forecast()

        # Determine current tariff provider for conditional fields
        tariff_provider = (
            user_input.get(CONF_TARIFF_PROVIDER, TariffProvider.NONE)
            if user_input
            else self.data.get(CONF_TARIFF_PROVIDER, TariffProvider.NONE)
        )
        # Merge user_input into defaults so the form remembers selections on error
        form_defaults = {**self.data, **(user_input or {})}
        schema = _energy_schema(tariff_provider, defaults=form_defaults)

        return self.async_show_form(
            step_id="energy",
            data_schema=schema,
            errors=errors,
            last_step=False,
        )

    # ------------------------------------------------------------------
    # Step 4: Solar Forecast
    # ------------------------------------------------------------------

    async def async_step_forecast(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the solar forecast step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            provider = user_input.get(
                CONF_FORECAST_PROVIDER, ForecastProvider.NONE
            )
            # Any non-None provider requires a forecast sensor entity
            if provider != ForecastProvider.NONE and not user_input.get(
                CONF_FORECAST_SENSOR
            ):
                errors[CONF_FORECAST_SENSOR] = "missing_forecast_sensor"

            if not errors:
                self.data.update(user_input)
                # If hybrid, go to battery step; otherwise skip to settings
                is_hybrid = (
                    self.data.get(CONF_INVERTER_TYPE) == InverterType.HYBRID
                )
                if is_hybrid:
                    return await self.async_step_battery()
                return await self.async_step_settings()

        # Determine current forecast provider for conditional fields
        forecast_provider = (
            user_input.get(CONF_FORECAST_PROVIDER, ForecastProvider.NONE)
            if user_input
            else self.data.get(CONF_FORECAST_PROVIDER, ForecastProvider.NONE)
        )
        # Merge user_input into defaults so the form remembers selections on error
        form_defaults = {**self.data, **(user_input or {})}
        schema = _forecast_schema(forecast_provider, defaults=form_defaults)

        return self.async_show_form(
            step_id="forecast",
            data_schema=schema,
            errors=errors,
            last_step=False,
        )

    # ------------------------------------------------------------------
    # Step 5: Battery Strategy (hybrid only)
    # ------------------------------------------------------------------

    async def async_step_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the battery strategy step (hybrid inverters only)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            target_soc = user_input.get(CONF_BATTERY_TARGET_SOC, 80)
            if not (0 <= target_soc <= 100):
                errors[CONF_BATTERY_TARGET_SOC] = "invalid_soc"

            if not errors:
                self.data.update(user_input)
                return await self.async_step_settings()

        return self.async_show_form(
            step_id="battery",
            data_schema=_battery_schema(defaults=self.data),
            errors=errors,
            last_step=False,
        )

    # ------------------------------------------------------------------
    # Step 6: Global Settings (final step)
    # ------------------------------------------------------------------

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the global settings step and create the config entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            if not errors:
                # Convert string interval values to integers
                controller_interval = int(
                    user_input.get(
                        CONF_CONTROLLER_INTERVAL, str(DEFAULT_CONTROLLER_INTERVAL)
                    )
                )
                planner_interval = int(
                    user_input.get(
                        CONF_PLANNER_INTERVAL, str(DEFAULT_PLANNER_INTERVAL)
                    )
                )
                user_input[CONF_CONTROLLER_INTERVAL] = controller_interval
                user_input[CONF_PLANNER_INTERVAL] = planner_interval

                self.data.update(user_input)
                return self.async_create_entry(
                    title="PV Excess Control",
                    data=self.data,
                )

        return self.async_show_form(
            step_id="settings",
            data_schema=_settings_schema(defaults=self.data),
            errors=errors,
            last_step=True,
        )

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: config_entries.ConfigEntry
    ) -> dict[str, type]:
        """Return supported subentry types for this config entry."""
        return {SUBENTRY_TYPE_APPLIANCE: ApplianceSubentryFlowHandler}

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> PvExcessControlOptionsFlow:
        """Get the options flow handler."""
        return PvExcessControlOptionsFlow()


class PvExcessControlOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for PV Excess Control.

    Options flow allows editing the main configuration after initial setup.
    It mirrors the same multi-step structure as the config flow.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the first step of options flow (redirects to user step)."""
        # Initialize data from the config entry (config_entry is set by HA base class)
        if not hasattr(self, "data") or not self.data:
            self.data = dict(self.config_entry.data)
        return await self.async_step_user(user_input)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the inverter setup step in options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            grid_voltage = user_input.get(CONF_GRID_VOLTAGE, DEFAULT_GRID_VOLTAGE)
            if not (100 <= grid_voltage <= 500):
                errors[CONF_GRID_VOLTAGE] = "invalid_voltage"
            if not errors:
                self.data.update(user_input)
                return await self.async_step_sensors()

        form_defaults = {**self.data, **(user_input or {})}
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_INVERTER_TYPE,
                    default=form_defaults.get(CONF_INVERTER_TYPE, InverterType.HYBRID),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=INVERTER_TYPE_OPTIONS,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(
                    CONF_GRID_VOLTAGE,
                    default=form_defaults.get(CONF_GRID_VOLTAGE, DEFAULT_GRID_VOLTAGE),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=100, max=500, step=1, unit_of_measurement="V",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_sensors(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle sensor mapping in options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            has_grid = bool(user_input.get(CONF_GRID_EXPORT))
            has_combined = bool(user_input.get(CONF_IMPORT_EXPORT))
            has_load = bool(user_input.get(CONF_LOAD_POWER))
            if not has_grid and not has_combined and not has_load:
                errors["base"] = "no_grid_sensor"
            if not errors:
                self.data.update(user_input)
                # Clean optional sensor keys not present in user_input
                for key in [CONF_GRID_EXPORT, CONF_IMPORT_EXPORT, CONF_LOAD_POWER,
                            CONF_BATTERY_POWER, CONF_BATTERY_CHARGE_POWER, CONF_BATTERY_DISCHARGE_POWER]:
                    if key not in user_input:
                        self.data.pop(key, None)
                return await self.async_step_energy()

        is_hybrid = self.data.get(CONF_INVERTER_TYPE) == InverterType.HYBRID
        schema = _sensor_schema(is_hybrid, defaults=self.data)

        return self.async_show_form(
            step_id="sensors",
            data_schema=schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_energy(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle energy pricing in options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            tariff = user_input.get(CONF_TARIFF_PROVIDER, TariffProvider.NONE)
            # Any non-None provider requires a price sensor entity
            if tariff != TariffProvider.NONE and not user_input.get(
                CONF_PRICE_SENSOR
            ):
                errors[CONF_PRICE_SENSOR] = "missing_price_sensor"
            if not errors:
                self.data.update(user_input)
                # Clean optional energy keys not present in user_input
                for key in [CONF_FEED_IN_TARIFF_SENSOR]:
                    if key not in user_input:
                        self.data.pop(key, None)
                return await self.async_step_forecast()

        tariff_provider = (
            user_input.get(CONF_TARIFF_PROVIDER, TariffProvider.NONE)
            if user_input
            else self.data.get(CONF_TARIFF_PROVIDER, TariffProvider.NONE)
        )
        form_defaults = {**self.data, **(user_input or {})}
        schema = _energy_schema(tariff_provider, defaults=form_defaults)

        return self.async_show_form(
            step_id="energy",
            data_schema=schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_forecast(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle forecast configuration in options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            provider = user_input.get(
                CONF_FORECAST_PROVIDER, ForecastProvider.NONE
            )
            # Any non-None provider requires a forecast sensor entity
            if provider != ForecastProvider.NONE and not user_input.get(
                CONF_FORECAST_SENSOR
            ):
                errors[CONF_FORECAST_SENSOR] = "missing_forecast_sensor"
            if not errors:
                self.data.update(user_input)
                is_hybrid = (
                    self.data.get(CONF_INVERTER_TYPE) == InverterType.HYBRID
                )
                if is_hybrid:
                    return await self.async_step_battery()
                return await self.async_step_settings()

        forecast_provider = (
            user_input.get(CONF_FORECAST_PROVIDER, ForecastProvider.NONE)
            if user_input
            else self.data.get(CONF_FORECAST_PROVIDER, ForecastProvider.NONE)
        )
        form_defaults = {**self.data, **(user_input or {})}
        schema = _forecast_schema(forecast_provider, defaults=form_defaults)

        return self.async_show_form(
            step_id="forecast",
            data_schema=schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle battery strategy in options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            target_soc = user_input.get(CONF_BATTERY_TARGET_SOC, 80)
            if not (0 <= target_soc <= 100):
                errors[CONF_BATTERY_TARGET_SOC] = "invalid_soc"
            if not errors:
                self.data.update(user_input)
                # Clean optional battery fields not present in user_input
                for key in [CONF_MIN_BATTERY_SOC, CONF_BATTERY_MAX_DISCHARGE_ENTITY, CONF_BATTERY_MAX_DISCHARGE_DEFAULT]:
                    if key not in user_input:
                        self.data.pop(key, None)
                return await self.async_step_settings()

        return self.async_show_form(
            step_id="battery",
            data_schema=_battery_schema(defaults=self.data),
            errors=errors,
            last_step=False,
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle settings and create options entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            controller_interval = int(
                user_input.get(
                    CONF_CONTROLLER_INTERVAL, str(DEFAULT_CONTROLLER_INTERVAL)
                )
            )
            planner_interval = int(
                user_input.get(
                    CONF_PLANNER_INTERVAL, str(DEFAULT_PLANNER_INTERVAL)
                )
            )
            user_input[CONF_CONTROLLER_INTERVAL] = controller_interval
            user_input[CONF_PLANNER_INTERVAL] = planner_interval

            self.data.update(user_input)

            # Clean optional settings keys not present in user_input
            if CONF_NOTIFICATION_SERVICE not in user_input:
                self.data.pop(CONF_NOTIFICATION_SERVICE, None)

            # Clean up stale tariff/forecast sensor keys when provider is None
            if self.data.get(CONF_TARIFF_PROVIDER) == TariffProvider.NONE:
                self.data.pop(CONF_PRICE_SENSOR, None)
            if self.data.get(CONF_FORECAST_PROVIDER) == ForecastProvider.NONE:
                self.data.pop(CONF_FORECAST_SENSOR, None)
                self.data.pop(CONF_FORECAST_TOMORROW_SENSOR, None)

            # If inverter type changed from hybrid to standard, remove stale
            # battery-related keys that are no longer applicable.
            if self.data.get(CONF_INVERTER_TYPE) != InverterType.HYBRID:
                for key in [
                    CONF_BATTERY_SOC, CONF_BATTERY_POWER,
                    CONF_BATTERY_CHARGE_POWER, CONF_BATTERY_DISCHARGE_POWER,
                    CONF_BATTERY_CAPACITY, CONF_BATTERY_STRATEGY,
                    CONF_BATTERY_TARGET_SOC, CONF_BATTERY_TARGET_TIME,
                    CONF_ALLOW_GRID_CHARGING,
                    CONF_BATTERY_MAX_DISCHARGE_ENTITY,
                    CONF_BATTERY_MAX_DISCHARGE_DEFAULT,
                    CONF_MIN_BATTERY_SOC,
                ]:
                    self.data.pop(key, None)

            # Update the config entry's data directly (OptionsFlow.async_create_entry
            # saves to .options, but our integration reads from .data)
            self.hass.config_entries.async_update_entry(
                self.config_entry, data=self.data
            )
            return self.async_create_entry(data={})

        return self.async_show_form(
            step_id="settings",
            data_schema=_settings_schema(defaults=self.data),
            errors=errors,
            last_step=True,
        )


# ---------------------------------------------------------------------------
# Appliance Subentry Flow
# ---------------------------------------------------------------------------

# Determine the base class dynamically so the module loads on older HA versions.
_SubentryBase: type = ConfigSubentryFlow if ConfigSubentryFlow is not None else object


class ApplianceSubentryFlowHandler(_SubentryBase):  # type: ignore[misc]
    """Handle adding / editing an appliance subentry.

    Steps:
      1. user   - Basic info + power profile
      2. current - Dynamic current + EV settings
      3. constraints - Constraints + grid supplement + big consumer
    """

    def __init__(self) -> None:
        """Initialize the appliance subentry flow."""
        super().__init__()
        self._data: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Step 1: Basic Info + Power Profile
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle basic info + power profile step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            name = user_input.get(CONF_APPLIANCE_NAME, "").strip()
            if not name:
                errors[CONF_APPLIANCE_NAME] = "missing_name"

            entity = user_input.get(CONF_APPLIANCE_ENTITY)
            if not entity:
                errors[CONF_APPLIANCE_ENTITY] = "missing_entity"

            nominal = user_input.get(CONF_NOMINAL_POWER, 0)
            if nominal <= 0:
                errors[CONF_NOMINAL_POWER] = "invalid_power"

            if not errors:
                # Convert phases from string to int
                user_input[CONF_PHASES] = int(
                    user_input.get(CONF_PHASES, "1")
                )
                # Convert priority to int
                user_input[CONF_APPLIANCE_PRIORITY] = int(
                    user_input.get(CONF_APPLIANCE_PRIORITY, 500)
                )
                self._data.update(user_input)
                return await self.async_step_current()

        form_defaults = {**self._data, **(user_input or {})}
        schema = _appliance_basic_schema(form_defaults)

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            last_step=False,
        )

    # ------------------------------------------------------------------
    # Step 2: Dynamic Current + EV Settings
    # ------------------------------------------------------------------

    async def async_step_current(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle dynamic current and EV settings step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            dynamic = user_input.get(CONF_DYNAMIC_CURRENT, False)
            if dynamic:
                current_entity = user_input.get(CONF_CURRENT_ENTITY)
                if not current_entity:
                    errors[CONF_CURRENT_ENTITY] = "missing_current_entity"

                min_c = user_input.get(CONF_MIN_CURRENT, 6.0)
                max_c = user_input.get(CONF_MAX_CURRENT, 16.0)
                if min_c >= max_c:
                    errors[CONF_MIN_CURRENT] = "invalid_current_range"

            if not errors:
                self._data.update(user_input)
                # Clean optional current/EV keys not present in user_input
                for key in [CONF_CURRENT_ENTITY, CONF_EV_SOC_ENTITY, CONF_EV_CONNECTED_ENTITY,
                            CONF_EV_TARGET_SOC]:
                    if key not in user_input:
                        self._data.pop(key, None)
                return await self.async_step_constraints()

        form_defaults = {**self._data, **(user_input or {})}
        schema = _appliance_current_schema(form_defaults)

        return self.async_show_form(
            step_id="current",
            data_schema=schema,
            errors=errors,
            last_step=False,
        )

    # ------------------------------------------------------------------
    # Step 3: Constraints + Grid + Big Consumer
    # ------------------------------------------------------------------

    async def async_step_constraints(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle constraints, grid supplement, and big consumer step."""
        errors: dict[str, str] = {}

        # Build available appliances for dependency dropdown
        entry = None
        handler = getattr(self, "handler", None)
        if isinstance(handler, (list, tuple)) and len(handler) > 0:
            entry = self.hass.config_entries.async_get_entry(handler[0])
        available_appliances = {}
        if entry:
            for sid, sub in getattr(entry, "subentries", {}).items():
                # Exclude self (for reconfigure, use _subentry_id if available)
                if sid != getattr(self, "_subentry_id", None):
                    available_appliances[sid] = sub.data.get("appliance_name", f"Appliance {sid[:8]}")

        if user_input is not None:
            # Clean empty requires_appliance to None
            if user_input.get(CONF_REQUIRES_APPLIANCE) == "":
                user_input.pop(CONF_REQUIRES_APPLIANCE, None)

            # Circular dependency check (only when we know our own subentry ID)
            req = user_input.get(CONF_REQUIRES_APPLIANCE)
            my_id = getattr(self, "_subentry_id", None)
            if req and entry and my_id:
                req_sub = getattr(entry, "subentries", {}).get(req)
                if req_sub and req_sub.data.get(CONF_REQUIRES_APPLIANCE) == my_id:
                    errors[CONF_REQUIRES_APPLIANCE] = "circular_dependency"

            # Helper-only + requires_appliance is forbidden — chained
            # helpers are out of scope for v1 (see design spec).
            if user_input.get(CONF_HELPER_ONLY, False) and user_input.get(CONF_REQUIRES_APPLIANCE):
                errors[CONF_HELPER_ONLY] = "helper_only_with_requires"

            min_rt = user_input.get(CONF_MIN_DAILY_RUNTIME)
            max_rt = user_input.get(CONF_MAX_DAILY_RUNTIME)
            if (
                min_rt is not None
                and max_rt is not None
                and min_rt > max_rt
            ):
                errors[CONF_MIN_DAILY_RUNTIME] = "invalid_runtime_range"

            # Warn if high-power appliance is not marked as big consumer
            nominal = self._data.get(CONF_NOMINAL_POWER, 0)
            if (
                not errors
                and nominal >= 3000
                and not user_input.get(CONF_IS_BIG_CONSUMER, False)
                and not getattr(self, "_big_consumer_warned", False)
            ):
                self._big_consumer_warned = True
                errors[CONF_IS_BIG_CONSUMER] = "suggest_big_consumer"

            if not errors:
                self._data.update(user_input)
                # Clean optional constraint keys not present in user_input
                for key in [CONF_MIN_DAILY_RUNTIME, CONF_MAX_DAILY_RUNTIME, CONF_SCHEDULE_DEADLINE,
                            CONF_START_AFTER, CONF_END_BEFORE,
                            CONF_MAX_GRID_POWER, CONF_BATTERY_DISCHARGE_OVERRIDE,
                            CONF_AVERAGING_WINDOW, CONF_REQUIRES_APPLIANCE,
                            CONF_ON_THRESHOLD]:
                    if key not in user_input:
                        self._data.pop(key, None)
                title = self._data.get(CONF_APPLIANCE_NAME, "Appliance")
                return self.async_create_entry(
                    title=title,
                    data=self._data,
                )

        form_defaults = {**self._data, **(user_input or {})}
        schema = _appliance_constraints_schema(form_defaults, available_appliances=available_appliances)

        return self.async_show_form(
            step_id="constraints",
            data_schema=schema,
            errors=errors,
            last_step=True,
        )

    # ------------------------------------------------------------------
    # Reconfigure: pre-populate from existing subentry data
    # ------------------------------------------------------------------

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle reconfigure - first step with pre-populated data."""
        # Load existing subentry data into _data for defaults
        if not hasattr(self, "_get_reconfigure_subentry"):
            return self.async_abort(reason="reconfigure_not_supported")
        subentry = self._get_reconfigure_subentry()
        self._data = dict(subentry.data)
        self._subentry_id = getattr(subentry, "subentry_id", None) or getattr(subentry, "id", None)
        # Convert phases back to string for the selector
        if CONF_PHASES in self._data:
            self._data[CONF_PHASES] = str(self._data[CONF_PHASES])
        return await self.async_step_reconfigure_basic(None)

    async def async_step_reconfigure_basic(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Reconfigure step 1: basic info + power profile."""
        errors: dict[str, str] = {}

        if user_input is not None:
            name = user_input.get(CONF_APPLIANCE_NAME, "").strip()
            if not name:
                errors[CONF_APPLIANCE_NAME] = "missing_name"

            entity = user_input.get(CONF_APPLIANCE_ENTITY)
            if not entity:
                errors[CONF_APPLIANCE_ENTITY] = "missing_entity"

            nominal = user_input.get(CONF_NOMINAL_POWER, 0)
            if nominal <= 0:
                errors[CONF_NOMINAL_POWER] = "invalid_power"

            if not errors:
                user_input[CONF_PHASES] = int(
                    user_input.get(CONF_PHASES, "1")
                )
                user_input[CONF_APPLIANCE_PRIORITY] = int(
                    user_input.get(CONF_APPLIANCE_PRIORITY, 500)
                )
                self._data.update(user_input)
                return await self.async_step_reconfigure_current()

        form_defaults = {**self._data, **(user_input or {})}
        schema = _appliance_basic_schema(form_defaults)

        return self.async_show_form(
            step_id="reconfigure_basic",
            data_schema=schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_reconfigure_current(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Reconfigure step 2: dynamic current + EV settings."""
        errors: dict[str, str] = {}

        if user_input is not None:
            dynamic = user_input.get(CONF_DYNAMIC_CURRENT, False)
            if dynamic:
                current_entity = user_input.get(CONF_CURRENT_ENTITY)
                if not current_entity:
                    errors[CONF_CURRENT_ENTITY] = "missing_current_entity"

                min_c = user_input.get(CONF_MIN_CURRENT, 6.0)
                max_c = user_input.get(CONF_MAX_CURRENT, 16.0)
                if min_c >= max_c:
                    errors[CONF_MIN_CURRENT] = "invalid_current_range"

            if not errors:
                self._data.update(user_input)
                # Clean optional current/EV keys not present in user_input
                for key in [CONF_CURRENT_ENTITY, CONF_EV_SOC_ENTITY, CONF_EV_CONNECTED_ENTITY,
                            CONF_EV_TARGET_SOC]:
                    if key not in user_input:
                        self._data.pop(key, None)
                return await self.async_step_reconfigure_constraints()

        form_defaults = {**self._data, **(user_input or {})}
        schema = _appliance_current_schema(form_defaults)

        return self.async_show_form(
            step_id="reconfigure_current",
            data_schema=schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_reconfigure_constraints(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Reconfigure step 3: constraints + grid + big consumer."""
        errors: dict[str, str] = {}

        # Build available appliances for dependency dropdown
        entry = None
        handler = getattr(self, "handler", None)
        if isinstance(handler, (list, tuple)) and len(handler) > 0:
            entry = self.hass.config_entries.async_get_entry(handler[0])
        available_appliances = {}
        if entry:
            for sid, sub in getattr(entry, "subentries", {}).items():
                # Exclude self (for reconfigure, use _subentry_id if available)
                if sid != getattr(self, "_subentry_id", None):
                    available_appliances[sid] = sub.data.get("appliance_name", f"Appliance {sid[:8]}")

        if user_input is not None:
            # Clean empty requires_appliance to None
            if user_input.get(CONF_REQUIRES_APPLIANCE) == "":
                user_input.pop(CONF_REQUIRES_APPLIANCE, None)

            # Circular dependency check (only when we know our own subentry ID)
            req = user_input.get(CONF_REQUIRES_APPLIANCE)
            my_id = getattr(self, "_subentry_id", None)
            if req and entry and my_id:
                req_sub = getattr(entry, "subentries", {}).get(req)
                if req_sub and req_sub.data.get(CONF_REQUIRES_APPLIANCE) == my_id:
                    errors[CONF_REQUIRES_APPLIANCE] = "circular_dependency"

            # Helper-only + requires_appliance is forbidden — chained
            # helpers are out of scope for v1 (see design spec).
            if user_input.get(CONF_HELPER_ONLY, False) and user_input.get(CONF_REQUIRES_APPLIANCE):
                errors[CONF_HELPER_ONLY] = "helper_only_with_requires"

            min_rt = user_input.get(CONF_MIN_DAILY_RUNTIME)
            max_rt = user_input.get(CONF_MAX_DAILY_RUNTIME)
            if (
                min_rt is not None
                and max_rt is not None
                and min_rt > max_rt
            ):
                errors[CONF_MIN_DAILY_RUNTIME] = "invalid_runtime_range"

            # Warn if high-power appliance is not marked as big consumer
            nominal = self._data.get(CONF_NOMINAL_POWER, 0)
            if (
                not errors
                and nominal >= 3000
                and not user_input.get(CONF_IS_BIG_CONSUMER, False)
                and not getattr(self, "_big_consumer_warned", False)
            ):
                self._big_consumer_warned = True
                errors[CONF_IS_BIG_CONSUMER] = "suggest_big_consumer"

            if not errors:
                self._data.update(user_input)
                # Clean optional constraint keys not present in user_input
                for key in [CONF_MIN_DAILY_RUNTIME, CONF_MAX_DAILY_RUNTIME, CONF_SCHEDULE_DEADLINE,
                            CONF_START_AFTER, CONF_END_BEFORE,
                            CONF_MAX_GRID_POWER, CONF_BATTERY_DISCHARGE_OVERRIDE,
                            CONF_AVERAGING_WINDOW, CONF_REQUIRES_APPLIANCE,
                            CONF_ON_THRESHOLD]:
                    if key not in user_input:
                        self._data.pop(key, None)
                title = self._data.get(CONF_APPLIANCE_NAME, "Appliance")
                try:
                    return self.async_update_and_abort(
                        self._get_entry(),
                        self._get_reconfigure_subentry(),
                        title=title,
                        data=self._data,
                    )
                except (AttributeError, TypeError):
                    # Fallback: update subentry via config_entries API
                    try:
                        entry = self.hass.config_entries.async_get_entry(
                            self.handler[0] if isinstance(self.handler, tuple) else self.handler
                        )
                        if entry and hasattr(self.hass.config_entries, "async_update_subentry"):
                            subentry_id = self._subentry_id if hasattr(self, "_subentry_id") else None
                            if subentry_id and subentry_id in getattr(entry, "subentries", {}):
                                self.hass.config_entries.async_update_subentry(
                                    entry, entry.subentries[subentry_id],
                                    data=self._data, title=title,
                                )
                                return self.async_abort(reason="reconfigure_successful")
                    except Exception:
                        pass
                    return self.async_abort(reason="reconfigure_not_supported")

        form_defaults = {**self._data, **(user_input or {})}
        schema = _appliance_constraints_schema(form_defaults, available_appliances=available_appliances)

        return self.async_show_form(
            step_id="reconfigure_constraints",
            data_schema=schema,
            errors=errors,
            last_step=True,
        )
