"""Constants for the PV Excess Control integration."""
from enum import StrEnum

DOMAIN = "pv_excess_control"
MANUFACTURER = "PV Excess Control"

# Defaults
DEFAULT_CONTROLLER_INTERVAL = 30  # seconds
DEFAULT_PLANNER_INTERVAL = 900  # 15 minutes in seconds
DEFAULT_SWITCH_INTERVAL = 300  # 5 minutes in seconds
DEFAULT_GRID_VOLTAGE = 230
DEFAULT_STARTUP_GRACE_PERIOD = 120  # 2 minutes in seconds

# Hysteresis defaults
DEFAULT_ON_THRESHOLD = 200  # W excess before turning ON
DEFAULT_OFF_THRESHOLD = -50  # W excess before turning OFF
DEFAULT_DYNAMIC_ON_THRESHOLD = 50  # W buffer before starting dynamic current appliance

# Limits
MIN_PRIORITY = 1
MAX_PRIORITY = 1000
MIN_CURRENT = 0.0
MAX_CURRENT = 32.0
MIN_PHASES = 1
MAX_PHASES = 3


class InverterType(StrEnum):
    """Inverter type."""
    STANDARD = "standard"
    HYBRID = "hybrid"


class BatteryStrategy(StrEnum):
    """Battery charging strategy."""
    BATTERY_FIRST = "battery_first"
    APPLIANCE_FIRST = "appliance_first"
    BALANCED = "balanced"


class PlanInfluence(StrEnum):
    """How much the planner influences optimizer decisions."""
    NONE = "none"
    LIGHT = "light"
    PLAN_FOLLOWS = "plan_follows"


class Action(StrEnum):
    """Control action for an appliance."""
    ON = "on"
    OFF = "off"
    SET_CURRENT = "set_current"
    IDLE = "idle"


class PlanReason(StrEnum):
    """Reason for a planned action."""
    EXCESS_AVAILABLE = "excess_available"
    CHEAP_TARIFF = "cheap_tariff"
    MIN_RUNTIME = "min_runtime"
    DEADLINE = "deadline"
    EXPORT_LIMIT = "export_limit"
    MANUAL_OVERRIDE = "manual_override"
    FORCE_CHARGE = "force_charge"
    WEATHER_PREPLANNING = "weather_preplanning"
    INSUFFICIENT_EXCESS = "insufficient_excess"
    MAX_RUNTIME_REACHED = "max_runtime_reached"
    EV_DISCONNECTED = "ev_disconnected"


class TariffProvider(StrEnum):
    """Supported tariff providers."""
    NONE = "none"
    GENERIC = "generic"
    TIBBER = "tibber"
    AWATTAR = "awattar"
    NORDPOOL = "nordpool"
    OCTOPUS = "octopus"


class ForecastProvider(StrEnum):
    """Supported forecast providers."""
    NONE = "none"
    SOLCAST = "solcast"
    FORECAST_SOLAR = "forecast_solar"
    GENERIC = "generic"


class NotificationEvent(StrEnum):
    """Notification event types."""
    APPLIANCE_ON = "appliance_on"
    APPLIANCE_OFF = "appliance_off"
    OVERRIDE_ACTIVATED = "override_activated"
    FORCE_CHARGE = "force_charge"
    SENSOR_UNAVAILABLE = "sensor_unavailable"
    DAILY_SUMMARY = "daily_summary"
    FORECAST_WARNING = "forecast_warning"
    PLAN_DEVIATION = "plan_deviation"


# Default notification settings (event -> enabled by default)
DEFAULT_NOTIFICATION_SETTINGS: dict[str, bool] = {
    NotificationEvent.APPLIANCE_ON: False,
    NotificationEvent.APPLIANCE_OFF: False,
    NotificationEvent.OVERRIDE_ACTIVATED: True,
    NotificationEvent.FORCE_CHARGE: True,
    NotificationEvent.SENSOR_UNAVAILABLE: True,
    NotificationEvent.DAILY_SUMMARY: False,
    NotificationEvent.FORECAST_WARNING: False,
    NotificationEvent.PLAN_DEVIATION: False,
}

