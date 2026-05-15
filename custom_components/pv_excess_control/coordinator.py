"""DataUpdateCoordinator for PV Excess Control.

Central data hub that:
1. Collects sensor states from Home Assistant
2. Maintains a rolling power history buffer
3. Runs the optimizer on each update cycle
4. Runs the planner on a slower cadence
5. Applies control decisions to HA entities
"""
from __future__ import annotations

import asyncio
import logging
import math
import time as _time
from datetime import datetime, time, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_ALLOW_GRID_CHARGING,
    CONF_APPLIANCE_ENTITY,
    CONF_AVERAGING_WINDOW,
    CONF_APPLIANCE_NAME,
    CONF_APPLIANCE_PRIORITY,
    CONF_ACTUAL_POWER_ENTITY,
    CONF_BATTERY_CAPACITY,
    CONF_INVERTER_TYPE,
    CONF_BATTERY_MAX_DISCHARGE_DEFAULT,
    CONF_BATTERY_MAX_DISCHARGE_ENTITY,
    CONF_MIN_BATTERY_SOC,
    CONF_BATTERY_CHARGE_PRICE_THRESHOLD,
    CONF_BATTERY_DISCHARGE_OVERRIDE,
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
    CONF_IS_BIG_CONSUMER,
    CONF_LOAD_POWER,
    CONF_MAX_CURRENT,
    CONF_MAX_DAILY_ACTIVATIONS,
    CONF_MAX_DAILY_RUNTIME,
    CONF_MAX_GRID_POWER,
    CONF_CHEAP_GRID_TARGET_CURRENT,
    CONF_COMPLETION_POWER_THRESHOLD,
    CONF_OFF_THRESHOLD,
    CONF_ON_THRESHOLD,
    CONF_MIN_CURRENT,
    CONF_MIN_DAILY_RUNTIME,
    CONF_NOMINAL_POWER,
    CONF_NOTIFICATION_SERVICE,
    CONF_NOTIFY_APPLIANCE_OFF,
    CONF_NOTIFY_APPLIANCE_ON,
    CONF_NOTIFY_DAILY_SUMMARY,
    DEFAULT_NOTIFICATION_SETTINGS,
    NotificationEvent,
    CONF_ON_ONLY,
    CONF_PHASES,
    CONF_PLAN_INFLUENCE,
    CONF_PLANNER_INTERVAL,
    CONF_PROTECT_FROM_PREEMPTION,
    CONF_PRICE_SENSOR,
    CONF_PV_POWER,
    CONF_REQUIRES_APPLIANCE,
    CONF_HELPER_ONLY,
    CONF_SCHEDULE_DEADLINE,
    CONF_START_AFTER,
    CONF_END_BEFORE,
    CONF_SWITCH_INTERVAL,
    CONF_TARIFF_PROVIDER,
    CONF_ALLOW_GRID_SUPPLEMENT,
    DEFAULT_CONTROLLER_INTERVAL,
    DEFAULT_GRID_VOLTAGE,
    DEFAULT_OFF_THRESHOLD,
    DEFAULT_PLANNER_INTERVAL,
    DEFAULT_STARTUP_GRACE_PERIOD,
    DEFAULT_SWITCH_INTERVAL,
    DOMAIN,
    BatteryStrategy,
    PlanInfluence,
    TariffProvider as TariffProviderEnum,
    ForecastProvider as ForecastProviderEnum,
    CONF_AUTO_BATTERY_GRID_CHARGE,
    CONF_BATTERY_GRID_CHARGE_POWER_W,
    CONF_GRID_CHARGE_ENGAGE_MIN_DURATION_MINUTES,
    CONF_INVERTER_FORCE_CHARGE_ENABLE_ENTITY,
    CONF_INVERTER_FORCE_CHARGE_ENABLE_ENGAGE_VALUE,
    CONF_INVERTER_FORCE_CHARGE_ENABLE_DISENGAGE_VALUE,
    CONF_INVERTER_FORCE_CHARGE_MODE_ENTITY,
    CONF_INVERTER_FORCE_CHARGE_MODE_ENGAGE_VALUE,
    CONF_INVERTER_FORCE_CHARGE_MODE_DISENGAGE_VALUE,
    CONF_INVERTER_FORCE_CHARGE_POWER_ENTITY,
    DEFAULT_GRID_CHARGE_ENGAGE_MIN_DURATION_MINUTES,
)
from .energy import create_tariff_provider
from .forecast import create_forecast_provider
from .models import (
    Action,
    ApplianceConfig,
    ApplianceState,
    BatteryConfig,
    BatteryDischargeAction,
    ControlDecision,
    ForecastData,
    InverterGridChargeConfig,
    OptimizerResult,
    Plan,
    PowerState,
    TariffInfo,
)
from .analytics import AnalyticsTracker
from .inverter_control import InverterGridChargeController
from .notifications import NotificationManager
from .optimizer import Optimizer
from .planner import Planner

_LOGGER = logging.getLogger(__name__)

_OFF_STATES = {"off", "false", "False", "0"}
_UNAVAILABLE_STATES = {STATE_UNAVAILABLE, STATE_UNKNOWN, "none", ""}

# Maximum number of power history entries to keep (~30 min at 30s intervals)
MAX_HISTORY_SIZE = 60

# Multipliers to normalise power values to watts.
_POWER_UNIT_MULTIPLIERS: dict[str, float] = {
    "w": 1.0,
    "kw": 1000.0,
    "mw": 1_000_000.0,
}


def _normalise_power(value: float, unit: str | None) -> float:
    """Convert a power reading to watts based on its unit_of_measurement."""
    if unit is None:
        return value
    return value * _POWER_UNIT_MULTIPLIERS.get(unit.lower().strip(), 1.0)


def _parse_sensor_float(
    hass: HomeAssistant,
    entity_id: str | None,
    *,
    power: bool = False,
) -> float | None:
    """Read a numeric sensor value, returning None if unavailable.

    When *power* is True the value is normalised to watts using the
    sensor's ``unit_of_measurement`` attribute (kW → W, MW → W).
    """
    if entity_id is None:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in _UNAVAILABLE_STATES:
        return None
    try:
        val = float(state.state)
        if math.isnan(val) or math.isinf(val):
            return None
    except (ValueError, TypeError):
        return None
    if power:
        val = _normalise_power(val, state.attributes.get("unit_of_measurement"))
    return val


def _parse_sensor_bool(hass: HomeAssistant, entity_id: str | None) -> bool | None:
    """Read a boolean sensor / binary_sensor value."""
    if entity_id is None:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in _UNAVAILABLE_STATES:
        return None
    return state.state in ("on", "true", "True", "1")


def _entity_state_dict(hass: HomeAssistant, entity_id: str) -> dict | None:
    """Build an HA-agnostic state dict for tariff/forecast providers."""
    state = hass.states.get(entity_id)
    if state is None:
        return None
    return {
        "state": state.state,
        "attributes": dict(state.attributes),
    }


def _parse_time_string(value: str | None) -> time | None:
    """Parse a time string like '16:00' into a time object."""
    if value is None:
        return None
    try:
        parts = value.split(":")
        return time(int(parts[0]), int(parts[1]))
    except (ValueError, IndexError, TypeError):
        return None


class PvExcessCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for PV Excess Control.

    Periodically reads sensor data, runs the optimizer and planner,
    and applies control decisions.
    """

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        controller_interval = config_entry.data.get(
            CONF_CONTROLLER_INTERVAL, DEFAULT_CONTROLLER_INTERVAL
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=config_entry,
            update_interval=timedelta(seconds=controller_interval),
        )

        grid_voltage = config_entry.data.get(CONF_GRID_VOLTAGE, DEFAULT_GRID_VOLTAGE)
        tz_name = str(hass.config.time_zone) if hasattr(hass.config, 'time_zone') else "UTC"
        enable_preemption = config_entry.data.get(CONF_ENABLE_PREEMPTION, True)
        off_threshold = config_entry.data.get(CONF_OFF_THRESHOLD, DEFAULT_OFF_THRESHOLD)
        self.optimizer = Optimizer(
            grid_voltage=grid_voltage,
            timezone_str=tz_name,
            enable_preemption=enable_preemption,
            off_threshold=off_threshold,
        )
        self.planner = Planner(grid_voltage=grid_voltage, timezone_str=tz_name)

        # State
        # Note: power_history resets on reload. The startup grace period
        # (DEFAULT_STARTUP_GRACE_PERIOD = 120s) protects against acting on
        # insufficient data -- ~4 cycles at 30s is enough to rebuild history.
        self.power_history: list[PowerState] = []
        # Tracks per-required-sensor availability between cycles so that
        # transition events (available -> unavailable and vice versa) can
        # be logged exactly once per transition. Keys are entity_ids;
        # values are True (available) or False (unavailable). A missing
        # key means "no prior observation" which is treated as a transition
        # from unknown to current state on the first observed cycle.
        self._last_sensor_available: dict[str, bool] = {}
        self._last_appliance_configs: list[ApplianceConfig] = []
        self.current_plan: Plan | None = None
        self.appliance_states: dict[str, ApplianceState] = {}
        self.control_decisions: list[ControlDecision] = []
        self.battery_discharge_action: BatteryDischargeAction | None = None

        # Plan influence mode
        self._plan_influence = config_entry.data.get(
            CONF_PLAN_INFLUENCE, PlanInfluence.LIGHT
        )

        # Planner cadence
        self._planner_interval = config_entry.data.get(
            CONF_PLANNER_INTERVAL, DEFAULT_PLANNER_INTERVAL
        )
        self._planner_counter = 0

        # Master switch & startup
        self._was_enabled = True  # Track master-switch transitions (M11)
        self._startup_time = datetime.now()
        _LOGGER.info(
            "Startup grace period active for %ds (optimization paused while history buffer fills)",
            DEFAULT_STARTUP_GRACE_PERIOD,
        )
        self._enabled = config_entry.data.get("control_enabled", True)
        self._last_tariff_info: TariffInfo | None = None

        # Runtime-writable control state (entity-driven)
        self.force_charge: bool = config_entry.data.get("force_charge", False)

        # Inverter forced grid-charge state machine
        self._inverter_ctl: InverterGridChargeController | None = self._build_inverter_controller()
        self._grid_charge_engaged: bool = config_entry.data.get("_grid_charge_engaged", False)
        self._grid_charge_engage_ts: float | None = None
        self._force_charge_prev: bool = self.force_charge
        self._latest_tariff = None
        self._latest_power_state = None

        # Restore persisted enabled/override state from config_entry.data
        disabled_ids = set(config_entry.data.get("disabled_appliances", []))
        overridden_ids = set(config_entry.data.get("overridden_appliances", []))
        self.appliance_enabled: dict[str, bool] = {
            aid: False for aid in disabled_ids
        }
        self.appliance_overrides: dict[str, bool] = {
            aid: True for aid in overridden_ids
        }
        self.appliance_priorities: dict[str, int] = {}
        self.appliance_min_daily_runtime: dict[str, int | None] = {}
        self.appliance_max_daily_runtime: dict[str, int | None] = {}

        # Initialize runtime priorities and runtime limits from saved subentry data
        subentries = getattr(config_entry, "subentries", {})
        for subentry_id, subentry in subentries.items():
            d = subentry.data
            saved_priority = d.get(CONF_APPLIANCE_PRIORITY, 500)
            self.appliance_priorities[subentry_id] = saved_priority
            # Seed only when the key exists; absence means "no override" —
            # the read path falls through to subentry.data.
            if CONF_MIN_DAILY_RUNTIME in d:
                self.appliance_min_daily_runtime[subentry_id] = d[CONF_MIN_DAILY_RUNTIME]
            if CONF_MAX_DAILY_RUNTIME in d:
                self.appliance_max_daily_runtime[subentry_id] = d[CONF_MAX_DAILY_RUNTIME]

        # Track last-set battery discharge limit to avoid redundant calls.
        # Seed from actual entity value on startup to avoid unnecessary service calls.
        self._last_discharge_limit: float | None = None
        discharge_entity = config_entry.data.get(CONF_BATTERY_MAX_DISCHARGE_ENTITY)
        if discharge_entity:
            current_val = _parse_sensor_float(hass, discharge_entity, power=True)
            if current_val is not None:
                self._last_discharge_limit = current_val

        # Track last state change time per appliance for switch interval enforcement
        self._last_state_change: dict[str, datetime] = {}

        # Track last applied current per appliance for deduplication (H3)
        self._last_applied_current: dict[str, float] = {}

        # Track daily activation count per appliance (OFF→ON transitions)
        self._activations_today: dict[str, int] = {}

        # Track which appliances are referenced in another appliance's
        # requires_appliance (derived from configs each cycle). Appliances
        # in this set bypass the switch-interval cooldown — they may need to
        # respond promptly to their dependents' state transitions.
        self._needed_by_others: set[str] = set()

        # Track previous cycle's is_on state per appliance for physical
        # transition detection. Used to increment activations_today on
        # off→on transitions instead of on service-call intent — protects
        # against devices that accept the command but fail to physically
        # engage. See 2026-04-09-helper-only-hardening-design.md Bug A.
        self._previous_is_on: dict[str, bool] = {}

        # Analytics tracker
        self.analytics = AnalyticsTracker(
            feed_in_tariff=config_entry.data.get(CONF_FEED_IN_TARIFF, 0.0),
            normal_import_price=0.25,
        )

        # Notification manager — build settings from config
        notification_service = config_entry.data.get(CONF_NOTIFICATION_SERVICE)
        notification_settings = dict(DEFAULT_NOTIFICATION_SETTINGS)
        notification_settings[NotificationEvent.APPLIANCE_ON] = config_entry.data.get(
            CONF_NOTIFY_APPLIANCE_ON, True
        )
        notification_settings[NotificationEvent.APPLIANCE_OFF] = config_entry.data.get(
            CONF_NOTIFY_APPLIANCE_OFF, True
        )
        notification_settings[NotificationEvent.DAILY_SUMMARY] = config_entry.data.get(
            CONF_NOTIFY_DAILY_SUMMARY, True
        )
        self.notifications = NotificationManager(
            hass, notification_settings=notification_settings,
            notification_service=notification_service,
        )

        # Battery strategy (runtime override; defaults to config value)
        strategy_str = config_entry.data.get(
            CONF_BATTERY_STRATEGY, BatteryStrategy.BALANCED
        )
        try:
            self.battery_strategy: str = BatteryStrategy(strategy_str)
        except ValueError:
            self.battery_strategy = BatteryStrategy.BALANCED

        # Tariff provider
        tariff_type = config_entry.data.get(
            CONF_TARIFF_PROVIDER, TariffProviderEnum.NONE
        )
        price_entity = config_entry.data.get(CONF_PRICE_SENSOR, "")
        if tariff_type != TariffProviderEnum.NONE and not price_entity:
            _LOGGER.warning(
                "Tariff provider '%s' configured but no price_sensor entity set",
                tariff_type,
            )
        self._tariff_provider = create_tariff_provider(tariff_type, price_entity, timezone_str=tz_name)

        # Forecast provider
        forecast_type = config_entry.data.get(
            CONF_FORECAST_PROVIDER, ForecastProviderEnum.NONE
        )
        forecast_entity = config_entry.data.get(CONF_FORECAST_SENSOR, "")
        if forecast_type != ForecastProviderEnum.NONE and forecast_entity:
            self._forecast_provider = create_forecast_provider(
                forecast_type, forecast_entity
            )
        elif forecast_type != ForecastProviderEnum.NONE and not forecast_entity:
            _LOGGER.warning("Forecast provider '%s' configured but no forecast_sensor entity set", forecast_type)
            self._forecast_provider = None
        else:
            self._forecast_provider = None

        _LOGGER.info(
            "PV Excess Control initialized: inverter=%s, voltage=%sV, "
            "tariff=%s, forecast=%s, controller_interval=%ss, planner_interval=%ss",
            config_entry.data.get(CONF_INVERTER_TYPE, "?"),
            config_entry.data.get(CONF_GRID_VOLTAGE, "?"),
            config_entry.data.get(CONF_TARIFF_PROVIDER, "none"),
            config_entry.data.get(CONF_FORECAST_PROVIDER, "none"),
            controller_interval,
            self._planner_interval,
        )

    # ------------------------------------------------------------------
    # Inverter grid-charge helpers (Task 10 plumbing)
    # ------------------------------------------------------------------

    def _build_inverter_controller(self) -> InverterGridChargeController | None:
        """Construct the inverter controller from config_entry.data, or return None."""
        d = self.config_entry.data
        enable_entity = d.get(CONF_INVERTER_FORCE_CHARGE_ENABLE_ENTITY)
        if not enable_entity:
            return None
        cfg = InverterGridChargeConfig(
            enable_entity_id=enable_entity,
            enable_engage_value=d.get(CONF_INVERTER_FORCE_CHARGE_ENABLE_ENGAGE_VALUE, ""),
            enable_disengage_value=d.get(CONF_INVERTER_FORCE_CHARGE_ENABLE_DISENGAGE_VALUE, ""),
            mode_entity_id=d.get(CONF_INVERTER_FORCE_CHARGE_MODE_ENTITY),
            mode_engage_value=d.get(CONF_INVERTER_FORCE_CHARGE_MODE_ENGAGE_VALUE),
            mode_disengage_value=d.get(CONF_INVERTER_FORCE_CHARGE_MODE_DISENGAGE_VALUE),
            power_entity_id=d.get(CONF_INVERTER_FORCE_CHARGE_POWER_ENTITY),
        )
        try:
            return InverterGridChargeController(self.hass, cfg)
        except ValueError as err:
            _LOGGER.error("Inverter grid-charge controller misconfigured: %s", err)
            return None

    def _persist_grid_charge_state(self, engaged: bool) -> None:
        """Persist the engagement flag to config_entry.data via async_update_entry.

        Uses the runtime-state-key bypass so this does not trigger a reload.
        """
        new_data = dict(self.config_entry.data)
        new_data["_grid_charge_engaged"] = engaged
        self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)

    def auto_should_engage_now(self) -> bool:
        """Evaluate the auto-engage gate against the latest snapshots."""
        d = self.config_entry.data
        if not d.get(CONF_AUTO_BATTERY_GRID_CHARGE, False):
            return False
        if self._latest_tariff is None or self._latest_power_state is None:
            return False
        target_soc = d.get(CONF_BATTERY_TARGET_SOC, 80)
        cheap_now = self._latest_tariff.current_price <= self._latest_tariff.battery_charge_price_threshold
        soc = self._latest_power_state.battery_soc
        soc_below_target = soc is None or soc < target_soc
        return cheap_now and soc_below_target

    async def _run_grid_charge_state_machine(
        self, tariff_info, power_state,
    ) -> None:
        """Engage / disengage forced grid charge based on price + SoC + force_charge.

        Idempotent. Safe to call without _inverter_ctl.
        """
        d = self.config_entry.data
        power_w = d.get(CONF_BATTERY_GRID_CHARGE_POWER_W)
        if self._inverter_ctl is None or power_w is None:
            return  # nothing to drive

        auto_flag = d.get(CONF_AUTO_BATTERY_GRID_CHARGE, False)
        target_soc = d.get(CONF_BATTERY_TARGET_SOC, 80)
        min_dur_s = d.get(
            CONF_GRID_CHARGE_ENGAGE_MIN_DURATION_MINUTES,
            DEFAULT_GRID_CHARGE_ENGAGE_MIN_DURATION_MINUTES,
        ) * 60

        cheap_now = (
            tariff_info is not None
            and tariff_info.current_price <= tariff_info.battery_charge_price_threshold
        )
        soc = getattr(power_state, "battery_soc", None) if power_state is not None else None
        soc_below_target = soc is None or soc < target_soc

        auto_should_engage = (
            auto_flag
            and cheap_now
            and soc_below_target
            and (
                # If the planner has run, only grid-charge when it says solar
                # forecast alone is insufficient to reach the battery target.
                # This prevents unnecessary grid imports on sunny days.
                self.current_plan is None
                or self.current_plan.grid_charge_recommended
            )
        )
        should_engage = self.force_charge or auto_should_engage

        force_off_edge = self._force_charge_prev and not self.force_charge
        self._force_charge_prev = self.force_charge

        if should_engage and not self._grid_charge_engaged:
            await self._inverter_ctl.engage(power_w)
            self._grid_charge_engaged = True
            self._grid_charge_engage_ts = _time.monotonic()
            self._persist_grid_charge_state(True)
            method = getattr(self.notifications, "notify_battery_grid_charge_engaged", None)
            if method is not None:
                try:
                    await method(power_w)
                except Exception:
                    _LOGGER.exception("Failed to send grid_charge_engaged notification")

        elif (not should_engage) and self._grid_charge_engaged:
            elapsed = _time.monotonic() - (self._grid_charge_engage_ts or 0.0)
            if elapsed >= min_dur_s or force_off_edge:
                await self._inverter_ctl.disengage()
                self._grid_charge_engaged = False
                self._grid_charge_engage_ts = None
                self._persist_grid_charge_state(False)
                reason = (
                    "manual force_charge switch off" if force_off_edge
                    else "price above threshold or SoC reached target"
                )
                method = getattr(self.notifications, "notify_battery_grid_charge_disengaged", None)
                if method is not None:
                    try:
                        await method(reason)
                    except Exception:
                        _LOGGER.exception("Failed to send grid_charge_disengaged notification")

    @property
    def enabled(self) -> bool:
        """Return whether the controller is enabled."""
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        """Set whether the controller is enabled."""
        self._enabled = value

    def reset_daily(self) -> None:
        """Reset daily counters at midnight.

        Creates new ApplianceState objects and a new dict instead of modifying
        in-place to avoid RuntimeError if _async_update_data is iterating the
        dict concurrently.
        """
        new_states: dict[str, ApplianceState] = {}
        for key, state in self.appliance_states.items():
            new_states[key] = ApplianceState(
                appliance_id=state.appliance_id,
                is_on=state.is_on,
                current_power=state.current_power,
                current_amperage=state.current_amperage,
                runtime_today=timedelta(),
                energy_today=0.0,
                last_state_change=state.last_state_change,
                ev_connected=state.ev_connected,
                ev_soc=state.ev_soc,
                activations_today=0,
            )
        self.appliance_states = new_states  # Atomic replacement
        # Only clear switch interval for OFF appliances; ON appliances keep protection
        new_last_change = {}
        for key, state in new_states.items():
            if state.is_on and key in self._last_state_change:
                new_last_change[key] = self._last_state_change[key]
        self._last_state_change = new_last_change
        self._activations_today.clear()
        self.analytics.reset_daily()
        _LOGGER.info("Midnight reset: cleared daily runtime, energy, activations, and analytics counters (ON appliances keep switch interval protection)")

    # ------------------------------------------------------------------
    # Main update loop
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Main control loop, called every controller_interval seconds.

        Steps: collect power state -> append to history -> run planner (on cadence) ->
        check enabled/grace period -> get appliance configs/states -> get tariff ->
        handle force charge -> run optimizer -> record analytics -> apply decisions ->
        send notifications.
        """
        # 1. Collect power state from sensors
        power_state = self._collect_power_state()
        def _fmt(val: float | None, suffix: str = "W") -> str:
            return f"{val:.0f}{suffix}" if val is not None else "unavailable"

        _LOGGER.debug(
            "Cycle: PV=%s grid_export=%s grid_import=%s "
            "load=%s battery_soc=%s battery_power=%s excess=%s",
            _fmt(power_state.pv_production),
            _fmt(power_state.grid_export),
            _fmt(power_state.grid_import),
            _fmt(power_state.load_power),
            _fmt(power_state.battery_soc, "%"),
            _fmt(power_state.battery_power),
            _fmt(power_state.excess_power),
        )

        # 2. Append to history (keep last MAX_HISTORY_SIZE entries)
        self.power_history.append(power_state)
        if len(self.power_history) > MAX_HISTORY_SIZE:
            self.power_history.pop(0)

        # 3. Run planner on its interval
        self._planner_counter += 1
        planner_ratio = max(
            1,
            int(self._planner_interval // self.update_interval.total_seconds()),
        )
        if self._planner_counter >= planner_ratio:
            self._planner_counter = 0
            await self._run_planner()

        # 4. Skip optimizer if disabled or in startup grace period
        if not self._enabled:
            _LOGGER.debug("Controller disabled, skipping optimization")
            # M11: Turn off all managed appliances on the transition to disabled
            if self._was_enabled:
                await self._turn_off_all_managed()
            self._was_enabled = False
            return self._build_coordinator_data()

        self._was_enabled = True  # Mark as enabled for M11 transition detection

        # 5. Get appliance configs and states early so runtime/energy tracking
        # works even during the startup grace period (M20)
        appliance_configs = self._get_appliance_configs()
        self._last_appliance_configs = appliance_configs
        appliance_states = self._get_appliance_states(appliance_configs)

        elapsed = (datetime.now() - self._startup_time).total_seconds()
        if elapsed < DEFAULT_STARTUP_GRACE_PERIOD:
            _LOGGER.debug(
                "Startup grace period (%ds remaining), skipping optimization",
                int(DEFAULT_STARTUP_GRACE_PERIOD - elapsed),
            )
            return self._build_coordinator_data()

        # 6. Get tariff info
        try:
            tariff_info = self._get_tariff_info()
        except Exception as err:
            _LOGGER.warning("Tariff provider error, using defaults: %s", err)
            tariff_info = TariffInfo(
                current_price=float("inf"),
                feed_in_tariff=0.0,
                cheap_price_threshold=0.0,
                battery_charge_price_threshold=0.0,
            )
        self._last_tariff_info = tariff_info
        _LOGGER.debug(
            "Tariff: price=%.4f feed_in=%.4f cheap_threshold=%.4f is_cheap=%s windows=%d",
            tariff_info.current_price,
            tariff_info.feed_in_tariff,
            tariff_info.cheap_price_threshold,
            tariff_info.current_price <= tariff_info.cheap_price_threshold,
            len(tariff_info.windows),
        )

        # Cache the latest snapshots so the snappy switch handler can re-evaluate
        self._latest_tariff = tariff_info
        self._latest_power_state = power_state

        # Run the inverter forced grid-charge state machine
        await self._run_grid_charge_state_machine(tariff_info, power_state)

        # 7. Build empty plan if none exists
        plan = self.current_plan or self._create_empty_plan()

        # 7b. Force charge: zero out excess for optimizer only (don't corrupt real history).
        # Note on on_only interaction: force_charge zeroes excess so the ALLOCATE phase
        # won't start new appliances (correct -- force_charge prioritises the battery).
        # Already-ON on_only appliances are protected because the optimizer's on_only
        # check (returns ON before the excess check) fires first, and the SHED phase
        # never sheds on_only appliances. So on_only semantics ("don't turn off once
        # started") are preserved during force_charge.
        if self.force_charge:
            _LOGGER.info("Force charge active: setting large negative excess to trigger shedding")
            power_state_for_optimizer = PowerState(
                pv_production=power_state.pv_production,
                grid_export=power_state.grid_export,
                grid_import=power_state.grid_import,
                load_power=power_state.load_power,
                excess_power=-10000.0,
                battery_soc=power_state.battery_soc,
                battery_power=power_state.battery_power,
                ev_soc=power_state.ev_soc,
                timestamp=power_state.timestamp,
            )
            history_for_optimizer = [
                PowerState(
                    pv_production=ps.pv_production,
                    grid_export=ps.grid_export,
                    grid_import=ps.grid_import,
                    load_power=ps.load_power,
                    excess_power=-10000.0,
                    battery_soc=ps.battery_soc,
                    battery_power=ps.battery_power,
                    ev_soc=ps.ev_soc,
                    timestamp=ps.timestamp,
                )
                for ps in self.power_history
            ]
        else:
            power_state_for_optimizer = power_state
            history_for_optimizer = self.power_history

        # Refresh plan_influence and grid_voltage from config each cycle (H12)
        self._plan_influence = self.config_entry.data.get(CONF_PLAN_INFLUENCE, PlanInfluence.LIGHT)
        grid_voltage = self.config_entry.data.get(CONF_GRID_VOLTAGE, DEFAULT_GRID_VOLTAGE)
        self.optimizer.grid_voltage = grid_voltage
        min_battery_soc = self.config_entry.data.get(CONF_MIN_BATTERY_SOC)

        # 8. Run optimizer
        try:
            result = self.optimizer.optimize(
                power_state=power_state_for_optimizer,
                appliances=appliance_configs,
                appliance_states=list(appliance_states.values()),
                plan=plan,
                power_history=history_for_optimizer,
                tariff=tariff_info,
                plan_influence=self._plan_influence,
                min_battery_soc=min_battery_soc,
                force_charge=self.force_charge,
                auto_grid_charge_engaged=self._grid_charge_engaged,
            )
        except Exception as err:
            _LOGGER.error("Optimizer error: %s", err)
            raise UpdateFailed(f"Optimizer error: {err}") from err

        self.control_decisions = result.decisions
        self.battery_discharge_action = result.battery_discharge_action

        on_count = sum(1 for d in result.decisions if d.action == Action.ON)
        off_count = sum(1 for d in result.decisions if d.action == Action.OFF)
        set_count = sum(1 for d in result.decisions if d.action == Action.SET_CURRENT)
        idle_count = sum(1 for d in result.decisions if d.action == Action.IDLE)
        _LOGGER.debug(
            "Optimizer: %d decisions (ON=%d OFF=%d SET_CURRENT=%d IDLE=%d) "
            "discharge_limit=%s",
            len(result.decisions), on_count, off_count, set_count, idle_count,
            result.battery_discharge_action.max_discharge_watts
            if result.battery_discharge_action.should_limit else "none",
        )
        for d in result.decisions:
            _LOGGER.debug("  %s -> %s: %s", d.appliance_id[:12], d.action, d.reason)

        # 9. Record analytics based on DECISIONS and current power state
        # (before applying decisions, so we capture the optimizer's view of the world)
        # Update analytics tariff values each cycle to keep them current
        self.analytics.feed_in_tariff = tariff_info.feed_in_tariff
        if tariff_info.current_price > tariff_info.cheap_price_threshold and not math.isinf(tariff_info.current_price):
            self.analytics.normal_import_price = tariff_info.current_price
        cycle_seconds = self.update_interval.total_seconds()
        for decision in result.decisions:
            if decision.action in (Action.ON, Action.SET_CURRENT):
                # H14: Skip disabled appliances to avoid phantom energy recording
                if not self.appliance_enabled.get(decision.appliance_id, True):
                    continue
                config = self._get_appliance_config_by_id(decision.appliance_id)
                if config is None:
                    continue
                # Use actual power if available, otherwise nominal
                app_state = appliance_states.get(decision.appliance_id)
                power = (app_state.current_power if app_state and app_state.current_power > 0
                         else config.nominal_power if config else 0)
                # M9: Use decision reason to correctly attribute grid-supplemented consumption
                if "grid supplement" in decision.reason.lower():
                    source = "cheap_tariff"
                elif power_state.excess_power is not None and power_state.excess_power > 0:
                    source = "solar"
                elif tariff_info.current_price <= tariff_info.cheap_price_threshold:
                    source = "cheap_tariff"
                else:
                    source = "grid"
                self.analytics.record_cycle(
                    decision.appliance_id, power, cycle_seconds,
                    source, tariff_info.current_price,
                )
        if power_state.pv_production is not None:
            self.analytics.record_solar_production(
                power_state.pv_production, cycle_seconds,
            )
        if power_state.grid_export is not None and power_state.grid_export > 0:
            self.analytics.record_grid_export(
                power_state.grid_export, cycle_seconds,
            )

        # 10. Apply decisions (call HA services)
        applied_ids = await self._apply_decisions(result)

        # 11. Send notifications on state changes (only for successfully applied decisions)
        for decision in result.decisions:
            if decision.appliance_id not in applied_ids:
                continue  # Service call failed or was skipped
            config = self._get_appliance_config_by_id(decision.appliance_id)
            if config is None:
                continue
            prev_state = appliance_states.get(decision.appliance_id)
            if prev_state is None:
                continue
            if decision.action in (Action.ON, Action.SET_CURRENT) and not prev_state.is_on:
                await self.notifications.notify_appliance_on(
                    config.name, decision.reason, config.nominal_power,
                )
            elif decision.action == Action.OFF and prev_state.is_on:
                await self.notifications.notify_appliance_off(
                    config.name, decision.reason,
                )

        return self._build_coordinator_data()

    # ------------------------------------------------------------------
    # Power state collection
    # ------------------------------------------------------------------

    def _track_sensor_availability(
        self,
        entity_id: str | None,
        value: float | None,
    ) -> None:
        """Log WARNING on available→unavailable transition, INFO on recovery.

        Called from _collect_power_state for each required sensor. The
        _last_sensor_available dict on the coordinator tracks per-sensor
        state between cycles. A missing key means no prior observation:
        on that first observation, if the sensor is unavailable we log
        a warning (treating 'unknown prior state' as a transition from
        available).
        """
        if entity_id is None:
            return
        is_available = value is not None
        previous = self._last_sensor_available.get(entity_id)
        if previous is None:
            # First observation of this sensor. If it is unavailable on
            # the very first cycle (e.g., HA restarted while a sensor
            # was already down), treat that as a transition so the
            # operator sees it in the log.
            if not is_available:
                _LOGGER.warning(
                    "Required sensor %s is unavailable — excess calculation paused",
                    entity_id,
                )
            self._last_sensor_available[entity_id] = is_available
            return
        if previous and not is_available:
            _LOGGER.warning(
                "Required sensor %s became unavailable — excess calculation paused",
                entity_id,
            )
        elif not previous and is_available:
            _LOGGER.info(
                "Sensor %s is available again",
                entity_id,
            )
        self._last_sensor_available[entity_id] = is_available

    def _collect_power_state(self) -> PowerState:
        """Read power sensor entities and build a PowerState snapshot.

        Sensor-backed fields are ``float | None``: ``None`` signals
        that the underlying HA sensor was ``unavailable`` at sample
        time. Downstream code (optimizer, binary sensors, analytics
        call sites) must not treat ``None`` as ``0.0``.
        """
        data = self.config_entry.data

        # Required/optional sensor reads: do NOT collapse None to 0.0.
        # Power sensors are read with power=True so that kW/MW values are
        # automatically normalised to watts.
        pv_production: float | None = _parse_sensor_float(
            self.hass, data.get(CONF_PV_POWER), power=True,
        )
        self._track_sensor_availability(data.get(CONF_PV_POWER), pv_production)

        # Grid export/import: either separate entity or combined.
        grid_export: float | None = None
        grid_import: float | None = None
        import_export_entity = data.get(CONF_IMPORT_EXPORT)
        grid_export_entity = data.get(CONF_GRID_EXPORT)

        if import_export_entity:
            # Combined sensor: positive = export, negative = import.
            combined = _parse_sensor_float(
                self.hass, import_export_entity, power=True,
            )
            self._track_sensor_availability(import_export_entity, combined)
            if combined is None:
                grid_export = None
                grid_import = None
            else:
                grid_export = max(combined, 0.0)
                grid_import = abs(min(combined, 0.0))
        elif grid_export_entity:
            grid_export = _parse_sensor_float(
                self.hass, grid_export_entity, power=True,
            )
            self._track_sensor_availability(grid_export_entity, grid_export)
            grid_import = 0.0 if grid_export is not None else None

        load_power: float | None = _parse_sensor_float(
            self.hass, data.get(CONF_LOAD_POWER), power=True,
        )
        self._track_sensor_availability(data.get(CONF_LOAD_POWER), load_power)

        battery_soc = _parse_sensor_float(self.hass, data.get(CONF_BATTERY_SOC))

        # Battery power: either combined sensor or separate charge/discharge
        battery_power: float | None = None
        battery_power_entity = data.get(CONF_BATTERY_POWER)
        battery_charge_entity = data.get(CONF_BATTERY_CHARGE_POWER)
        battery_discharge_entity = data.get(CONF_BATTERY_DISCHARGE_POWER)

        if battery_power_entity:
            battery_power = _parse_sensor_float(
                self.hass, battery_power_entity, power=True,
            )
        elif battery_charge_entity or battery_discharge_entity:
            charge = _parse_sensor_float(
                self.hass, battery_charge_entity, power=True,
            ) or 0.0
            discharge = _parse_sensor_float(
                self.hass, battery_discharge_entity, power=True,
            ) or 0.0
            battery_power = charge - discharge

        # Calculate excess. Branch selection is topology-based and identical
        # to the hybrid fix; None-handling is added within each branch body.
        # See the branch behaviour table in the design spec.
        has_battery = (
            data.get(CONF_BATTERY_POWER) is not None
            or data.get(CONF_BATTERY_CHARGE_POWER) is not None
            or data.get(CONF_BATTERY_DISCHARGE_POWER) is not None
        )

        excess_power: float | None
        if import_export_entity:
            if grid_export is None or grid_import is None:
                excess_power = None
            else:
                excess_power = grid_export - grid_import
                # For hybrid inverters with battery configured, add battery
                # power back so excess reflects total solar above house load
                # rather than just grid export. Equivalent to pv - load.
                # battery_power is positive when charging (adds to excess),
                # negative when discharging (removes battery contribution).
                if has_battery and battery_power is not None:
                    excess_power += battery_power
        elif has_battery and load_power is not None and load_power > 0:
            # Hybrid branch: requires pv_production; load_power is guaranteed
            # non-None by the predicate.
            excess_power = (
                pv_production - load_power if pv_production is not None else None
            )
        elif grid_export_entity:
            if grid_export is None:
                # Nuance 1a: grid_export is the user's configured truth source.
                # Do not fall through — return None to surface the outage.
                excess_power = None
            elif grid_export > 0:
                excess_power = grid_export
            else:
                # grid_export == 0: fallback to pv - load. Requires both.
                if (
                    pv_production is not None
                    and load_power is not None
                    and load_power > 0
                ):
                    excess_power = pv_production - load_power
                else:
                    excess_power = None
        elif load_power is not None and load_power > 0:
            # Load-only branch: requires pv_production.
            excess_power = (
                pv_production - load_power if pv_production is not None else None
            )
        else:
            # Neither grid nor load configured — misconfiguration, not outage.
            excess_power = 0.0

        return PowerState(
            pv_production=pv_production,
            grid_export=grid_export,
            grid_import=grid_import,
            load_power=load_power,
            excess_power=excess_power,
            battery_soc=battery_soc,
            battery_power=battery_power,
            ev_soc=None,
            timestamp=datetime.now(),
        )

    # ------------------------------------------------------------------
    # Appliance configuration
    # ------------------------------------------------------------------

    def _get_appliance_configs(self) -> list[ApplianceConfig]:
        """Convert config entry subentries to ApplianceConfig list."""
        configs: list[ApplianceConfig] = []

        # Subentries are stored in config_entry.subentries (HA 2024.12+)
        subentries = getattr(self.config_entry, "subentries", {})
        for subentry_id, subentry in subentries.items():
            sub_data = subentry.data
            min_runtime_min = self.appliance_min_daily_runtime.get(
                subentry_id, sub_data.get(CONF_MIN_DAILY_RUNTIME)
            )
            max_runtime_min = self.appliance_max_daily_runtime.get(
                subentry_id, sub_data.get(CONF_MAX_DAILY_RUNTIME)
            )
            deadline_str = sub_data.get(CONF_SCHEDULE_DEADLINE)
            max_activations = sub_data.get(CONF_MAX_DAILY_ACTIVATIONS)
            if max_activations is not None:
                max_activations = int(max_activations)

            # Use runtime overrides from entity controls if available,
            # otherwise fall back to config entry data
            priority = self.appliance_priorities.get(
                subentry_id, sub_data.get(CONF_APPLIANCE_PRIORITY, 500)
            )
            override_active = self.appliance_overrides.get(subentry_id, False)
            is_enabled = self.appliance_enabled.get(subentry_id, True)

            # Skip disabled appliances unless they have an active override
            if not is_enabled and not override_active:
                continue

            # Skip appliances with no entity configured
            entity_id = sub_data.get(CONF_APPLIANCE_ENTITY, "")
            if not entity_id:
                _LOGGER.warning(
                    "Appliance %s has no entity configured, skipping",
                    sub_data.get(CONF_APPLIANCE_NAME, subentry_id),
                )
                continue

            # Clamp switch_interval to minimum of 5s to protect against
            # legacy configs that may have stored 0
            switch_interval = int(max(
                5, sub_data.get(CONF_SWITCH_INTERVAL, DEFAULT_SWITCH_INTERVAL)
            ))

            config = ApplianceConfig(
                id=subentry_id,
                name=sub_data.get(CONF_APPLIANCE_NAME, f"Appliance {subentry_id}"),
                entity_id=entity_id,
                priority=priority,
                phases=int(sub_data.get(CONF_PHASES, 1)),
                nominal_power=sub_data.get(CONF_NOMINAL_POWER, 0.0),
                actual_power_entity=sub_data.get(CONF_ACTUAL_POWER_ENTITY),
                dynamic_current=sub_data.get(CONF_DYNAMIC_CURRENT, False),
                current_entity=sub_data.get(CONF_CURRENT_ENTITY),
                min_current=sub_data.get(CONF_MIN_CURRENT, 6.0),
                max_current=sub_data.get(CONF_MAX_CURRENT, 16.0),
                ev_soc_entity=sub_data.get(CONF_EV_SOC_ENTITY),
                ev_connected_entity=sub_data.get(CONF_EV_CONNECTED_ENTITY),
                ev_target_soc=sub_data.get(CONF_EV_TARGET_SOC),
                is_big_consumer=sub_data.get(CONF_IS_BIG_CONSUMER, False),
                battery_max_discharge_override=sub_data.get(
                    CONF_BATTERY_DISCHARGE_OVERRIDE
                ),
                on_only=sub_data.get(CONF_ON_ONLY, False),
                min_daily_runtime=(
                    timedelta(minutes=min_runtime_min) if min_runtime_min is not None else None
                ),
                max_daily_runtime=(
                    timedelta(minutes=max_runtime_min) if max_runtime_min is not None else None
                ),
                schedule_deadline=_parse_time_string(deadline_str),
                start_after=_parse_time_string(sub_data.get(CONF_START_AFTER)),
                end_before=_parse_time_string(sub_data.get(CONF_END_BEFORE)),
                switch_interval=switch_interval,
                allow_grid_supplement=sub_data.get(CONF_ALLOW_GRID_SUPPLEMENT, False),
                max_grid_power=sub_data.get(CONF_MAX_GRID_POWER),
                cheap_grid_target_current=sub_data.get(CONF_CHEAP_GRID_TARGET_CURRENT),
                cheap_price_threshold=sub_data.get(CONF_CHEAP_PRICE_THRESHOLD),
                averaging_window=sub_data.get(CONF_AVERAGING_WINDOW),
                requires_appliance=sub_data.get(CONF_REQUIRES_APPLIANCE),
                helper_only=sub_data.get(CONF_HELPER_ONLY, False),
                protect_from_preemption=sub_data.get(CONF_PROTECT_FROM_PREEMPTION, False),
                current_step=sub_data.get(CONF_CURRENT_STEP, 0.1),
                override_active=override_active,
                max_daily_activations=max_activations,
                on_threshold=sub_data.get(CONF_ON_THRESHOLD),
                completion_power_threshold=sub_data.get(CONF_COMPLETION_POWER_THRESHOLD),
            )
            configs.append(config)

        # Derive "needed by others" set: any appliance referenced by another
        # appliance's requires_appliance must bypass switch-interval cooldown
        # in _apply_decisions so it can respond promptly to dependent state
        # transitions. See 2026-04-09-helper-only-hardening-design.md.
        self._needed_by_others = {
            c.requires_appliance
            for c in configs
            if c.requires_appliance
        }

        # Clean up stale entries from all tracking dicts for removed appliances.
        # Use ALL subentry IDs (not just configs) so disabled appliances keep
        # their appliance_enabled[id] = False entry instead of being re-enabled.
        active_ids = set(subentries.keys())
        for d in (
            self._last_state_change,
            self._last_applied_current,
            self._activations_today,
            self._previous_is_on,
            self.appliance_enabled,
            self.appliance_overrides,
            self.appliance_priorities,
            self.appliance_min_daily_runtime,
            self.appliance_max_daily_runtime,
        ):
            stale = [k for k in d if k not in active_ids]
            for k in stale:
                del d[k]

        return configs

    def _get_appliance_states(
        self, configs: list[ApplianceConfig]
    ) -> dict[str, ApplianceState]:
        """Read current state of each controlled appliance entity."""
        states: dict[str, ApplianceState] = {}

        for config in configs:
            entity_state = self.hass.states.get(config.entity_id)
            is_on = False
            if entity_state is not None:
                is_on = entity_state.state not in _OFF_STATES and entity_state.state not in _UNAVAILABLE_STATES

            # Detect off→on physical transition (Bug A from 2026-04-09
            # incident spec). activations_today is incremented based on
            # observed physical state, not on service-call intent. Protects
            # against devices that accept the command but fail to engage
            # (e.g., Sonoff relay with delayed state callbacks).
            prev_is_on = self._previous_is_on.get(config.id)
            if prev_is_on is False and is_on is True:
                self._activations_today[config.id] = (
                    self._activations_today.get(config.id, 0) + 1
                )
            self._previous_is_on[config.id] = is_on

            current_power = 0.0
            if config.actual_power_entity:
                current_power = (
                    _parse_sensor_float(
                        self.hass, config.actual_power_entity, power=True,
                    ) or 0.0
                )

            current_amperage: float | None = None
            if config.current_entity:
                current_amperage = _parse_sensor_float(
                    self.hass, config.current_entity
                )

            ev_connected: bool | None = None
            if config.ev_connected_entity:
                ev_connected = _parse_sensor_bool(
                    self.hass, config.ev_connected_entity
                )

            ev_soc: float | None = None
            if config.ev_soc_entity:
                ev_soc = _parse_sensor_float(self.hass, config.ev_soc_entity)

            # Retrieve and update runtime from stored state
            previous = self.appliance_states.get(config.id)
            runtime_today = previous.runtime_today if previous else timedelta()
            energy_today = previous.energy_today if previous else 0.0
            last_state_change = previous.last_state_change if previous else None

            # Increment runtime and energy if the appliance is currently ON
            if is_on and previous is not None:
                cycle_seconds = self.update_interval.total_seconds()
                # Gate runtime on actual power when completion threshold is configured
                counts_as_running = (
                    config.completion_power_threshold is None
                    or current_power >= config.completion_power_threshold
                )
                if counts_as_running:
                    runtime_today += timedelta(seconds=cycle_seconds)
                # Energy in kWh: power(W) * time(h)
                power_for_energy = (
                    current_power if current_power > 0
                    else (0.0 if config.actual_power_entity else config.nominal_power)
                )
                energy_today += (power_for_energy * cycle_seconds) / 3600 / 1000

            # Seed last_state_change for appliances that are ON but have no
            # recorded change time (e.g. after a reload) to prevent immediate
            # switching that would violate the switch interval constraint.
            if is_on and config.id not in self._last_state_change:
                self._last_state_change[config.id] = datetime.now()

            state = ApplianceState(
                appliance_id=config.id,
                is_on=is_on,
                current_power=current_power,
                current_amperage=current_amperage,
                runtime_today=runtime_today,
                energy_today=energy_today,
                last_state_change=last_state_change,
                ev_connected=ev_connected,
                ev_soc=ev_soc,
                activations_today=self._activations_today.get(config.id, 0),
            )
            states[config.id] = state

        # Preserve state for disabled appliances so runtime/energy sensors
        # don't go unavailable and don't reset to zero when re-enabled.
        # Refresh is_on and current_power from actual HA entities so the
        # Active and Power sensors stay accurate even when disabled.
        subentries = getattr(self.config_entry, "subentries", {})
        for sub_id in subentries:
            if sub_id not in states and sub_id in self.appliance_states:
                old = self.appliance_states[sub_id]
                sub_data = subentries[sub_id].data

                # Refresh is_on from the actual switch entity
                entity_id = sub_data.get(CONF_APPLIANCE_ENTITY, "")
                entity_state = self.hass.states.get(entity_id) if entity_id else None
                is_on = (
                    entity_state is not None
                    and entity_state.state not in _OFF_STATES
                    and entity_state.state not in _UNAVAILABLE_STATES
                )

                # Refresh current_power from the actual power sensor
                power_entity = sub_data.get(CONF_ACTUAL_POWER_ENTITY)
                current_power = (
                    _parse_sensor_float(self.hass, power_entity, power=True)
                    or 0.0
                ) if power_entity else 0.0

                states[sub_id] = ApplianceState(
                    appliance_id=old.appliance_id,
                    is_on=is_on,
                    current_power=current_power,
                    current_amperage=old.current_amperage,
                    runtime_today=old.runtime_today,
                    energy_today=old.energy_today,
                    last_state_change=old.last_state_change,
                    ev_connected=old.ev_connected,
                    ev_soc=old.ev_soc,
                    activations_today=old.activations_today,
                )

        self.appliance_states = states
        return states

    # ------------------------------------------------------------------
    # Tariff
    # ------------------------------------------------------------------

    def _get_tariff_info(self) -> TariffInfo:
        """Use the configured tariff provider to get current tariff info."""
        data = self.config_entry.data

        # Build state dict for entities the tariff provider needs
        ha_states: dict[str, dict] = {}
        price_entity = data.get(CONF_PRICE_SENSOR)
        if price_entity:
            state_dict = _entity_state_dict(self.hass, price_entity)
            if state_dict:
                ha_states[price_entity] = state_dict

        cheap_threshold = data.get(CONF_CHEAP_PRICE_THRESHOLD, 0.0)
        battery_charge_threshold = data.get(CONF_BATTERY_CHARGE_PRICE_THRESHOLD, 0.0)

        # Feed-in tariff: from sensor or static value
        feed_in = data.get(CONF_FEED_IN_TARIFF, 0.0)
        fit_sensor = data.get(CONF_FEED_IN_TARIFF_SENSOR)
        if fit_sensor:
            fit_val = _parse_sensor_float(self.hass, fit_sensor)
            if fit_val is not None:
                feed_in = fit_val

        return self._tariff_provider.get_tariff_info(
            states=ha_states,
            cheap_price_threshold=cheap_threshold,
            battery_charge_price_threshold=battery_charge_threshold,
            feed_in_tariff=feed_in,
        )

    # ------------------------------------------------------------------
    # Planner
    # ------------------------------------------------------------------

    async def _run_planner(self) -> None:
        """Run the planner to generate a new plan."""
        if self._forecast_provider is None:
            _LOGGER.debug("No forecast provider configured, skipping planner")
            return

        data = self.config_entry.data

        # Gather forecast data
        forecast_entity = data.get(CONF_FORECAST_SENSOR, "")
        ha_states: dict[str, dict] = {}
        if forecast_entity:
            state_dict = _entity_state_dict(self.hass, forecast_entity)
            if state_dict:
                ha_states[forecast_entity] = state_dict

        try:
            forecast_data = self._forecast_provider.get_forecast(ha_states)
        except Exception as err:
            _LOGGER.warning("Forecast provider error: %s", err)
            return

        # If a separate tomorrow sensor is configured, use its state as tomorrow_total_kwh
        tomorrow_entity = data.get(CONF_FORECAST_TOMORROW_SENSOR)
        if tomorrow_entity and forecast_data.tomorrow_total_kwh is None:
            tomorrow_val = _parse_sensor_float(self.hass, tomorrow_entity)
            if tomorrow_val is not None:
                forecast_data = ForecastData(
                    remaining_today_kwh=forecast_data.remaining_today_kwh,
                    hourly_breakdown=forecast_data.hourly_breakdown,
                    tomorrow_total_kwh=tomorrow_val,
                )

        try:
            tariff_info = self._get_tariff_info()
        except Exception as err:
            _LOGGER.warning("Planner: tariff provider error, using defaults: %s", err)
            tariff_info = TariffInfo(current_price=float("inf"), feed_in_tariff=0.0, cheap_price_threshold=0.0, battery_charge_price_threshold=0.0)
        appliance_configs = self._get_appliance_configs()

        # Battery config
        battery_config = self._get_battery_config()
        battery_soc = _parse_sensor_float(self.hass, data.get(CONF_BATTERY_SOC))
        export_limit = data.get(CONF_EXPORT_LIMIT)

        try:
            self.current_plan = self.planner.create_plan(
                forecast=forecast_data,
                tariff=tariff_info,
                appliances=appliance_configs,
                battery_config=battery_config,
                current_soc=battery_soc,
                export_limit=export_limit,
            )
            _LOGGER.debug(
                "Planner generated plan with %d entries, confidence %.2f",
                len(self.current_plan.entries),
                self.current_plan.confidence,
            )
        except Exception as err:
            _LOGGER.error("Planner error: %s", err)

    def _get_battery_config(self) -> BatteryConfig | None:
        """Build BatteryConfig from config entry data if battery is configured."""
        data = self.config_entry.data
        capacity = data.get(CONF_BATTERY_CAPACITY)
        if not capacity:
            return None

        target_soc = data.get(CONF_BATTERY_TARGET_SOC, 100.0)
        target_time_str = data.get(CONF_BATTERY_TARGET_TIME, "16:00")
        target_time = _parse_time_string(target_time_str) or time(16, 0)

        # Use runtime battery_strategy (from select entity) instead of config data
        try:
            strategy = BatteryStrategy(self.battery_strategy)
        except ValueError:
            strategy = BatteryStrategy.BALANCED

        return BatteryConfig(
            capacity_kwh=capacity,
            max_discharge_entity=data.get(CONF_BATTERY_MAX_DISCHARGE_ENTITY),
            max_discharge_default=data.get(CONF_BATTERY_MAX_DISCHARGE_DEFAULT),
            target_soc=target_soc,
            target_time=target_time,
            strategy=strategy,
            allow_grid_charging=data.get(CONF_ALLOW_GRID_CHARGING, False),
        )

    # ------------------------------------------------------------------
    # Apply decisions
    # ------------------------------------------------------------------

    async def _apply_decisions(self, result: OptimizerResult) -> list[str]:
        """Apply control decisions by calling HA services.

        Returns list of appliance_ids that were successfully changed.
        """
        if not self._enabled:
            _LOGGER.debug("Controller disabled, skipping all service calls")
            return []

        applied_ids: list[str] = []

        # Order decisions for dependency safety:
        # ON/SET_CURRENT: dependencies first (no requires_appliance), then dependents
        # OFF: dependents first (has requires_appliance), then dependencies
        def _dep_sort_key(d):
            cfg = self._get_appliance_config_by_id(d.appliance_id)
            has_dep = cfg.requires_appliance if cfg else None
            if d.action == Action.OFF:
                return (0 if has_dep else 1,)
            else:
                return (1 if has_dep else 0,)

        sorted_decisions = sorted(result.decisions, key=_dep_sort_key)

        for decision in sorted_decisions:
            if decision.action == Action.IDLE:
                continue

            # Skip disabled appliances (unless override is active)
            if not self.appliance_enabled.get(decision.appliance_id, True) and not self.appliance_overrides.get(decision.appliance_id, False):
                continue

            appliance_config = self._get_appliance_config_by_id(decision.appliance_id)
            if appliance_config is None:
                continue

            entity_id = appliance_config.entity_id
            domain = entity_id.split(".")[0] if "." in entity_id else "switch"

            # Check if entity exists before calling service
            current_state = self.hass.states.get(entity_id)
            if current_state is None:
                _LOGGER.warning("Entity %s not found in HA, skipping", entity_id)
                continue

            # Check if state actually needs to change
            is_on = current_state.state not in _OFF_STATES and current_state.state not in _UNAVAILABLE_STATES
            if decision.action == Action.ON and is_on:
                _LOGGER.debug(
                    "Skipping %s for %s (%s): already on",
                    decision.action, appliance_config.name, entity_id,
                )
                continue  # Already on, skip
            if decision.action == Action.OFF and not is_on:
                _LOGGER.debug(
                    "Skipping %s for %s (%s): already off",
                    decision.action, appliance_config.name, entity_id,
                )
                continue  # Already off, skip

            # Check switch interval (only for ON/OFF transitions, not current adjustments)
            # Skip switch interval for safety/constraint decisions that should
            # act immediately. The optimizer marks these via the
            # `bypasses_cooldown` flag on ControlDecision (set on the seven
            # safety-OFF sites: max daily runtime, max daily activations,
            # EV not connected, EV SoC target reached, outside operating
            # window, battery SoC protection — both big-consumer and
            # appliance shed paths). Previously this used substring matching
            # on `decision.reason`, which broke silently any time a reason
            # string was reworded; the structured flag is the source of truth.
            # Bypass cooldown for:
            # (1) decisions with bypasses_cooldown=True (safety-OFF sites), OR
            # (2) appliances referenced by another appliance's requires_appliance
            #     (they may need to respond promptly to dependent transitions —
            #     see 2026-04-09-helper-only-hardening-design.md Bug C).
            is_needed_by_others = decision.appliance_id in self._needed_by_others
            if not decision.bypasses_cooldown and not is_needed_by_others:
                if decision.action != Action.SET_CURRENT or not is_on:
                    last_change = self._last_state_change.get(decision.appliance_id)
                    if last_change is not None:
                        elapsed = (datetime.now() - last_change).total_seconds()
                        if elapsed < appliance_config.switch_interval:
                            _LOGGER.debug(
                                "Skipping %s for %s (%s): switch interval not elapsed "
                                "(%.0fs of %ds)",
                                decision.action, appliance_config.name, entity_id,
                                elapsed, appliance_config.switch_interval,
                            )
                            continue  # Too soon to change

            try:
                if decision.action == Action.ON:
                    try:
                        async with asyncio.timeout(10):
                            await self.hass.services.async_call(
                                domain, "turn_on", {"entity_id": entity_id},
                                blocking=True,
                            )
                    except (TimeoutError, Exception) as err:
                        _LOGGER.error("Failed to turn on %s: %s", appliance_config.name, err)
                        continue
                elif decision.action == Action.OFF:
                    try:
                        async with asyncio.timeout(10):
                            await self.hass.services.async_call(
                                domain, "turn_off", {"entity_id": entity_id},
                                blocking=True,
                            )
                    except (TimeoutError, Exception) as err:
                        _LOGGER.error("Failed to turn off %s: %s", appliance_config.name, err)
                        continue
                    self._last_applied_current.pop(decision.appliance_id, None)
                elif decision.action == Action.SET_CURRENT:
                    # For dynamic current, set the current entity if available
                    if (
                        appliance_config.current_entity
                        and decision.target_current is not None
                    ):
                        # H3: Skip entirely if same current already applied and appliance is on
                        if (
                            is_on
                            and decision.target_current == self._last_applied_current.get(decision.appliance_id)
                        ):
                            _LOGGER.debug(
                                "Skipping SET_CURRENT for %s: current %.1fA already applied",
                                appliance_config.name, decision.target_current,
                            )
                            continue

                        current_domain = (
                            appliance_config.current_entity.split(".")[0]
                            if "." in appliance_config.current_entity
                            else "number"
                        )
                        # First: set the current value
                        try:
                            async with asyncio.timeout(10):
                                await self.hass.services.async_call(
                                    current_domain,
                                    "set_value",
                                    {
                                        "entity_id": appliance_config.current_entity,
                                        "value": decision.target_current,
                                    },
                                    blocking=True,
                                )
                        except (TimeoutError, Exception) as err:
                            _LOGGER.error("Failed to set current for %s: %s", appliance_config.name, err)
                            continue

                        # Track last applied current for deduplication
                        self._last_applied_current[decision.appliance_id] = decision.target_current

                        # H2: Only call turn_on if the appliance is not already on
                        if not is_on:
                            try:
                                async with asyncio.timeout(10):
                                    await self.hass.services.async_call(
                                        domain, "turn_on", {"entity_id": entity_id},
                                        blocking=True,
                                    )
                            except (TimeoutError, Exception) as err:
                                _LOGGER.warning("Current set but turn_on failed for %s: %s", appliance_config.name, err)
                                continue  # Don't record as applied
                    else:
                        _LOGGER.warning(
                            "SET_CURRENT for %s but no current_entity configured, skipping",
                            appliance_config.name,
                        )
                        continue  # Don't turn on at full power

                # Only update switch interval timer for actual ON/OFF state
                # transitions, not for SET_CURRENT adjustments on already-running
                # appliances (which would permanently block OFF decisions).
                if decision.action in (Action.ON, Action.OFF):
                    self._last_state_change[decision.appliance_id] = datetime.now()
                elif decision.action == Action.SET_CURRENT and not is_on:
                    # Initial turn-on via SET_CURRENT also needs switch interval protection
                    self._last_state_change[decision.appliance_id] = datetime.now()
                applied_ids.append(decision.appliance_id)
                _LOGGER.info(
                    "Applied %s to %s (%s): %s",
                    decision.action, appliance_config.name, entity_id,
                    decision.reason,
                )
            except Exception as err:
                _LOGGER.error(
                    "Failed to apply decision for %s: %s",
                    decision.appliance_id,
                    err,
                )

        # Apply battery discharge action (both set limit and restore default)
        await self._apply_battery_discharge_limit(result.battery_discharge_action)

        return applied_ids

    async def _apply_battery_discharge_limit(
        self, action: BatteryDischargeAction
    ) -> None:
        """Set or restore battery discharge limit via the configured entity."""
        data = self.config_entry.data
        discharge_entity = data.get(CONF_BATTERY_MAX_DISCHARGE_ENTITY)
        if not discharge_entity:
            return

        domain = discharge_entity.split(".")[0] if "." in discharge_entity else "number"

        if action.should_limit and action.max_discharge_watts is not None:
            # Big consumer active: limit discharge
            target_value = action.max_discharge_watts
        else:
            # No big consumers active: restore to default
            default_value = data.get(CONF_BATTERY_MAX_DISCHARGE_DEFAULT)
            if default_value is None:
                _LOGGER.warning(
                    "Battery discharge limit active but no default configured. "
                    "Cannot restore. Configure battery_max_discharge_default in settings."
                )
                return
            target_value = default_value

        # Skip the service call if the target value hasn't changed
        if target_value == self._last_discharge_limit:
            return

        try:
            async with asyncio.timeout(10):
                await self.hass.services.async_call(
                    domain,
                    "set_value",
                    {
                        "entity_id": discharge_entity,
                        "value": target_value,
                    },
                    blocking=True,
                )
            self._last_discharge_limit = target_value
        except TimeoutError:
            _LOGGER.warning(
                "Service call timed out setting battery discharge limit on %s",
                discharge_entity,
            )
        except Exception as err:
            _LOGGER.error("Failed to set battery discharge limit: %s", err)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_appliance_config_by_id(
        self, appliance_id: str
    ) -> ApplianceConfig | None:
        """Look up an appliance config by its subentry ID."""
        subentries = getattr(self.config_entry, "subentries", {})
        subentry = subentries.get(appliance_id)
        if subentry is None:
            return None

        sub_data = subentry.data
        min_runtime_min = self.appliance_min_daily_runtime.get(
            appliance_id, sub_data.get(CONF_MIN_DAILY_RUNTIME)
        )
        max_runtime_min = self.appliance_max_daily_runtime.get(
            appliance_id, sub_data.get(CONF_MAX_DAILY_RUNTIME)
        )
        deadline_str = sub_data.get(CONF_SCHEDULE_DEADLINE)

        # Priority may be overridden by runtime dict (same as _get_appliance_configs).
        priority = self.appliance_priorities.get(
            appliance_id, sub_data.get(CONF_APPLIANCE_PRIORITY, 500)
        )
        override_active = self.appliance_overrides.get(appliance_id, False)

        return ApplianceConfig(
            id=appliance_id,
            name=sub_data.get(CONF_APPLIANCE_NAME, f"Appliance {appliance_id}"),
            entity_id=sub_data.get(CONF_APPLIANCE_ENTITY, ""),
            priority=priority,
            phases=int(sub_data.get(CONF_PHASES, 1)),
            nominal_power=sub_data.get(CONF_NOMINAL_POWER, 0.0),
            actual_power_entity=sub_data.get(CONF_ACTUAL_POWER_ENTITY),
            dynamic_current=sub_data.get(CONF_DYNAMIC_CURRENT, False),
            current_entity=sub_data.get(CONF_CURRENT_ENTITY),
            min_current=sub_data.get(CONF_MIN_CURRENT, 6.0),
            max_current=sub_data.get(CONF_MAX_CURRENT, 16.0),
            ev_soc_entity=sub_data.get(CONF_EV_SOC_ENTITY),
            ev_connected_entity=sub_data.get(CONF_EV_CONNECTED_ENTITY),
            ev_target_soc=sub_data.get(CONF_EV_TARGET_SOC),
            is_big_consumer=sub_data.get(CONF_IS_BIG_CONSUMER, False),
            battery_max_discharge_override=sub_data.get(
                CONF_BATTERY_DISCHARGE_OVERRIDE
            ),
            on_only=sub_data.get(CONF_ON_ONLY, False),
            min_daily_runtime=(
                timedelta(minutes=min_runtime_min) if min_runtime_min is not None else None
            ),
            max_daily_runtime=(
                timedelta(minutes=max_runtime_min) if max_runtime_min is not None else None
            ),
            schedule_deadline=_parse_time_string(deadline_str),
            start_after=_parse_time_string(sub_data.get(CONF_START_AFTER)),
            end_before=_parse_time_string(sub_data.get(CONF_END_BEFORE)),
            switch_interval=int(max(
                5, sub_data.get(CONF_SWITCH_INTERVAL, DEFAULT_SWITCH_INTERVAL)
            )),
            allow_grid_supplement=sub_data.get(CONF_ALLOW_GRID_SUPPLEMENT, False),
            max_grid_power=sub_data.get(CONF_MAX_GRID_POWER),
            cheap_grid_target_current=sub_data.get(CONF_CHEAP_GRID_TARGET_CURRENT),
            cheap_price_threshold=sub_data.get(CONF_CHEAP_PRICE_THRESHOLD),
            averaging_window=sub_data.get(CONF_AVERAGING_WINDOW),
            requires_appliance=sub_data.get(CONF_REQUIRES_APPLIANCE),
            helper_only=sub_data.get(CONF_HELPER_ONLY, False),
            protect_from_preemption=sub_data.get(CONF_PROTECT_FROM_PREEMPTION, False),
            current_step=sub_data.get(CONF_CURRENT_STEP, 0.1),
            override_active=override_active,
            max_daily_activations=(
                int(sub_data[CONF_MAX_DAILY_ACTIVATIONS])
                if sub_data.get(CONF_MAX_DAILY_ACTIVATIONS) is not None
                else None
            ),
            on_threshold=sub_data.get(CONF_ON_THRESHOLD),
            completion_power_threshold=sub_data.get(CONF_COMPLETION_POWER_THRESHOLD),
        )

    async def _turn_off_all_managed(self) -> None:
        """Turn off all currently-ON managed appliances (M11).

        Called when the master switch transitions from enabled to disabled.
        """
        subentries = getattr(self.config_entry, "subentries", {})
        for subentry_id, subentry in subentries.items():
            entity_id = subentry.data.get(CONF_APPLIANCE_ENTITY, "")
            if not entity_id:
                continue
            current_state = self.hass.states.get(entity_id)
            if current_state is None:
                continue
            if current_state.state in _OFF_STATES or current_state.state in _UNAVAILABLE_STATES:
                continue
            domain = entity_id.split(".")[0] if "." in entity_id else "switch"
            name = subentry.data.get(CONF_APPLIANCE_NAME, subentry_id)
            try:
                async with asyncio.timeout(10):
                    await self.hass.services.async_call(
                        domain, "turn_off", {"entity_id": entity_id},
                        blocking=True,
                    )
                _LOGGER.info("Master switch disabled: turned off %s (%s)", name, entity_id)
            except (TimeoutError, Exception) as err:
                _LOGGER.error("Failed to turn off %s on master disable: %s", name, err)

        # Reset battery discharge limit to default (no limiting)
        await self._apply_battery_discharge_limit(BatteryDischargeAction(should_limit=False))

    def _create_empty_plan(self) -> Plan:
        """Create an empty plan with no entries."""
        from .models import BatteryStrategy, BatteryTarget

        return Plan(
            created_at=datetime.now(),
            horizon=timedelta(hours=24),
            entries=[],
            battery_target=BatteryTarget(
                target_soc=100.0,
                target_time=datetime.now() + timedelta(hours=8),
                strategy=BatteryStrategy.BALANCED,
            ),
            confidence=0.0,
        )

    def _build_coordinator_data(self) -> dict[str, Any]:
        """Build a data dict that entity platforms can read."""
        latest_power = self.power_history[-1] if self.power_history else None

        # Compute remaining startup grace period in seconds (None when elapsed)
        elapsed = (datetime.now() - self._startup_time).total_seconds()
        if elapsed < DEFAULT_STARTUP_GRACE_PERIOD:
            grace_period_remaining: float | None = DEFAULT_STARTUP_GRACE_PERIOD - elapsed
        else:
            grace_period_remaining = None

        return {
            "power_state": latest_power,
            "power_history": list(self.power_history),
            "current_plan": self.current_plan,
            "control_decisions": list(self.control_decisions),
            "battery_discharge_action": self.battery_discharge_action,
            "appliance_states": dict(self.appliance_states),
            "appliance_configs": {c.id: c for c in self._last_appliance_configs},
            "grace_period_remaining": grace_period_remaining,
            "enabled": self._enabled,
            "tariff": self._last_tariff_info,
            "analytics": {
                "self_consumption_ratio": self.analytics.self_consumption_ratio,
                "savings_today": self.analytics.savings_today,
                "solar_consumed_kwh": self.analytics.solar_consumed_kwh,
                "grid_export_kwh": self.analytics.grid_export_kwh,
            },
        }
