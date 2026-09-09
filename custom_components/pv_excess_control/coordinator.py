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
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from .daily_state import nonnegative_number
from .power_policy import battery_first_budget

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
    CONF_CURRENT_UPDATE_INTERVAL,
    CONF_CURRENT_MIN_CHANGE,
    MAX_AVERAGING_WINDOW,
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
    CONF_DYNAMIC_BATTERY_CHARGE_ENABLED,
    CONF_INVERTER_BATTERY_MAX_CHARGE_POWER_ENTITY,
    CONF_BATTERY_MAX_CHARGE_POWER_W,
    CONF_BATTERY_TRICKLE_CHARGE_POWER_W,
    DEFAULT_BATTERY_TRICKLE_CHARGE_POWER_W,
)
from .energy import create_tariff_provider
from .forecast import AggregatingForecastProvider
from .models import (
    Action,
    ApplianceConfig,
    ApplianceState,
    BatteryChargeCurve,
    BatteryConfig,
    BatteryDischargeAction,
    ControlDecision,
    InverterGridChargeConfig,
    OptimizerResult,
    Plan,
    PowerState,
    TariffInfo,
)
from .analytics import AnalyticsTracker
from .inverter_control import InverterGridChargeController
from .notifications import NotificationManager
from .optimizer import Optimizer, recent_power_history
from .planner import Planner

_LOGGER = logging.getLogger(__name__)

_OFF_STATES = {"off", "false", "False", "0"}
_UNAVAILABLE_STATES = {STATE_UNAVAILABLE, STATE_UNKNOWN, "none", ""}

# Maximum number of power history entries to keep (~30 min at 30s intervals)
MAX_HISTORY_SIZE = 3600

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


def compute_battery_charge_setpoint(
    *,
    now: datetime,
    excess_w: float | None,
    curve: BatteryChargeCurve | None,
    export_soft_ceiling_w: int,
    battery_max_charge_power_w: int,
    battery_trickle_charge_power_w: int,
) -> int:
    """Compute the battery max-charge-power setpoint for this cycle.

    Combines the planner's curve (the floor) with a reactive lift driven
    by measured PV excess overshooting the soft ceiling. See
    docs/dynamic-battery-charging.md
    section "Coordinator reactive loop".
    """
    planned_w = battery_trickle_charge_power_w
    if curve is not None:
        for sp in curve.setpoints:
            if sp.start <= now < sp.end:
                planned_w = sp.max_charge_w
                break

    reactive_w = 0
    if excess_w is not None and excess_w > export_soft_ceiling_w:
        reactive_w = int(excess_w - export_soft_ceiling_w)

    target_w = max(planned_w, reactive_w)
    return max(
        battery_trickle_charge_power_w,
        min(target_w, battery_max_charge_power_w),
    )


def _dyn_charge_should_run(coordinator) -> bool:
    """True when the dynamic-battery-charge loop should produce a normal write."""
    if not coordinator.config_entry.data.get(CONF_DYNAMIC_BATTERY_CHARGE_ENABLED, False):
        return False
    if getattr(coordinator, "_dyn_charge_self_disabled_reason", None):
        return False
    startup_time = getattr(coordinator, "_startup_time", None)
    if startup_time is not None:
        elapsed = (datetime.now() - startup_time).total_seconds()
        if elapsed < DEFAULT_STARTUP_GRACE_PERIOD:
            return False
    if getattr(coordinator, "_forecast_status", "not_configured") in ("unavailable", "pending", "planner_error"):
        return False
    if getattr(coordinator, "force_charge", False):
        return False
    if getattr(coordinator, "_grid_charge_engaged", False):
        return False
    return True


class PvExcessCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for PV Excess Control.

    Periodically reads sensor data, runs the optimizer and planner,
    and applies control decisions.
    """

    config_entry: ConfigEntry
    _daily_state_store: Store | None = None
    _daily_save_pending = False
    _pending_daily_summary: tuple[float, float, float] | None = None

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
        self._daily_state_date = dt_util.now().date()
        self._restored_appliance_ids: set[str] = set()
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
        self.appliance_paused = {aid: True for aid in config_entry.data.get("paused_appliances", [])}
        self._pending_stop_appliances = set(config_entry.data.get("_pending_stop_appliances", []))
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

        # Dynamic battery charging (#17)
        self._dyn_charge_loop_active: bool = False
        self._dyn_charge_release_pending: bool = False
        self._dyn_charge_pause_released: bool = False
        self._dyn_charge_last_written_w: int | None = None
        self._dyn_charge_last_write_time: datetime | None = None
        self._dyn_charge_last_warn_time: datetime | None = None
        self._dyn_charge_self_disabled_reason: str | None = None
        self._dyn_charge_planned_w: int = 0
        self._dyn_charge_reactive_w: int = 0

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
        self._forecast_entities = list(dict.fromkeys(filter(None, [
            config_entry.data.get(CONF_FORECAST_SENSOR),
            *config_entry.data.get("additional_forecast_sensors", []),
        ])))
        self._forecast_tomorrow_entities = list(dict.fromkeys(filter(None, [
            config_entry.data.get(CONF_FORECAST_TOMORROW_SENSOR),
            *config_entry.data.get("additional_forecast_tomorrow_sensors", []),
        ])))
        self._forecast_provider = (
            AggregatingForecastProvider(forecast_type, self._forecast_entities, self._forecast_tomorrow_entities)
            if forecast_type != ForecastProviderEnum.NONE else None
        )
        self._forecast_status = "pending" if self._forecast_provider else "not_configured"
        self._forecast_error: str | None = None
        self._forecast_data = None

        # Validate dynamic battery charge config at startup; sets
        # _dyn_charge_self_disabled_reason if any dependency is missing.
        self._validate_dynamic_battery_charge_config()

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

    def update_from_subentries(self) -> None:
        """Keep entity controls and configuration forms on the same values."""
        subentries = self.config_entry.subentries
        self.appliance_priorities = {
            sid: sub.data.get(CONF_APPLIANCE_PRIORITY, 500)
            for sid, sub in subentries.items()
        }
        for key, attr in ((CONF_MIN_DAILY_RUNTIME, "appliance_min_daily_runtime"),
                          (CONF_MAX_DAILY_RUNTIME, "appliance_max_daily_runtime")):
            setattr(self, attr, {sid: sub.data[key] for sid, sub in subentries.items() if key in sub.data})

    def _persist_pending_stops(self) -> None:
        """Keep an unfinished explicit stop retryable across reloads."""
        try:
            data = dict(self.config_entry.data)
            data["_pending_stop_appliances"] = sorted(self._pending_stop_appliances)
            self.hass.config_entries.async_update_entry(self.config_entry, data=data)
        except Exception:
            _LOGGER.exception("Could not persist pending appliance shutdowns")

    def cancel_pending_stop(self, appliance_id: str) -> None:
        """A new explicit start/resume supersedes an unfinished stop."""
        if appliance_id in getattr(self, "_pending_stop_appliances", set()):
            self._pending_stop_appliances.discard(appliance_id)
            self._persist_pending_stops()

    async def async_stop_appliance(self, appliance_id: str) -> None:
        """Stop explicitly even when the appliance has left automatic control."""
        if not hasattr(self, "_pending_stop_appliances"):
            self._pending_stop_appliances = set()
        if appliance_id not in self._pending_stop_appliances:
            self._pending_stop_appliances.add(appliance_id)
            self._persist_pending_stops()
        config = self._get_appliance_config_by_id(appliance_id)
        if config is None or not config.entity_id:
            self.cancel_pending_stop(appliance_id)
            return
        state = self.hass.states.get(config.entity_id)
        if state is not None and state.state in _OFF_STATES:
            self.cancel_pending_stop(appliance_id)
            return
        try:
            async with asyncio.timeout(10):
                await self.hass.services.async_call(
                    config.entity_id.split(".")[0], "turn_off",
                    {"entity_id": config.entity_id}, blocking=True,
                )
        except Exception as err:
            _LOGGER.warning("Shutdown pending for %s; will retry: %s", config.name, err)
        # Keep the request until a subsequent physical OFF observation confirms it.

    async def _retry_pending_stops(self) -> None:
        for appliance_id in tuple(getattr(self, "_pending_stop_appliances", ())):
            # Another explicit action may cancel this request while a previous
            # appliance's service call yields to the event loop.
            if appliance_id in self._pending_stop_appliances:
                await self.async_stop_appliance(appliance_id)

    async def async_restore_daily_state(self) -> None:
        """Restore only today's counters before the first sensor refresh."""
        if self._daily_state_store is None:
            self._daily_state_store = Store(
                self.hass, 1, f"{DOMAIN}.{self.config_entry.entry_id}.daily_state",
            )
        self._daily_state_date = dt_util.now().date()
        self._restored_appliance_ids = set()
        try:
            data = await self._daily_state_store.async_load()
        except (OSError, ValueError, TypeError):
            _LOGGER.exception("Could not restore daily counters; storage disabled until reload")
            # Do not overwrite unread counters after a transient read failure.
            self._daily_state_store = None
            return
        if not isinstance(data, dict) or data.get("date") != self._daily_state_date.isoformat():
            return
        rows = data.get("appliances")
        if not isinstance(rows, dict):
            return
        for appliance_id in self.config_entry.subentries:
            row = rows.get(appliance_id)
            if not isinstance(row, dict):
                continue
            activations = int(nonnegative_number(row.get("activations", 0)))
            self.appliance_states[appliance_id] = ApplianceState(
                appliance_id=appliance_id, is_on=False, current_power=0.0,
                current_amperage=None,
                runtime_today=timedelta(seconds=nonnegative_number(row.get("runtime_seconds"), 86400)),
                energy_today=nonnegative_number(row.get("energy_kwh")),
                last_state_change=None, ev_connected=None, ev_soc=None,
                activations_today=activations,
            )
            self._activations_today[appliance_id] = activations
            self._restored_appliance_ids.add(appliance_id)
        self.analytics.restore_daily(data.get("analytics"), set(self.config_entry.subentries))

    def _ensure_daily_date(self) -> None:
        """Recover the local day boundary even if HA missed the midnight event."""
        if getattr(self, "_daily_state_date", dt_util.now().date()) != dt_util.now().date():
            self._pending_daily_summary = (
                self.analytics.self_consumption_ratio,
                self.analytics.savings_today,
                self.analytics.solar_consumed_kwh,
            )
            self.reset_daily()

    async def async_handle_midnight(self) -> None:
        """Roll over once before awaiting I/O; keep the previous day's summary."""
        self._ensure_daily_date()
        summary = self._pending_daily_summary
        self._pending_daily_summary = None
        await self.async_save_daily_state()
        if summary is not None:
            try:
                await self.notifications.notify_daily_summary(*summary)
            except Exception:
                _LOGGER.exception("Failed to send daily summary notification")
        await self.async_request_refresh()

    def _daily_state_data(self) -> dict[str, Any]:
        """Take the latest snapshot at write time, never label yesterday as today."""
        self._ensure_daily_date()
        return {
            "date": dt_util.now().date().isoformat(),
            "appliances": {
                appliance_id: {
                    "runtime_seconds": state.runtime_today.total_seconds(),
                    "energy_kwh": state.energy_today,
                    "activations": self._activations_today.get(appliance_id, state.activations_today),
                }
                for appliance_id, state in self.appliance_states.items()
                if appliance_id in self.config_entry.subentries
            },
            "analytics": self.analytics.snapshot_daily(),
        }

    def _schedule_daily_state_save(self) -> None:
        """Arm one write; frequent updates must not postpone it indefinitely."""
        if self._daily_state_store is None or self._daily_save_pending:
            return
        self._daily_save_pending = True

        def snapshot() -> dict[str, Any]:
            self._daily_save_pending = False
            return self._daily_state_data()

        self._daily_state_store.async_delay_save(snapshot, 60)

    async def async_save_daily_state(self) -> None:
        """Flush counters on unload; Store also flushes queued writes at HA stop."""
        if self._daily_state_store is not None:
            try:
                await self._daily_state_store.async_save(self._daily_state_data())
            except (OSError, ValueError, TypeError):
                _LOGGER.exception("Could not save daily counters")
            finally:
                self._daily_save_pending = False

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

    def _validate_dynamic_battery_charge_config(self) -> None:
        """Set _dyn_charge_self_disabled_reason if config is invalid.

        Called once during __init__. The dispatcher's _dyn_charge_should_run
        predicate respects the disabled reason and skips writes for the
        rest of the session.
        """
        data = self.config_entry.data
        if not data.get(CONF_DYNAMIC_BATTERY_CHARGE_ENABLED, False):
            return  # not enabled; no validation needed

        entity_id = data.get(CONF_INVERTER_BATTERY_MAX_CHARGE_POWER_ENTITY)
        if not entity_id:
            self._dyn_charge_self_disabled_reason = "entity_not_configured"
            _LOGGER.warning(
                "Dynamic battery charge enabled but no inverter entity configured"
            )
            return

        domain = entity_id.split(".")[0]
        if domain not in {"number", "input_number"}:
            self._dyn_charge_self_disabled_reason = "entity_wrong_domain"
            _LOGGER.warning(
                "Dynamic battery charge entity %s domain '%s' not supported",
                entity_id, domain,
            )
            return

        max_w = data.get(CONF_BATTERY_MAX_CHARGE_POWER_W, 0) or 0
        if max_w <= 0:
            self._dyn_charge_self_disabled_reason = "invalid_max_power"
            _LOGGER.warning(
                "Dynamic battery charge: invalid max charge power %s", max_w
            )
            return

        export_limit = data.get(CONF_EXPORT_LIMIT)
        if not export_limit or export_limit <= 0:
            self._dyn_charge_self_disabled_reason = "invalid_export_limit"
            _LOGGER.warning(
                "Dynamic battery charge: invalid export_limit"
            )
            return

        battery_config = self._get_battery_config()
        capacity = battery_config.capacity_kwh if battery_config is not None else 0
        if capacity <= 0:
            self._dyn_charge_self_disabled_reason = "invalid_battery_capacity"
            _LOGGER.warning(
                "Dynamic battery charge: invalid battery capacity"
            )
            return

        # HA may set up the inverter after this integration. Availability is a
        # runtime condition handled by the retryable writer, not invalid config.
        if self.hass.states.get(entity_id) is None:
            _LOGGER.debug(
                "Dynamic battery charge entity %s not available yet; writes will retry",
                entity_id,
            )

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

        auto_should_engage = auto_flag and cheap_now and soc_below_target
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
        self._daily_state_date = dt_util.now().date()
        self._restored_appliance_ids = set()
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
                current_power_available=state.current_power_available,
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
        self.battery_strategy = self.config_entry.data.get(CONF_BATTERY_STRATEGY, self.battery_strategy)
        await self._retry_pending_stops()
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

        # 2. Retain the maximum supported time window, with a sample safety cap.
        self.power_history.append(power_state)
        self.power_history = recent_power_history(
            self.power_history, MAX_AVERAGING_WINDOW, power_state.timestamp,
        )[-MAX_HISTORY_SIZE:]

        forecast_fingerprint = tuple(
            (entity, state.state if (state := self.hass.states.get(entity)) else None,
             repr(state.attributes) if state else None)
            for entity in [*getattr(self, "_forecast_entities", []), *getattr(self, "_forecast_tomorrow_entities", [])]
        )
        forecast_changed = forecast_fingerprint != getattr(self, "_forecast_input_fingerprint", forecast_fingerprint)
        self._forecast_input_fingerprint = forecast_fingerprint
        # 3. Run planner on its interval
        self._planner_counter += 1
        planner_ratio = max(
            1,
            int(self._planner_interval // self.update_interval.total_seconds()),
        )
        if self._planner_counter >= planner_ratio or forecast_changed or getattr(self, "_forecast_status", None) == "pending":
            self._planner_counter = 0
            await self._run_planner()

        # 5. Get appliance configs and states early so runtime/energy tracking
        # works even during the startup grace period (M20)
        appliance_configs = self._get_appliance_configs()
        self._last_appliance_configs = appliance_configs
        appliance_states = self._get_appliance_states(appliance_configs)
        fingerprint = tuple((c.id, c.phases, appliance_states[c.id].is_on, appliance_states[c.id].enable_condition, appliance_states[c.id].remaining_runtime_minutes) for c in appliance_configs)
        previous_fingerprint = getattr(self, "_control_input_fingerprint", None)
        self._control_input_fingerprint = fingerprint
        if previous_fingerprint is not None and fingerprint != previous_fingerprint:
            self.current_plan = None
            await self._run_planner()
        policy_power, policy_history = self._power_for_optimizer(power_state, appliance_configs, appliance_states)

        # 4. Skip optimizer if disabled or in startup grace period
        if not self._enabled:
            self._solar_start_since = {}
            _LOGGER.debug("Controller disabled, skipping optimization")
            # M11: Turn off all managed appliances on the transition to disabled
            if self._was_enabled:
                await self._turn_off_all_managed()
            self._was_enabled = False
            return self._build_coordinator_data()

        self._was_enabled = True  # Mark as enabled for M11 transition detection

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
            power_state_for_optimizer = policy_power
            history_for_optimizer = policy_history

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

        self._update_start_qualification(result.decisions)
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
                # The normalized reading already includes an unmetered-load
                # estimate. Never replace valid zero/unavailable with nominal,
                # nor count a command before the appliance is observed ON.
                app_state = appliance_states.get(decision.appliance_id)
                if (app_state is None or not app_state.is_on
                        or not app_state.current_power_available or app_state.current_power <= 0):
                    continue
                power = app_state.current_power
                # Source classification comes from the structured decision.
                if decision.uses_grid_supplement:
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

        # Dynamic battery charge dispatch (#17).
        await self._dispatch_dynamic_battery_charge(power_state)

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
        uses_net_meter = bool(data.get(CONF_IMPORT_EXPORT))
        hybrid_mapping = data.get(CONF_INVERTER_TYPE) == "hybrid" or any(data.get(key) for key in (
            CONF_BATTERY_SOC, CONF_BATTERY_POWER, CONF_BATTERY_CHARGE_POWER, CONF_BATTERY_DISCHARGE_POWER,
        ))
        uses_load_balance = not uses_net_meter and bool(data.get(CONF_PV_POWER) and data.get(CONF_LOAD_POWER)) and (
            hybrid_mapping or not data.get(CONF_GRID_EXPORT)
        )

        # Required/optional sensor reads: do NOT collapse None to 0.0.
        # Power sensors are read with power=True so that kW/MW values are
        # automatically normalised to watts.
        pv_production: float | None = _parse_sensor_float(
            self.hass, data.get(CONF_PV_POWER), power=True,
        )
        if uses_load_balance:
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
            if not uses_load_balance:
                self._track_sensor_availability(grid_export_entity, grid_export)
            grid_import = 0.0 if grid_export is not None else None

        load_power: float | None = _parse_sensor_float(
            self.hass, data.get(CONF_LOAD_POWER), power=True,
        )
        if uses_load_balance:
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
            charge = (
                _parse_sensor_float(self.hass, battery_charge_entity, power=True)
                if battery_charge_entity else 0.0
            )
            discharge = (
                _parse_sensor_float(self.hass, battery_discharge_entity, power=True)
                if battery_discharge_entity else 0.0
            )
            battery_power = charge - discharge if charge is not None and discharge is not None else None

        has_battery = any(data.get(key) for key in (
            CONF_BATTERY_POWER, CONF_BATTERY_CHARGE_POWER, CONF_BATTERY_DISCHARGE_POWER,
        ))
        if import_export_entity:
            if grid_export is None or grid_import is None or (has_battery and battery_power is None):
                excess_power = None
            else:
                excess_power = grid_export - grid_import + (battery_power if has_battery else 0.0)
        elif uses_load_balance:
            # Household load includes managed appliances, not battery charging.
            # Valid zero remains zero; an outage must not switch the topology.
            excess_power = (
                pv_production - load_power
                if pv_production is not None and load_power is not None else None
            )
        elif grid_export_entity:
            # Preserve the existing non-hybrid meter preference. Export-only
            # readings cannot quantify import; PV/load can supply that fallback.
            if grid_export is None:
                excess_power = None
            elif grid_export > 0:
                excess_power = grid_export
            elif data.get(CONF_PV_POWER) and data.get(CONF_LOAD_POWER):
                self._track_sensor_availability(data.get(CONF_PV_POWER), pv_production)
                self._track_sensor_availability(data.get(CONF_LOAD_POWER), load_power)
                excess_power = pv_production - load_power if pv_production is not None and load_power is not None else None
            else:
                excess_power = grid_export
        else:
            excess_power = None

        return PowerState(
            pv_production=pv_production,
            grid_export=grid_export,
            grid_import=grid_import,
            load_power=load_power,
            excess_power=excess_power,
            battery_soc=battery_soc,
            battery_power=battery_power,
            ev_soc=None,
            timestamp=dt_util.utcnow(),
        )

    def _power_for_optimizer(
        self, power: PowerState, configs: list[ApplianceConfig],
        states: dict[str, ApplianceState],
    ) -> tuple[PowerState, list[PowerState]]:
        """Apply today's battery policy without mutating raw telemetry/history."""
        data = self.config_entry.data
        self._battery_charge_reserve_w = 0.0
        self._battery_priority_status = "inactive"
        self._available_excess_power_w = power.excess_power
        reserve: float | None = 0.0
        strategy = data.get(CONF_BATTERY_STRATEGY, self.battery_strategy)
        has_battery = any(data.get(key) for key in (
            CONF_BATTERY_SOC, CONF_BATTERY_POWER, CONF_BATTERY_CHARGE_POWER,
            CONF_BATTERY_DISCHARGE_POWER,
        ))
        if strategy == BatteryStrategy.BATTERY_FIRST and has_battery:
            running_w = 0.0
            for config in configs:
                state = states.get(config.id)
                if state is None or not state.is_on:
                    continue
                if config.actual_power_entity or state.current_power > 0:
                    draw = state.current_power
                elif state.ev_connected is False:
                    draw = 0.0
                elif config.dynamic_current and state.current_amperage is not None:
                    draw = state.current_amperage * max(config.phases, 1) * self.optimizer.grid_voltage
                else:
                    draw = config.nominal_power
                running_w += max(draw, 0.0)
            cap_entity = data.get(CONF_INVERTER_BATTERY_MAX_CHARGE_POWER_ENTITY)
            live_limit = _parse_sensor_float(self.hass, cap_entity, power=True) if cap_entity else None
            if cap_entity and (cap_state := self.hass.states.get(cap_entity)) is not None:
                unit = cap_state.attributes.get("unit_of_measurement")
                if unit is not None and str(unit).lower().strip() not in ("", "w", "kw", "mw"):
                    live_limit = None  # An ampere limit needs an explicit power adapter.
            charging_w = power.battery_power
            if charging_w is None and not any(data.get(key) for key in (
                CONF_BATTERY_POWER, CONF_BATTERY_CHARGE_POWER, CONF_BATTERY_DISCHARGE_POWER,
            )) and power.grid_export is not None and power.grid_import is not None and power.excess_power is not None:
                charging_w = power.excess_power - (power.grid_export - power.grid_import)
            budget = battery_first_budget(
                power.excess_power, power.battery_soc,
                float(data.get(CONF_BATTERY_TARGET_SOC, 80)), charging_w, running_w,
                data.get(CONF_BATTERY_MAX_CHARGE_POWER_W), live_limit, bool(cap_entity),
            )
            self._battery_charge_reserve_w = reserve = budget.reserved_w
            self._battery_priority_status = budget.source
            self._available_excess_power_w = budget.available_w
        if self._available_excess_power_w is None:
            # Do not allocate from stale positive history when a current
            # required reading or the current policy budget is unavailable.
            return replace(power, excess_power=None), [replace(p, excess_power=None) for p in self.power_history]
        if reserve is None:
            # Unknown battery requirements forbid allocating positive surplus,
            # but a reliable negative balance must still reach normal SHED.
            return replace(power, excess_power=self._available_excess_power_w), [
                replace(p, excess_power=min(p.excess_power, 0.0) if p.excess_power is not None else None)
                for p in self.power_history
            ]
        if not reserve:
            return power, self.power_history
        return replace(power, excess_power=self._available_excess_power_w), [
            replace(p, excess_power=p.excess_power - reserve if p.excess_power is not None else None)
            for p in self.power_history
        ]

    # ------------------------------------------------------------------
    # Appliance configuration
    # ------------------------------------------------------------------

    def _effective_phases(self, appliance_id: str, data: dict) -> int:
        """Read actual phase count, keeping the last valid reading through gaps."""
        if not hasattr(self, "_effective_phase_counts"):
            self._effective_phase_counts = {}
        static = max(1, min(3, int(data.get(CONF_PHASES, 1))))
        entity = data.get("phase_count_entity")
        if not entity:
            self._effective_phase_counts.pop(appliance_id, None)
            return static
        reading = _parse_sensor_float(self.hass, entity)
        if reading is not None and reading in (1, 2, 3):
            self._effective_phase_counts[appliance_id] = int(reading)
        return self._effective_phase_counts.get(appliance_id, static)

    def _update_start_qualification(self, decisions: list[ControlDecision]) -> None:
        """Retain only continuously qualifying pending solar starts."""
        previous = getattr(self, "_solar_start_since", {})
        now = _time.monotonic()
        self._solar_start_since = {
            decision.appliance_id: previous.get(decision.appliance_id, now)
            for decision in decisions if decision.solar_start_qualified
        }

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
            paused = getattr(self, "appliance_paused", {}).get(subentry_id, False)
            if (not is_enabled or paused) and not override_active:
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
                phases=self._effective_phases(subentry_id, sub_data),
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
                current_update_interval=sub_data.get(CONF_CURRENT_UPDATE_INTERVAL, 0),
                current_min_change=sub_data.get(CONF_CURRENT_MIN_CHANGE, 0),
                override_active=override_active,
                max_daily_activations=max_activations,
                on_threshold=sub_data.get(CONF_ON_THRESHOLD, self.config_entry.data.get(CONF_ON_THRESHOLD)),
                completion_power_threshold=sub_data.get(CONF_COMPLETION_POWER_THRESHOLD),
                enable_condition_entity=sub_data.get("enable_condition_entity"),
                enable_condition_mode=sub_data.get("enable_condition_mode", "start_only"),
                start_delay=sub_data.get("start_delay", 0),
                phase_count_entity=sub_data.get("phase_count_entity"),
                remaining_runtime_entity=sub_data.get("remaining_runtime_entity"),
                require_contiguous_runtime=sub_data.get("require_contiguous_runtime", False),
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
            getattr(self, "_last_current_write", {}),
            getattr(self, "_effective_phase_counts", {}),
            getattr(self, "_solar_start_since", {}),
            self._activations_today,
            self._previous_is_on,
            self.appliance_enabled,
            getattr(self, "appliance_paused", {}),
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
        self, configs: list[ApplianceConfig], *, update_runtime: bool = True
    ) -> dict[str, ApplianceState]:
        """Read current state of each controlled appliance entity."""
        self._ensure_daily_date()
        states: dict[str, ApplianceState] = {}
        # Track all physical appliances even while their automatic control is
        # disabled. This also gives restored disabled appliances a live state.
        by_id = {config.id: config for config in configs}
        for appliance_id in self.config_entry.subentries:
            if appliance_id not in by_id:
                config = self._get_appliance_config_by_id(appliance_id)
                if config is not None:
                    by_id[appliance_id] = config

        for config in by_id.values():
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
            current_power_available = True
            if config.actual_power_entity:
                reading = _parse_sensor_float(self.hass, config.actual_power_entity, power=True)
                current_power_available = reading is not None
                current_power = reading if reading is not None else 0.0

            current_amperage: float | None = None
            if config.current_entity:
                current_amperage = _parse_sensor_float(
                    self.hass, config.current_entity
                )

            # During actuator readback lag, account for the accepted setpoint.
            # Otherwise an unchanged stale reading could invent a reduction.
            last_write = getattr(self, "_last_current_write", {}).get(config.id)
            last_target = self._last_applied_current.get(config.id)
            if (last_write is not None and last_target is not None
                    and (current_amperage is None or config.min_current <= current_amperage <= config.max_current)
                    and _time.monotonic() - last_write < max(60, config.current_update_interval)):
                current_amperage = last_target

            ev_connected: bool | None = None
            if config.ev_connected_entity:
                ev_connected = _parse_sensor_bool(
                    self.hass, config.ev_connected_entity
                )

            ev_soc: float | None = None
            if config.ev_soc_entity:
                ev_soc = _parse_sensor_float(self.hass, config.ev_soc_entity)

            if is_on and not config.actual_power_entity:
                # Nominal draw is the documented fallback for unmetered loads.
                # A configured real zero is never replaced by this estimate.
                if ev_connected is False:
                    current_power = 0.0
                elif config.dynamic_current and current_amperage is not None:
                    current_power = max(current_amperage, 0.0) * self.optimizer.grid_voltage * max(config.phases, 1)
                else:
                    current_power = config.nominal_power

            # Retrieve and update runtime from stored state
            previous = self.appliance_states.get(config.id)
            runtime_today = previous.runtime_today if previous else timedelta()
            energy_today = previous.energy_today if previous else 0.0
            last_state_change = previous.last_state_change if previous else None

            # Increment runtime and energy if the appliance is currently ON
            restored = config.id in getattr(self, "_restored_appliance_ids", set())
            if update_runtime and is_on and previous is not None and not restored:
                cycle_seconds = self.update_interval.total_seconds()
                # Gate runtime on actual power when completion threshold is configured
                counts_as_running = (
                    config.require_contiguous_runtime
                    or config.completion_power_threshold is None
                    or current_power >= config.completion_power_threshold
                )
                if counts_as_running:
                    runtime_today += timedelta(seconds=cycle_seconds)
                # Energy in kWh: power(W) * time(h)
                power_for_energy = current_power
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
                current_power_available=current_power_available,
                remaining_runtime_minutes=_parse_sensor_float(self.hass, config.remaining_runtime_entity) if config.remaining_runtime_entity else None,
                enable_condition=(_parse_sensor_bool(self.hass, config.enable_condition_entity) if config.enable_condition_entity else True),
                solar_start_elapsed=max(0, _time.monotonic() - getattr(self, "_solar_start_since", {}).get(config.id, _time.monotonic())),
                seconds_since_current_change=(
                    max(0.0, _time.monotonic() - self._last_current_write[config.id])
                    if config.id in getattr(self, "_last_current_write", {}) else None
                ),
            )
            states[config.id] = state

        if update_runtime:
            self.appliance_states = states
            self._restored_appliance_ids = set()
            self._schedule_daily_state_save()
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

        # A missing configured source invalidates the whole forecast; retain no stale plan.
        ha_states = {}
        for entity in [*getattr(self, "_forecast_entities", []), *getattr(self, "_forecast_tomorrow_entities", [])]:
            state_dict = _entity_state_dict(self.hass, entity)
            if state_dict:
                ha_states[entity] = state_dict
        try:
            forecast_data = self._forecast_provider.get_forecast(ha_states, now=dt_util.now())
        except Exception as err:
            if getattr(self, "_forecast_status", None) != "unavailable":
                _LOGGER.warning("Forecast provider error: %s", err)
            self._forecast_status = "unavailable"
            self._forecast_data = None
            self._forecast_error = str(err)
            self.current_plan = None
            return
        self._forecast_status = "available"
        self._forecast_data = forecast_data
        self._forecast_error = None

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
                appliance_states=self._get_appliance_states(appliance_configs, update_runtime=False),
                now=dt_util.now(),
                battery_config=battery_config,
                current_soc=battery_soc,
                export_limit=export_limit,
                dynamic_battery_charge_enabled=self.config_entry.data.get(
                    CONF_DYNAMIC_BATTERY_CHARGE_ENABLED, False
                ),
                battery_max_charge_power_w=int(
                    self.config_entry.data.get(CONF_BATTERY_MAX_CHARGE_POWER_W, 0) or 0
                ),
                battery_trickle_charge_power_w=int(
                    self.config_entry.data.get(
                        CONF_BATTERY_TRICKLE_CHARGE_POWER_W,
                        DEFAULT_BATTERY_TRICKLE_CHARGE_POWER_W,
                    )
                ),
            )
            _LOGGER.debug(
                "Planner generated plan with %d entries, confidence %.2f",
                len(self.current_plan.entries),
                self.current_plan.confidence,
            )
        except Exception as err:
            self.current_plan = None
            self._forecast_status = "planner_error"
            self._forecast_error = str(err)
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
            self._solar_start_since = {}
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

            # A pause relinquishes automatic control; an explicit override still acts.
            if (getattr(self, "appliance_paused", {}).get(decision.appliance_id, False)
                    and not self.appliance_overrides.get(decision.appliance_id, False)):
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
                        target = decision.target_current
                        observed = _parse_sensor_float(self.hass, appliance_config.current_entity)
                        last_target = self._last_applied_current.get(decision.appliance_id)
                        disconnected = _parse_sensor_bool(self.hass, appliance_config.ev_connected_entity) is False
                        writes = getattr(self, "_last_current_write", {})
                        elapsed = _time.monotonic() - writes[decision.appliance_id] if decision.appliance_id in writes else None
                        pending_other = (last_target is not None and last_target != target
                                         and elapsed is not None
                                         and elapsed < max(60, appliance_config.current_update_interval))
                        confirmed = (not pending_other and observed is not None
                                     and math.isclose(observed, target, abs_tol=1e-6))
                        pending = (last_target == target
                                   and (observed is None or appliance_config.min_current <= observed <= appliance_config.max_current)) and (
                            disconnected or
                            (elapsed is not None and elapsed < max(60, appliance_config.current_update_interval))
                        )
                        if confirmed or pending:
                            self._last_applied_current[decision.appliance_id] = target
                            if is_on or disconnected:
                                continue
                        else:
                            current_domain = appliance_config.current_entity.split(".")[0]
                            try:
                                async with asyncio.timeout(10):
                                    await self.hass.services.async_call(
                                        current_domain, "set_value",
                                        {"entity_id": appliance_config.current_entity, "value": target},
                                        blocking=True,
                                    )
                            except Exception as err:
                                _LOGGER.error("Failed to set current for %s: %s", appliance_config.name, err)
                                continue
                            self._last_applied_current[decision.appliance_id] = target
                            if not hasattr(self, "_last_current_write"):
                                self._last_current_write = {}
                            self._last_current_write[decision.appliance_id] = _time.monotonic()
                        if disconnected:
                            # A disconnected EV may be preset once; it has not
                            # physically started and must not produce ON events.
                            continue

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

        # Clamp against the target entity's declared min/max so HA doesn't reject
        # the service call. Some Sungrow Modbus packages declare the helper as
        # min=100 W, which silently rejects the optimizer's 0 W discharge block.
        state = self.hass.states.get(discharge_entity)
        if state is not None:
            entity_min = state.attributes.get("min")
            entity_max = state.attributes.get("max")
            original = target_value
            if entity_min is not None and target_value < entity_min:
                target_value = float(entity_min)
            if entity_max is not None and target_value > entity_max:
                target_value = float(entity_max)
            if target_value != original:
                _LOGGER.debug(
                    "Clamped battery discharge limit %.1fW -> %.1fW for %s (range %s..%s)",
                    original, target_value, discharge_entity, entity_min, entity_max,
                )

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

    async def _write_battery_max_charge(self, value_w: int) -> bool:
        """Write the battery cap; return success for an accepted or deduplicated write.

        A missing actuator or failed service call returns False so pending
        releases can be retried on the next controller cycle.

        Mirrors the clamping + dedupe + throttled-WARN pattern of
        _apply_battery_discharge_limit. See
        docs/dynamic-battery-charging.md
        section "Coordinator reactive loop / Write hysteresis".
        """
        data = self.config_entry.data
        entity_id = data.get(CONF_INVERTER_BATTERY_MAX_CHARGE_POWER_ENTITY)
        if not entity_id:
            return False

        domain = entity_id.split(".")[0] if "." in entity_id else "number"
        target_value: float = float(value_w)
        now_ts = datetime.now(timezone.utc)

        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            last = self._dyn_charge_last_warn_time
            if last is None or (now_ts - last).total_seconds() > 60:
                _LOGGER.warning(
                    "Battery max-charge entity %s unavailable; skipping write",
                    entity_id,
                )
                self._dyn_charge_last_warn_time = now_ts
            return False

        entity_min = state.attributes.get("min")
        entity_max = state.attributes.get("max")
        original = target_value
        if entity_min is not None and target_value < entity_min:
            target_value = float(entity_min)
        if entity_max is not None and target_value > entity_max:
            target_value = float(entity_max)
        if target_value != original:
            _LOGGER.debug(
                "Clamped battery max-charge %.1fW -> %.1fW for %s (range %s..%s)",
                original, target_value, entity_id, entity_min, entity_max,
            )

        # Dedupe: 50W deadband + 5min idle timeout.
        DEADBAND_W = 50.0
        IDLE_TIMEOUT_S = 300.0
        last_w = self._dyn_charge_last_written_w
        last_t = self._dyn_charge_last_write_time
        if (
            last_w is not None
            and last_t is not None
            and abs(target_value - last_w) < DEADBAND_W
            and (now_ts - last_t).total_seconds() < IDLE_TIMEOUT_S
        ):
            return True

        try:
            async with asyncio.timeout(10):
                await self.hass.services.async_call(
                    domain,
                    "set_value",
                    {"entity_id": entity_id, "value": target_value},
                    blocking=True,
                )
            self._dyn_charge_last_written_w = int(target_value)
            self._dyn_charge_last_write_time = now_ts
            return True
        except TimeoutError:
            _LOGGER.warning(
                "Service call timed out setting battery max-charge on %s",
                entity_id,
            )
        except Exception as err:
            _LOGGER.error("Failed to set battery max-charge: %s", err)
        return False

    async def _dispatch_dynamic_battery_charge(self, power_state: PowerState | None) -> None:
        """Per-cycle dispatch for the dynamic battery charge loop.

        Implements the pause/resume state machine described in
        docs/dynamic-battery-charging.md
        section "Pause / resume state machine".
        """
        should_run = _dyn_charge_should_run(self)
        max_w = int(self.config_entry.data.get(CONF_BATTERY_MAX_CHARGE_POWER_W, 0) or 0)

        # An accepted release ends this pause's responsibility; failures remain
        # pending so an unavailable actuator or transient service error can recover.
        if not should_run:
            valid_target = bool(self.config_entry.data.get(CONF_INVERTER_BATTERY_MAX_CHARGE_POWER_ENTITY)) and max_w > 0
            startup_time = getattr(self, "_startup_time", None)
            grace_elapsed = startup_time is None or (datetime.now() - startup_time).total_seconds() >= DEFAULT_STARTUP_GRACE_PERIOD
            cold_forecast_pause = (
                valid_target and grace_elapsed
                and self.config_entry.data.get(CONF_DYNAMIC_BATTERY_CHARGE_ENABLED, False)
                and not getattr(self, "_dyn_charge_self_disabled_reason", None)
                and getattr(self, "_forecast_status", "not_configured") in ("unavailable", "pending", "planner_error")
                and not getattr(self, "_dyn_charge_pause_released", False)
            )
            if self._dyn_charge_loop_active or cold_forecast_pause:
                self._dyn_charge_release_pending = True
                self._dyn_charge_loop_active = False
            if getattr(self, "_dyn_charge_release_pending", False) and valid_target:
                if await self._write_battery_max_charge(max_w) is True:
                    self._dyn_charge_release_pending = False
                    self._dyn_charge_pause_released = True
                    _LOGGER.info("Dynamic battery charge: paused, yielded cap to max")
            return

        # A valid resumed curve supersedes an unfinished release. The next pause
        # must release again, even if this one began during startup.
        self._dyn_charge_release_pending = False
        self._dyn_charge_pause_released = False

        # Falling edge of pause: resume; the deadband naturally lets the new value through.
        if should_run and not self._dyn_charge_loop_active:
            self._dyn_charge_loop_active = True
            _LOGGER.info("Dynamic battery charge: resumed dynamic control")

        curve = (
            self.current_plan.battery_charge_curve
            if getattr(self, "current_plan", None) is not None else None
        )
        trickle_w = int(self.config_entry.data.get(
            CONF_BATTERY_TRICKLE_CHARGE_POWER_W,
            DEFAULT_BATTERY_TRICKLE_CHARGE_POWER_W,
        ))
        ceiling_w = int(self.config_entry.data.get(CONF_EXPORT_LIMIT, 0) or 0)
        now_ts = datetime.now(timezone.utc)

        setpoint_w = compute_battery_charge_setpoint(
            now=now_ts,
            excess_w=power_state.excess_power if power_state else None,
            curve=curve,
            export_soft_ceiling_w=ceiling_w,
            battery_max_charge_power_w=max_w,
            battery_trickle_charge_power_w=trickle_w,
        )

        # Stash components for the status sensor (Task 6 will surface these).
        if curve is not None:
            for sp in curve.setpoints:
                if sp.start <= now_ts < sp.end:
                    self._dyn_charge_planned_w = sp.max_charge_w
                    break
            else:
                self._dyn_charge_planned_w = trickle_w
        else:
            self._dyn_charge_planned_w = trickle_w

        self._dyn_charge_reactive_w = (
            max(0, int((power_state.excess_power or 0) - ceiling_w))
            if power_state and power_state.excess_power is not None else 0
        )

        await self._write_battery_max_charge(setpoint_w)

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
            phases=self._effective_phases(appliance_id, sub_data),
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
            current_update_interval=sub_data.get(CONF_CURRENT_UPDATE_INTERVAL, 0),
            current_min_change=sub_data.get(CONF_CURRENT_MIN_CHANGE, 0),
            override_active=override_active,
            max_daily_activations=(
                int(sub_data[CONF_MAX_DAILY_ACTIVATIONS])
                if sub_data.get(CONF_MAX_DAILY_ACTIVATIONS) is not None
                else None
            ),
            on_threshold=sub_data.get(CONF_ON_THRESHOLD, self.config_entry.data.get(CONF_ON_THRESHOLD)),
            completion_power_threshold=sub_data.get(CONF_COMPLETION_POWER_THRESHOLD),
            enable_condition_entity=sub_data.get("enable_condition_entity"),
            enable_condition_mode=sub_data.get("enable_condition_mode", "start_only"),
            start_delay=sub_data.get("start_delay", 0),
            phase_count_entity=sub_data.get("phase_count_entity"),
            remaining_runtime_entity=sub_data.get("remaining_runtime_entity"),
            require_contiguous_runtime=sub_data.get("require_contiguous_runtime", False),
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
            "battery_charge_reserve_w": getattr(self, "_battery_charge_reserve_w", None),
            "available_excess_power_w": getattr(self, "_available_excess_power_w", None),
            "battery_priority_status": getattr(self, "_battery_priority_status", "inactive"),
            "power_history": list(self.power_history),
            "current_plan": self.current_plan,
            "forecast_status": getattr(self, "_forecast_status", "not_configured"),
            "forecast_remaining_today_kwh": getattr(getattr(self, "_forecast_data", None), "remaining_today_kwh", None),
            "forecast_tomorrow_total_kwh": getattr(getattr(self, "_forecast_data", None), "tomorrow_total_kwh", None),
            "forecast_interval_count": len(getattr(getattr(self, "_forecast_data", None), "hourly_breakdown", [])),
            "forecast_error": getattr(self, "_forecast_error", None),
            "forecast_sources": getattr(self, "_forecast_entities", []),
            "forecast_tomorrow_sources": getattr(self, "_forecast_tomorrow_entities", []),
            "control_decisions": list(self.control_decisions),
            "appliance_enabled": dict(self.appliance_enabled),
            "appliance_overrides": dict(self.appliance_overrides),
            "paused_appliances": dict(getattr(self, "appliance_paused", {})),
            "pending_stop_appliances": sorted(getattr(self, "_pending_stop_appliances", ())),
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