# Config flow keys
CONF_INVERTER_TYPE = "inverter_type"
CONF_GRID_VOLTAGE = "grid_voltage"
CONF_PV_POWER = "pv_power"
CONF_GRID_EXPORT = "grid_export"
CONF_IMPORT_EXPORT = "import_export_power"
CONF_LOAD_POWER = "load_power"
CONF_BATTERY_SOC = "battery_soc"
CONF_BATTERY_POWER = "battery_power"
CONF_BATTERY_CHARGE_POWER = "battery_charge_power"
CONF_BATTERY_DISCHARGE_POWER = "battery_discharge_power"
CONF_BATTERY_CAPACITY = "battery_capacity"
CONF_TARIFF_PROVIDER = "tariff_provider"
CONF_PRICE_SENSOR = "price_sensor"
CONF_CHEAP_PRICE_THRESHOLD = "cheap_price_threshold"
CONF_BATTERY_CHARGE_PRICE_THRESHOLD = "battery_charge_price_threshold"
CONF_FEED_IN_TARIFF = "feed_in_tariff"
CONF_FEED_IN_TARIFF_SENSOR = "feed_in_tariff_sensor"
CONF_FORECAST_PROVIDER = "forecast_provider"
CONF_FORECAST_SENSOR = "forecast_sensor"
CONF_FORECAST_TOMORROW_SENSOR = "forecast_tomorrow_sensor"
CONF_BATTERY_STRATEGY = "battery_strategy"
CONF_BATTERY_TARGET_SOC = "battery_target_soc"
CONF_BATTERY_TARGET_TIME = "battery_target_time"
CONF_ALLOW_GRID_CHARGING = "allow_grid_charging"
CONF_BATTERY_MAX_DISCHARGE_ENTITY = "battery_max_discharge_entity"
CONF_BATTERY_MAX_DISCHARGE_DEFAULT = "battery_max_discharge_default"
CONF_MIN_BATTERY_SOC = "min_battery_soc"

# Inverter forced grid-charge (auto + manual)
CONF_AUTO_BATTERY_GRID_CHARGE = "auto_battery_grid_charge"
CONF_BATTERY_GRID_CHARGE_POWER_W = "battery_grid_charge_power_w"
CONF_GRID_CHARGE_ENGAGE_MIN_DURATION_MINUTES = "grid_charge_engage_min_duration_minutes"

CONF_INVERTER_FORCE_CHARGE_ENABLE_ENTITY = "inverter_force_charge_enable_entity"
CONF_INVERTER_FORCE_CHARGE_ENABLE_ENGAGE_VALUE = "inverter_force_charge_enable_engage_value"
CONF_INVERTER_FORCE_CHARGE_ENABLE_DISENGAGE_VALUE = "inverter_force_charge_enable_disengage_value"
CONF_INVERTER_FORCE_CHARGE_MODE_ENTITY = "inverter_force_charge_mode_entity"
CONF_INVERTER_FORCE_CHARGE_MODE_ENGAGE_VALUE = "inverter_force_charge_mode_engage_value"
CONF_INVERTER_FORCE_CHARGE_MODE_DISENGAGE_VALUE = "inverter_force_charge_mode_disengage_value"
CONF_INVERTER_FORCE_CHARGE_POWER_ENTITY = "inverter_force_charge_power_entity"

DEFAULT_GRID_CHARGE_ENGAGE_MIN_DURATION_MINUTES = 5

# Forecast-aware grid charging and acknowledged battery holding.
CONF_BATTERY_GRID_TARGET_SOC = "battery_grid_target_soc"
CONF_BATTERY_ROUNDTRIP_EFFICIENCY = "battery_roundtrip_efficiency"
CONF_BATTERY_WEAR_COST_PER_KWH = "battery_wear_cost_per_kwh"
CONF_BATTERY_SOC_HYSTERESIS = "battery_soc_hysteresis"
CONF_BATTERY_INPUT_MAX_AGE_SECONDS = "battery_input_max_age_seconds"
CONF_BATTERY_FORECAST_MAX_AGE_SECONDS = "battery_forecast_max_age_seconds"
CONF_BATTERY_LOAD_PROFILE_W = "battery_load_profile_w"
CONF_INVERTER_FORCE_CHARGE_ENABLE_FEEDBACK_ENTITY = "inverter_force_charge_enable_feedback_entity"
CONF_INVERTER_FORCE_CHARGE_MODE_FEEDBACK_ENTITY = "inverter_force_charge_mode_feedback_entity"
CONF_INVERTER_FORCE_CHARGE_POWER_FEEDBACK_ENTITY = "inverter_force_charge_power_feedback_entity"
CONF_BATTERY_HOLD_ENTITY = "battery_hold_entity"
CONF_BATTERY_HOLD_ENGAGE_VALUE = "battery_hold_engage_value"
CONF_BATTERY_HOLD_RELEASE_VALUE = "battery_hold_release_value"
CONF_BATTERY_HOLD_FEEDBACK_ENTITY = "battery_hold_feedback_entity"
CONF_BATTERY_HOLD_FEEDBACK_ENGAGE_VALUE = "battery_hold_feedback_engage_value"
CONF_BATTERY_HOLD_FEEDBACK_RELEASE_VALUE = "battery_hold_feedback_release_value"
CONF_BATTERY_HOLD_VERIFIED = "battery_hold_verified"

DEFAULT_BATTERY_GRID_TARGET_SOC = 80.0
DEFAULT_BATTERY_ROUNDTRIP_EFFICIENCY = 0.85
DEFAULT_BATTERY_WEAR_COST_PER_KWH = 0.0
DEFAULT_BATTERY_SOC_HYSTERESIS = 2.0
DEFAULT_BATTERY_INPUT_MAX_AGE_SECONDS = 300
DEFAULT_BATTERY_FORECAST_MAX_AGE_SECONDS = 21600

CONF_EXPORT_LIMIT = "export_limit"
CONF_CONTROLLER_INTERVAL = "controller_interval"
CONF_PLANNER_INTERVAL = "planner_interval"
CONF_NOTIFICATION_SERVICE = "notification_service"
CONF_ENABLE_PREEMPTION = "enable_preemption"

# Appliance subentry config keys
CONF_APPLIANCE_NAME = "appliance_name"
CONF_APPLIANCE_ENTITY = "appliance_entity"
CONF_APPLIANCE_PRIORITY = "appliance_priority"
CONF_NOMINAL_POWER = "nominal_power"
CONF_ACTUAL_POWER_ENTITY = "actual_power_entity"
CONF_PHASES = "phases"
CONF_DYNAMIC_CURRENT = "dynamic_current"
CONF_CURRENT_ENTITY = "current_entity"
CONF_MIN_CURRENT = "min_current"
CONF_MAX_CURRENT = "max_current"
CONF_EV_SOC_ENTITY = "ev_soc_entity"
CONF_EV_CONNECTED_ENTITY = "ev_connected_entity"
CONF_IS_BIG_CONSUMER = "is_big_consumer"
CONF_BATTERY_DISCHARGE_OVERRIDE = "battery_discharge_override"
CONF_ON_ONLY = "on_only"
CONF_MIN_DAILY_RUNTIME = "min_daily_runtime"
CONF_MAX_DAILY_RUNTIME = "max_daily_runtime"
CONF_MAX_DAILY_ACTIVATIONS = "max_daily_activations"
CONF_SCHEDULE_DEADLINE = "schedule_deadline"
CONF_START_AFTER = "start_after"
CONF_END_BEFORE = "end_before"
CONF_NOTIFY_APPLIANCE_ON = "notify_appliance_on"
CONF_NOTIFY_APPLIANCE_OFF = "notify_appliance_off"
CONF_NOTIFY_DAILY_SUMMARY = "notify_daily_summary"
CONF_SWITCH_INTERVAL = "switch_interval"
CONF_AVERAGING_WINDOW = "averaging_window"
CONF_REQUIRES_APPLIANCE = "requires_appliance"
CONF_HELPER_ONLY = "helper_only"
CONF_PLAN_INFLUENCE = "plan_influence"
CONF_EV_TARGET_SOC = "ev_target_soc"
CONF_ALLOW_GRID_SUPPLEMENT = "allow_grid_supplement"
CONF_MAX_GRID_POWER = "max_grid_power"
CONF_CHEAP_GRID_TARGET_CURRENT = "cheap_grid_target_current"
CONF_PROTECT_FROM_PREEMPTION = "protect_from_preemption"
CONF_CURRENT_STEP = "current_step"
CONF_ON_THRESHOLD = "on_threshold"
CONF_COMPLETION_POWER_THRESHOLD = "completion_power_threshold"
CONF_OFF_THRESHOLD = "off_threshold"

# Dynamic battery charging
CONF_DYNAMIC_BATTERY_CHARGE_ENABLED = "dynamic_battery_charge_enabled"
CONF_INVERTER_BATTERY_MAX_CHARGE_POWER_ENTITY = "inverter_battery_max_charge_power_entity"
CONF_BATTERY_MAX_CHARGE_POWER_W = "battery_max_charge_power_w"
CONF_BATTERY_TRICKLE_CHARGE_POWER_W = "battery_trickle_charge_power_w"

DEFAULT_BATTERY_TRICKLE_CHARGE_POWER_W = 100

# Current command controls and timestamp history
CONF_CURRENT_UPDATE_INTERVAL = "current_update_interval"
CONF_CURRENT_MIN_CHANGE = "current_min_change"
MAX_AVERAGING_WINDOW = 1800

CONF_ENABLE_CONDITION_ENTITY = "enable_condition_entity"
CONF_ENABLE_CONDITION_MODE = "enable_condition_mode"
CONF_START_DELAY = "start_delay"
CONF_PHASE_COUNT_ENTITY = "phase_count_entity"

CONF_REMAINING_RUNTIME_ENTITY = "remaining_runtime_entity"
CONF_REQUIRE_CONTIGUOUS_RUNTIME = "require_contiguous_runtime"
CONF_ADDITIONAL_FORECAST_SENSORS = "additional_forecast_sensors"
CONF_ADDITIONAL_FORECAST_TOMORROW_SENSORS = "additional_forecast_tomorrow_sensors"
