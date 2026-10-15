"""Home Assistant adapter for safe, forecast-based battery grid charging.

This coordinator mixin owns measurements, persisted cleanup and service dispatch.
Economic planning remains in the HA-independent battery_planner module.
"""
from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta, timezone

from homeassistant.core import CoreState, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util

from .const import CONF_BATTERY_PV_FORECAST_FACTOR, DEFAULT_BATTERY_PV_FORECAST_FACTOR
from .inverter_control import InverterGridChargeController
from .models import InverterGridChargeConfig

_LOGGER = logging.getLogger(__name__)

# Some tariff providers round future-window EUR/kWh prices to four decimals
# while retaining full precision in the live sensor. Permit half that step.
_FOUR_DECIMAL_PRICE_TOLERANCE = .00005 + 1e-12

# Announcements describe sustained measured flow, independently of commands.
_GRID_NOTICE_START_W = 100.
_GRID_NOTICE_STOP_W = 50.
_GRID_NOTICE_START_SECONDS = 30
_GRID_NOTICE_STOP_SECONDS = 60
_GRID_NOTICE_MAX_AGE_SECONDS = 60
_GRID_NOTICE_DELIVERY_TIMEOUT_SECONDS = 2


@dataclass(frozen=True)
class _BatteryPlanningRequest:
    """Detached planner inputs; building a plan never accesses Home Assistant."""

    key: tuple
    now: datetime
    soc: float
    tariffs: tuple
    forecast: tuple
    profile: tuple
    config: object
    commitments: tuple

    @property
    def inputs(self):
        return self.key, self.soc, self.profile, self.config

    def build(self):
        from .battery_planner import build_battery_grid_plan
        return build_battery_grid_plan(
            self.now, self.soc, self.tariffs, self.forecast, self.profile, self.config,
            load_commitments=self.commitments,
        )


def _finite(value):
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    return number if math.isfinite(number) else None


class BatteryControlMixin:
    """Battery-specific coordinator methods, sharing its lifecycle and state."""

    def _init_battery_control(self):
        if "_battery_lock" in self.__dict__:
            return
        data = self.config_entry.data
        self._battery_lock = asyncio.Lock()
        self._battery_unloading = False
        self._grid_charge_cleanup_pending = bool(data.get("_grid_charge_cleanup_pending") or data.get("_grid_charge_engaged"))
        self._battery_hold_cleanup_pending = bool(data.get("_battery_hold_cleanup_pending"))
        self._battery_discharge_cleanup_pending = bool(data.get("_battery_discharge_cleanup_pending"))
        self._battery_recovery = (self._grid_charge_cleanup_pending or self._battery_hold_cleanup_pending
                                  or self._battery_discharge_cleanup_pending)
        self._battery_discharge_owner = None
        self._battery_discharge_template = None
        self._battery_discharge_last_action = None
        self._battery_numeric_hold_requested = False
        self._battery_numeric_hold_deadline = None
        self._battery_hold_engaged = False
        self._battery_hold_ctl = None
        self._battery_watchdog = None
        self._battery_watchdog_generation = 0
        self._battery_startup_unsub = None
        self._battery_retry_after = None
        self._battery_charge_stop_confirmed = False
        self._battery_hold_retry_after = None
        self._battery_target_latched = None
        self._battery_target_slot = None
        self._battery_grid_plan = None
        self._battery_plan_key = None
        self._battery_plan_created = None
        self._battery_planning_abort = None
        self._battery_charge_power = None
        self._battery_grid_notice_active = False
        self._battery_grid_notice_since = None
        self._battery_grid_notice_last_observed = None
        self._battery_slot_target = None
        self._battery_control_state = "idle"
        self._battery_control_reason = "automation_disabled"
        from .battery_journal import BatteryOwnershipJournal
        self._battery_journal = BatteryOwnershipJournal(self.hass, self.config_entry.entry_id)
        self._battery_storage_ready = False
        self._battery_charge_owner = self._inverter_ctl if self._grid_charge_cleanup_pending else None
        self._battery_hold_owner = None
        self._battery_load_store = None
        self._battery_load_hours = [[0., 0., None] for _ in range(24)]
        self._battery_load_last = None
        from .battery_load_forecast import RecentHouseholdLoad
        self._battery_recent_house_load = RecentHouseholdLoad()
        self._battery_load_sources = None
        self._battery_observed_load_until = None
        self._battery_observed_load_floor = 0.
        self._battery_observed_load_horizon = None
        self._battery_observed_load_reported_at = None
        self._battery_observed_house_runs = ()
        self._battery_observed_house_until = None
        self._battery_observed_limits = ()
        self._battery_observed_limits_until = None
        self._battery_load_commitments = ()
        self._battery_load_assumptions = ()
        shared_numeric = (data.get("battery_hold_entity") == data.get("battery_max_discharge_entity")
                          and str(data.get("battery_hold_entity", "")).split(".", 1)[0] in {"number", "input_number"})
        if (data.get("battery_hold_verified") or shared_numeric) and data.get("battery_hold_entity"):
            try:
                controller = InverterGridChargeController(self.hass, InverterGridChargeConfig(
                    enable_entity_id=data["battery_hold_entity"],
                    enable_engage_value=data.get("battery_hold_engage_value", ""),
                    enable_disengage_value=data.get("battery_hold_release_value", ""),
                    enable_feedback_entity_id=data.get("battery_hold_feedback_entity"),
                    enable_feedback_engage_value=data.get("battery_hold_feedback_engage_value"),
                    enable_feedback_disengage_value=data.get("battery_hold_feedback_release_value"),
                    enable_reassert_entity_id=data.get("battery_hold_reassert_entity"),
                ))
                if data.get("battery_hold_verified"):
                    self._battery_hold_ctl = controller
                if shared_numeric:
                    self._battery_discharge_template = controller
            except ValueError:
                _LOGGER.exception("Invalid battery hold mapping")
        if self._battery_hold_cleanup_pending:
            self._battery_hold_owner = self._battery_hold_ctl
        if self._battery_discharge_cleanup_pending:
            mapping = self._shared_discharge_mapping()
            if mapping is not None:
                self._battery_discharge_owner = InverterGridChargeController(self.hass, mapping)

    async def _restore_battery_ownership(self):
        self._battery_storage_ready = False
        try:
            charge, hold = await self._battery_journal.async_load()
            retry_after = getattr(self._battery_journal, "charge_retry_after", None)
            if isinstance(retry_after, datetime) and retry_after > dt_util.utcnow():
                self._delay_battery_restart(retry_after)
            if self._battery_journal.loaded_record_exists is True:
                # The awaited journal outranks delayed configuration flags.
                self._grid_charge_engaged = False
                self._grid_charge_cleanup_pending = charge is not None
                self._battery_hold_cleanup_pending = hold is not None
                self._battery_charge_owner = None
                self._battery_hold_owner = None
                self._battery_discharge_owner = None
                self._battery_discharge_cleanup_pending = False
            if charge is not None:
                self._battery_charge_owner = InverterGridChargeController(self.hass, charge)
                self._grid_charge_cleanup_pending = True
            if hold is not None:
                self._battery_hold_owner = InverterGridChargeController(self.hass, hold)
                self._battery_hold_cleanup_pending = True
            discharge = getattr(self._battery_journal, "discharge_config", None)
            if isinstance(discharge, InverterGridChargeConfig):
                self._battery_discharge_owner = InverterGridChargeController(self.hass, discharge)
                self._battery_discharge_cleanup_pending = True
            mapping = getattr(self._battery_journal, "discharge_mapping", None)
            if (isinstance(mapping, InverterGridChargeConfig) and self._battery_discharge_template is None
                    and mapping.enable_entity_id == self.config_entry.data.get("battery_max_discharge_entity")):
                self._battery_discharge_template = InverterGridChargeController(self.hass, mapping)
            self._battery_recovery = (self._grid_charge_cleanup_pending or self._battery_hold_cleanup_pending
                                      or self._battery_discharge_cleanup_pending)
            if getattr(self._battery_journal, "charge_retry_after_invalid", False) is True:
                raw_minutes = self.config_entry.data.get("grid_charge_engage_min_duration_minutes", 5)
                minutes = _finite(raw_minutes)
                if isinstance(raw_minutes, bool) or minutes is None or not 0 <= minutes <= 60:
                    minutes = 5.
                self._delay_battery_restart(dt_util.utcnow() + timedelta(minutes=minutes))
                # Preserve every validated original cleanup mapping. Keep the
                # start gate closed throughout the awaited repair; failed
                # persistence leaves those owners available for safe release.
                await self._battery_journal.async_save(
                    charge, hold,
                    discharge_config=discharge if isinstance(discharge, InverterGridChargeConfig) else None,
                    discharge_mapping=mapping if isinstance(mapping, InverterGridChargeConfig) else None,
                    charge_retry_after=self._battery_retry_after,
                )
                _LOGGER.warning("Repaired invalid battery restart deadline; configured restart delay applied")
            self._battery_storage_ready = True
        except (HomeAssistantError, OSError, ValueError, TypeError):
            self._battery_storage_ready = False
            self._battery_control_state = "fault"
            self._battery_control_reason = "ownership_storage_unavailable"
            _LOGGER.exception("Could not restore battery ownership; starts blocked")

    async def _save_battery_ownership(self):
        if not self._battery_storage_ready:
            raise RuntimeError("Battery ownership storage unavailable")
        charge = self._battery_charge_owner.config if self._grid_charge_cleanup_pending and self._battery_charge_owner else None
        hold = self._battery_hold_owner.config if self._battery_hold_cleanup_pending and self._battery_hold_owner else None
        extra = {}
        if self._battery_retry_after is not None:
            extra["charge_retry_after"] = self._battery_retry_after
        if self._battery_discharge_cleanup_pending and self._battery_discharge_owner:
            extra["discharge_config"] = self._battery_discharge_owner.config
        if mapping := self._shared_discharge_mapping():
            extra["discharge_mapping"] = mapping
        await self._battery_journal.async_save(charge, hold, **extra)

    def _persist_battery_controls(self):
        data = dict(self.config_entry.data)
        data.update({
            "_grid_charge_engaged": self._grid_charge_engaged,
            "_grid_charge_cleanup_pending": self._grid_charge_cleanup_pending,
            "_battery_hold_cleanup_pending": self._battery_hold_cleanup_pending,
            "_battery_discharge_cleanup_pending": self._battery_discharge_cleanup_pending,
        })
        keys = ("_grid_charge_engaged", "_grid_charge_cleanup_pending", "_battery_hold_cleanup_pending",
                "_battery_discharge_cleanup_pending")
        if any(bool(data[k]) != bool(self.config_entry.data.get(k, False)) for k in keys):
            self.hass.config_entries.async_update_entry(self.config_entry, data=data)

    def _persist_grid_charge_state(self, engaged):
        self._init_battery_control()
        self._grid_charge_engaged = engaged
        self._grid_charge_cleanup_pending = engaged
        self._persist_battery_controls()

    def _battery_read_number(self, entity_id, *, max_age=None, power=False):
        """Zero is valid; unknown, invalid and old telemetry are not."""
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if not isinstance(state, State):
            return None
        value = _finite(state.state)
        if value is None:
            return None
        if max_age is not None:
            stamp = getattr(state, "last_reported", None) or state.last_updated
            age = (dt_util.utcnow() - stamp).total_seconds()
            if age < -5 or age > max_age:
                return None
        if power:
            unit = str(state.attributes.get("unit_of_measurement", "W")).lower()
            if unit not in ("w", "kw"):
                return None
            value *= 1000 if unit == "kw" else 1
        return value

    def _battery_soc_now(self):
        d = self.config_entry.data
        soc = self._battery_read_number(d.get("battery_soc"), max_age=d.get("battery_input_max_age_seconds", 300))
        return soc if soc is not None and 0 <= soc <= 100 else None

    def _cancel_battery_watchdog(self):
        # A fired timer may already have a task waiting for the battery lock.
        # Invalidate that task as well as cancelling a timer that has not fired.
        self._battery_watchdog_generation += 1
        if self._battery_watchdog is not None:
            self._battery_watchdog.cancel()
            self._battery_watchdog = None

    def _battery_waiting_for_startup(self):
        """Helper bridges attach automation triggers only after core startup."""
        return getattr(self.hass, "state", None) in (CoreState.not_running, CoreState.starting)

    def _cancel_battery_startup_recovery(self):
        if self._battery_startup_unsub is not None:
            self._battery_startup_unsub()
            self._battery_startup_unsub = None

    def _defer_battery_startup_recovery(self):
        """Keep ownership pending without consuming a helper state transition."""
        if not (self._battery_recovery or self._grid_charge_engaged
                or self._grid_charge_cleanup_pending or self._battery_hold_cleanup_pending
                or self._battery_discharge_cleanup_pending):
            return
        self._battery_recovery = True
        self._battery_control_state = "idle"
        self._battery_control_reason = "startup_recovery_pending"
        if self._battery_startup_unsub is not None:
            return

        async def recover(_hass):
            self._battery_startup_unsub = None
            # Even a failed unload during startup still owns its cleanup. The
            # unloading flag remains set, so this callback can only release.
            if (self._battery_recovery or self._grid_charge_engaged
                    or self._grid_charge_cleanup_pending or self._battery_hold_cleanup_pending
                    or self._battery_discharge_cleanup_pending):
                await self.async_stop_battery_controls("startup_recovery")

        self._battery_startup_unsub = async_at_started(self.hass, recover)

    def _schedule_battery_watchdog(self, seconds, reason):
        """Only the latest armed deadline may release owned controls."""
        self._cancel_battery_watchdog()
        generation = self._battery_watchdog_generation

        async def release_if_current():
            async with self._battery_lock:
                if generation == self._battery_watchdog_generation:
                    await self._release_battery_locked(reason)

        def expired():
            if generation != self._battery_watchdog_generation:
                return
            self._battery_watchdog = None
            self._interrupt_battery_planning()
            self.hass.async_create_task(release_if_current())

        self._battery_watchdog = self.hass.loop.call_later(max(.1, seconds), expired)

    def _arm_battery_watchdog(self, end=None, *, require_fresh_soc=True):
        """A stalled update loop must not leave an owned charge/hold running."""
        seconds = float(self.config_entry.data.get("battery_input_max_age_seconds", 300))
        soc_state = self.hass.states.get(self.config_entry.data.get("battery_soc"))
        if require_fresh_soc and isinstance(soc_state, State):
            stamp = getattr(soc_state, "last_reported", None) or soc_state.last_updated
            seconds -= max(0., (dt_util.utcnow() - stamp).total_seconds())
        if end is not None:
            seconds = min(seconds, (end - dt_util.utcnow()).total_seconds())
        self._schedule_battery_watchdog(seconds, "watchdog_expired")

    async def _release_battery_locked(self, reason, *, release_discharge=True):
        """Cancellation also leaves an independently retryable cleanup."""
        if self._battery_waiting_for_startup():
            self._cancel_battery_watchdog()
            self._defer_battery_startup_recovery()
            return not self._battery_recovery
        try:
            return await self._release_battery_locked_impl(reason, release_discharge=release_discharge)
        except asyncio.CancelledError:
            self._battery_recovery = True
            self._arm_battery_watchdog(dt_util.utcnow() + timedelta(seconds=30))
            raise

    async def _release_battery_locked_impl(self, reason, *, release_discharge=True):
        """Try every owned release; retain ownership until confirmed."""
        self._cancel_battery_watchdog()
        ok = True
        self._battery_grid_notice_since = None
        had_charge = self._grid_charge_cleanup_pending or self._grid_charge_engaged
        had_hold = self._battery_hold_cleanup_pending
        had_discharge = release_discharge and self._battery_discharge_cleanup_pending
        if release_discharge:
            self._battery_numeric_hold_requested = False
            self._battery_numeric_hold_deadline = None
        if had_charge:
            self._grid_charge_cleanup_pending = True
            try:
                controller = self._battery_charge_owner or self._inverter_ctl
                if controller is None:
                    raise ValueError("Missing controller for pending battery release")
                await controller.disengage()
                if not self._battery_charge_stop_confirmed:
                    raw_minutes = self.config_entry.data.get("grid_charge_engage_min_duration_minutes", 5)
                    minutes = _finite(raw_minutes)
                    # Match the configured upper bound, retaining legacy zero.
                    # Invalid options must never interrupt confirmed cleanup.
                    if isinstance(raw_minutes, bool) or minutes is None or not 0 <= minutes <= 60:
                        minutes = 5.
                    self._delay_battery_restart(dt_util.utcnow() + timedelta(minutes=minutes))
                    self._battery_charge_stop_confirmed = True
                    self._battery_plan_key = None
                    self._battery_grid_plan = None
                self._grid_charge_engaged = False
                self._grid_charge_cleanup_pending = False
                self._grid_charge_engage_ts = None
                self._battery_charge_power = None
            except Exception as err:
                ok = False
                _LOGGER.warning("Battery charge release unconfirmed: %s", err)
        if self._battery_hold_cleanup_pending:
            try:
                controller = self._battery_hold_owner or self._battery_hold_ctl
                if controller is None:
                    raise ValueError("Missing controller for pending battery hold release")
                await controller.disengage()
                self._battery_hold_engaged = False
                self._battery_hold_cleanup_pending = False
            except Exception as err:
                ok = False
                _LOGGER.warning("Battery hold release unconfirmed: %s", err)
        if had_discharge:
            try:
                if self._battery_discharge_owner is None:
                    raise ValueError("Missing shared discharge cleanup mapping")
                await self._battery_discharge_owner.disengage()
                self._battery_discharge_cleanup_pending = False
                self._battery_hold_engaged = bool(self._battery_hold_cleanup_pending)
                self._last_discharge_limit = None
            except Exception as err:
                ok = False
                _LOGGER.warning("Shared battery discharge release unconfirmed: %s", err)
        if (had_charge or had_hold or had_discharge) and self._battery_storage_ready:
            try:
                await self._save_battery_ownership()
            except asyncio.CancelledError:
                self._grid_charge_cleanup_pending |= had_charge
                self._battery_hold_cleanup_pending |= had_hold
                self._battery_discharge_cleanup_pending |= had_discharge
                raise
            except Exception as err:
                self._grid_charge_cleanup_pending |= had_charge
                self._battery_hold_cleanup_pending |= had_hold
                self._battery_discharge_cleanup_pending |= had_discharge
                ok = False
                _LOGGER.warning("Could not persist battery release: %s", err)
        try:
            self._persist_battery_controls()
        except Exception as err:
            _LOGGER.warning("Could not update battery runtime flags: %s", err)
        self._battery_control_state = "idle" if ok else "fault"
        self._battery_control_reason = reason if ok else "release_unconfirmed"
        self._battery_recovery = not ok
        if not ok:
            # Retry even when the regular coordinator update loop has failed.
            self._schedule_battery_watchdog(30, reason)
        if ok:
            if release_discharge and not self._battery_discharge_cleanup_pending:
                self._battery_discharge_owner = None
            await self._finish_grid_charge_notification(reason)
            if not release_discharge and self._battery_discharge_cleanup_pending:
                self._arm_shared_discharge_watchdog()
        return ok

    def _delay_battery_restart(self, until):
        """Keep the latest confirmed-stop or command-failure deadline."""
        if self._battery_retry_after is None or until > self._battery_retry_after:
            self._battery_retry_after = until
            self._battery_plan_key = None

    async def _notify_grid_charge(self, event, value):
        method = getattr(getattr(self, "notifications", None), f"notify_battery_grid_charge_{event}", None)
        if method is not None:
            try:
                # Delivery failure must not hold the battery safety lock open.
                async with asyncio.timeout(_GRID_NOTICE_DELIVERY_TIMEOUT_SECONDS):
                    return bool(await method(value))
            except Exception:
                _LOGGER.exception("Battery charging notification failed")
        return False

    def _measured_battery_grid_flow(self):
        """Return grid contribution, charge timestamp and latest evidence time.

        Export-only grid import is a placeholder, so use fresh PV/load/battery
        balance when no signed meter is configured. A directional charging
        sensor is sufficient: its complementary zero-discharge template may
        legitimately remain unchanged for hours.
        """
        d = self.config_entry.data
        max_age = d.get("battery_input_max_age_seconds", 300)
        stamps = []

        def read(key):
            entity_id = d.get(key)
            value = self._battery_read_number(entity_id, power=True, max_age=max_age)
            if value is not None:
                state = self.hass.states.get(entity_id)
                stamps.append(getattr(state, "last_reported", None) or state.last_updated)
            return value

        grid = read("import_export_power") if d.get("import_export_power") else None
        export = read("grid_export") if not d.get("import_export_power") else None
        if d.get("import_export_power") and grid is None:
            return None  # Never silently change topology after a meter outage.
        no_import = ((grid is not None and grid >= 0)
                     or (export is not None and export > _GRID_NOTICE_STOP_W))
        if no_import:
            # Fresh zero import/export proves no grid charging independently of
            # the battery channel. Other fresh readings may confirm persistence.
            read("battery_power" if d.get("battery_power") else "battery_charge_power")
            read("pv_power")
            read("load_power")
            return 0., None, max(stamps)

        battery = read("battery_power" if d.get("battery_power") else "battery_charge_power")
        if battery is None or (not d.get("battery_power") and battery < 0):
            return None
        charge_stamp = stamps[-1]
        if battery <= 0:
            read("pv_power")
            read("load_power")
            return 0., charge_stamp, max(stamps)
        # Positive starts need a recent primary charge sample. Stable zero
        # templates and balance inputs retain the configured freshness limit,
        # so controller jitter cannot expire a zero before the stop debounce.
        if (dt_util.utcnow() - charge_stamp).total_seconds() > min(max_age, _GRID_NOTICE_MAX_AGE_SECONDS):
            return None
        if not d.get("battery_power"):
            # Simultaneously positive directions are conflicting telemetry.
            # Missing/stale complementary values do not override fresh charge.
            discharge = self._battery_read_number(d.get("battery_discharge_power"), power=True, max_age=max_age)
            if discharge is not None and discharge > _GRID_NOTICE_STOP_W:
                return None
        if grid is not None:
            contribution = min(battery, -grid)
        else:
            pv, load = read("pv_power"), read("load_power")
            if pv is None or load is None or pv < 0 or load < 0:
                return None
            contribution = min(battery, max(0., load + battery - pv))
        return contribution, charge_stamp, max(stamps)

    async def _finish_grid_charge_notification(self, reason):
        """Close only a successfully announced measured charging episode."""
        self._battery_grid_notice_since = None
        if self._battery_grid_notice_active:
            await self._notify_grid_charge("disengaged", reason)
            self._battery_grid_notice_active = False

    async def _observe_grid_charge_notification(self):
        """Debounce measured flow without affecting inverter control decisions."""
        now = dt_util.utcnow()
        previous = self._battery_grid_notice_last_observed
        self._battery_grid_notice_last_observed = now
        if previous is not None and not 0 <= (now - previous).total_seconds() <= 90:
            self._battery_grid_notice_since = None
        flow = self._measured_battery_grid_flow()
        if not self._grid_charge_engaged or flow is None:
            self._battery_grid_notice_since = None
            return
        power, charge_stamp, latest_stamp = flow
        active = self._battery_grid_notice_active
        stamp = latest_stamp if active else charge_stamp
        qualifies = power <= _GRID_NOTICE_STOP_W if active else power > _GRID_NOTICE_START_W
        if not qualifies:
            self._battery_grid_notice_since = None
            return
        since = self._battery_grid_notice_since
        if since is None or stamp < since[1] or now < since[0]:
            self._battery_grid_notice_since = now, stamp
            return
        duration = _GRID_NOTICE_STOP_SECONDS if active else _GRID_NOTICE_START_SECONDS
        # Starts require advancing primary charge samples as well as elapsed
        # observation time. Stops can use fresh grid/balance evidence even when
        # a directional zero-charge template itself stays unchanged.
        if min((now - since[0]).total_seconds(), (stamp - since[1]).total_seconds()) < duration:
            return
        if active:
            await self._finish_grid_charge_notification("measured_grid_charging_ended")
        else:
            self._battery_grid_notice_active = await self._notify_grid_charge("engaged", power)
            self._battery_grid_notice_since = None

    async def async_stop_battery_controls(self, reason="disabled"):
        self._init_battery_control()
        self._interrupt_battery_planning()
        async with self._battery_lock:
            return await self._release_battery_locked(reason)

    async def async_prepare_battery_unload(self):
        self._init_battery_control()
        self._battery_unloading = True
        result = await self.async_stop_battery_controls("unload")
        if result:
            self._cancel_battery_startup_recovery()
        await self.async_save_battery_load()
        return result

    async def _battery_preflight(self, power_state=None):
        self._init_battery_control()
        self._observe_battery_load()
        if self._battery_waiting_for_startup():
            self._defer_battery_startup_recovery()
            return
        if self._battery_recovery or not self.enabled or self._battery_unloading:
            await self.async_stop_battery_controls("startup_recovery" if self._battery_recovery else "disabled")
        elif (self._grid_charge_engaged or self._battery_hold_engaged) and self._battery_soc_now() is None:
            if self._shared_discharge_mapping() is None:
                await self.async_stop_battery_controls("soc_missing_or_stale")
            else:
                self._interrupt_battery_planning()
                async with self._battery_lock:
                    self._battery_numeric_hold_requested = False
                    if not self._battery_hold_cleanup_pending:
                        self._battery_hold_engaged = False
                    self._battery_numeric_hold_deadline = None
                    await self._release_battery_locked("soc_missing_or_stale", release_discharge=False)
                if self._battery_discharge_last_action is not None:
                    await self._apply_shared_discharge_limit(self._battery_discharge_last_action)

    def auto_should_engage_now(self):
        self._init_battery_control()
        if (self._battery_waiting_for_startup() or not self.enabled or self._battery_unloading or not self._battery_storage_ready
                or not self.config_entry.data.get("auto_battery_grid_charge")
                or not self.config_entry.data.get("allow_grid_charging")
                or (self._battery_retry_after is not None and dt_util.utcnow() < self._battery_retry_after)):
            return False
        slot = self._current_battery_slot()
        return bool(slot and slot.action == "charge" and self._battery_soc_now() is not None)

    def _current_battery_slot(self):
        plan = self._battery_grid_plan
        if plan is None or not plan.valid:
            return None
        now = dt_util.utcnow()
        return next((slot for slot in plan.slots if slot.start <= now < slot.end), None)

    def _battery_hold_window_end(self, slot):
        """Merge only exactly adjacent hold intervals of the validated plan."""
        if slot is None or slot.action != "hold":
            return None
        end = slot.end
        plan = self._battery_grid_plan
        if plan is None or not plan.valid:
            return end
        found = False
        for following in plan.slots:
            if not found:
                found = following is slot
                continue
            if following.action != "hold" or following.start != end or following.end <= end:
                break
            end = following.end
        return end

    async def _run_grid_charge_state_machine(self, tariff_info, power_state):
        self._init_battery_control()
        if self._battery_waiting_for_startup():
            self._defer_battery_startup_recovery()
            return
        async with self._battery_lock:
            self._battery_numeric_hold_requested = False
            if self._battery_recovery:
                await self._release_battery_locked("startup_recovery")
                return
            if not self.enabled or self._battery_unloading:
                await self._release_battery_locked("disabled")
                return
            async def release_control(reason, *, release_discharge=None):
                # Ordinary consumer protection has a separate owner. Plan
                # failures must not pulse its shared actuator to the default.
                if release_discharge is None:
                    release_discharge = self._shared_discharge_mapping() is None
                if not release_discharge:
                    self._battery_numeric_hold_requested = False
                    self._battery_numeric_hold_deadline = None
                    if not self._battery_hold_cleanup_pending:
                        self._battery_hold_engaged = False
                return await self._release_battery_locked(reason, release_discharge=release_discharge)
            if not self._battery_storage_ready:
                await release_control("ownership_storage_unavailable")
                return
            soc = self._battery_soc_now()
            if soc is None:
                await release_control("soc_missing_or_stale")
                return
            d = self.config_entry.data
            requested_power = _finite(d.get("battery_grid_charge_power_w"))
            if self._inverter_ctl is None or requested_power is None or requested_power <= 0:
                await release_control("charge_control_unavailable")
                return
            if self._inverter_ctl.confirmation_level != "physical":
                await release_control("physical_feedback_required")
                return
            manual = self.force_charge
            slot = None
            target = 100.
            if manual:
                action = "charge" if soc < target else "self_consumption"
            elif d.get("auto_battery_grid_charge") and d.get("allow_grid_charging"):
                if not d.get("inverter_force_charge_power_entity"):
                    await release_control("adjustable_power_control_required")
                    return
                native_charge_limit = _finite(d.get("battery_max_charge_power_w"))
                if native_charge_limit is None or native_charge_limit <= 0:
                    await release_control("native_charge_limit_required")
                    return
                try:
                    reason = await self._async_refresh_battery_grid_plan(tariff_info, power_state)
                except (ValueError, TypeError, AttributeError, ArithmeticError) as err:
                    _LOGGER.warning("Battery planning input rejected: %s", err)
                    reason = "invalid_planning_input"
                if reason:
                    self._battery_grid_plan = None
                    self._battery_plan_key = None
                    await release_control(reason)
                    return
                # Options and measurements can change while the pure search
                # runs. Use the same fresh limits that validated its result.
                d = self.config_entry.data
                tariff_info = self._latest_tariff or tariff_info
                soc = self._battery_soc_now()
                requested_power = _finite(d.get("battery_grid_charge_power_w"))
                native_charge_limit = _finite(d.get("battery_max_charge_power_w"))
                if soc is None:
                    await release_control("soc_missing_or_stale")
                    return
                if (requested_power is None or requested_power <= 0
                        or native_charge_limit is None or native_charge_limit <= 0):
                    await release_control("charge_control_unavailable")
                    return
                if self._inverter_ctl is None or self._inverter_ctl.confirmation_level != "physical":
                    await release_control("physical_feedback_required")
                    return
                slot = self._current_battery_slot()
                action = slot.action if slot else "self_consumption"
                if slot is not None:
                    current_pv = self._battery_read_number(d.get("pv_power"), power=True)
                    current_load = self._battery_read_number(d.get("load_power"), power=True)
                    current_surplus = max(0., current_pv - current_load)
                    requested_power = min(native_charge_limit, requested_power + current_surplus, slot.battery_charge_power_w)
                    target = min(float(d.get("battery_grid_target_soc", 80)), slot.soc_end)
                    price_limit = _finite(d.get("battery_charge_price_threshold", tariff_info.battery_charge_price_threshold))
                    if action == "charge" and (price_limit is None or tariff_info.current_price > price_limit):
                        action = "self_consumption"
            else:
                action = "self_consumption"
            now = dt_util.utcnow()
            restart_waiting = self._battery_retry_after is not None and now < self._battery_retry_after
            if action == "charge":
                target_slot = slot.end if slot else None
                if target_slot != self._battery_target_slot or (
                    self._battery_target_latched is not None
                    and target > self._battery_target_latched + float(d.get("battery_soc_hysteresis", 2))
                ):
                    self._battery_target_latched = None
                self._battery_target_slot = target_slot
                if soc >= target:
                    self._battery_target_latched = target
                    action = "self_consumption"
                elif not manual and self._battery_target_latched is not None:
                    hysteresis = float(d.get("battery_soc_hysteresis", 2))
                    if soc > min(target, self._battery_target_latched) - hysteresis:
                        action = "self_consumption"
                    else:
                        self._battery_target_latched = None
                if restart_waiting:
                    action = "self_consumption"
            if action == "charge":
                requested_power = self._feasible_battery_power(requested_power)
                if requested_power is None or requested_power <= 0:
                    await release_control("invalid_planned_power")
                    return
                if self._battery_hold_cleanup_pending and not await release_control("leave_hold"):
                    return
                try:
                    if self._grid_charge_engaged and self._battery_charge_power == requested_power:
                        await self._inverter_ctl.verify_engaged(requested_power)
                    else:
                        self._battery_charge_stop_confirmed = False
                        self._grid_charge_cleanup_pending = True
                        self._battery_charge_owner = self._inverter_ctl
                        await self._save_battery_ownership()
                        self._persist_battery_controls()
                        if (not self.enabled or self._battery_unloading or self.force_charge != manual
                                or self._battery_soc_now() is None):
                            await release_control("request_changed")
                            return
                        await self._release_solar_charge_cap()
                        if manual:
                            current_power = _finite(self.config_entry.data.get("battery_grid_charge_power_w"))
                            requested_power = (self._feasible_battery_power(min(requested_power, current_power))
                                               if current_power is not None and current_power > 0 else None)
                            if requested_power is None or requested_power <= 0:
                                await release_control("invalid_planned_power")
                                return
                        if not manual:
                            refreshed = await self._battery_action_after_wait("charge", tariff_info, power_state)
                            if refreshed is None:
                                await release_control("planning_input_changed")
                                return
                            slot = refreshed
                            d = self.config_entry.data
                            tariff_info = self._latest_tariff or tariff_info
                            target = min(float(d.get("battery_grid_target_soc", 80)), slot.soc_end)
                            pv = self._battery_read_number(d.get("pv_power"), power=True)
                            load = self._battery_read_number(d.get("load_power"), power=True)
                            requested_power = self._feasible_battery_power(min(
                                requested_power, slot.battery_charge_power_w,
                                float(d["battery_max_charge_power_w"]),
                                float(d["battery_grid_charge_power_w"]) + max(0., pv - load),
                            ))
                            if requested_power is None or requested_power <= 0:
                                await release_control("invalid_planned_power")
                                return
                        if (not self.enabled or self._battery_unloading or self.force_charge != manual
                                or self._battery_soc_now() is None or self._battery_soc_now() >= target
                                or (slot is not None and dt_util.utcnow() >= slot.end)):
                            await release_control("request_changed")
                            return
                        await self._inverter_ctl.engage(requested_power)
                        self._grid_charge_engaged = True
                        self._battery_charge_power = requested_power
                        self._persist_battery_controls()
                    if (not self.enabled or self._battery_unloading or self.force_charge != manual
                            or self._battery_soc_now() is None
                            or (slot is not None and dt_util.utcnow() >= slot.end)):
                        await release_control("request_changed")
                        return
                    self._battery_control_state = "charging"
                    self._battery_control_reason = "manual" if manual else "economic_plan"
                    self._arm_battery_watchdog(slot.end if slot else None)
                    await self._observe_grid_charge_notification()
                except asyncio.CancelledError:
                    self._battery_recovery = True
                    self._arm_battery_watchdog(dt_util.utcnow())
                    raise
                except Exception as err:
                    _LOGGER.warning("Battery start/confirmation failed: %s", err)
                    self._delay_battery_restart(dt_util.utcnow() + timedelta(minutes=5))
                    await release_control("charge_unconfirmed")
                return
            if action == "hold" and self._battery_hold_ctl is not None:
                if self._battery_hold_retry_after is not None and now < self._battery_hold_retry_after:
                    await release_control("retry_backoff")
                    return
                hold_end = self._battery_hold_window_end(slot)
                if self._uses_shared_numeric_hold():
                    if self._grid_charge_cleanup_pending and not await release_control(
                        "enter_hold", release_discharge=False,
                    ):
                        return
                    self._battery_numeric_hold_requested = True
                    self._battery_numeric_hold_deadline = hold_end
                    return
                if self._grid_charge_cleanup_pending and not await release_control("enter_hold"):
                    return
                try:
                    if self._battery_hold_engaged:
                        await self._battery_hold_ctl.verify_engaged(0)
                    else:
                        self._battery_hold_cleanup_pending = True
                        self._battery_hold_owner = self._battery_hold_ctl
                        await self._save_battery_ownership()
                        self._persist_battery_controls()
                        if (not self.enabled or self._battery_unloading or self.force_charge
                                or self._battery_soc_now() is None or dt_util.utcnow() >= hold_end):
                            await release_control("request_changed")
                            return
                        await self._release_solar_charge_cap()
                        refreshed = await self._battery_action_after_wait("hold", tariff_info, power_state)
                        if refreshed is None:
                            await release_control("planning_input_changed")
                            return
                        slot = refreshed
                        hold_end = self._battery_hold_window_end(slot)
                        if (not self.enabled or self._battery_unloading or self.force_charge
                                or self._battery_soc_now() is None or dt_util.utcnow() >= hold_end):
                            await release_control("request_changed")
                            return
                        await self._battery_hold_ctl.engage(0)
                        self._battery_hold_engaged = True
                    refreshed = await self._battery_action_after_wait("hold", tariff_info, power_state)
                    if refreshed is None:
                        await release_control("planning_input_changed")
                        return
                    hold_end = self._battery_hold_window_end(refreshed)
                    if (not self.enabled or self._battery_unloading or self.force_charge
                            or self._battery_soc_now() is None or dt_util.utcnow() >= hold_end):
                        await release_control("request_changed")
                        return
                    self._battery_control_state = "holding"
                    self._battery_control_reason = "later_expensive_window"
                    self._arm_battery_watchdog(hold_end)
                except asyncio.CancelledError:
                    self._battery_recovery = True
                    self._arm_battery_watchdog(dt_util.utcnow())
                    raise
                except Exception as err:
                    _LOGGER.warning("Battery hold unconfirmed: %s", err)
                    self._battery_plan_key = None
                    self._battery_hold_retry_after = now + timedelta(minutes=5)
                    await release_control("hold_unconfirmed")
                return
            released = await self._release_battery_locked(
                "restart_delay" if restart_waiting else "target_reached" if soc >= target else "self_consumption",
                release_discharge=self._shared_discharge_mapping() is None,
            )
            if released and d.get("auto_battery_grid_charge"):
                self._battery_control_state = "self_consumption"

    async def _battery_action_after_wait(self, action, tariff_info, power_state):
        """Revalidate inputs after asynchronous preparation before a new write."""
        try:
            reason = await self._async_refresh_battery_grid_plan(tariff_info, power_state)
            slot = self._current_battery_slot()
            if not reason and slot is not None and slot.action == action:
                return slot
        except (ValueError, TypeError, AttributeError, ArithmeticError):
            pass
        self._battery_grid_plan = None
        self._battery_plan_key = None
        return None

    async def _release_solar_charge_cap(self):
        """Yield a previous solar cap before executing a grid/hold schedule.

        This also covers a cold restart where the previous cap still exists
        at the inverter but this process has never run the solar control loop.
        """
        d = self.config_entry.data
        if not d.get("dynamic_battery_charge_enabled"):
            return
        maximum = _finite(d.get("battery_max_charge_power_w"))
        if not d.get("inverter_battery_max_charge_power_entity") or maximum is None or maximum <= 0:
            raise ValueError("Solar charge cap cannot be released")
        self._dyn_charge_loop_active = False
        self._dyn_charge_release_pending = True
        self._dyn_charge_last_written_w = None
        if await self._write_battery_max_charge(int(maximum)) is not True:
            raise RuntimeError("Solar charge cap release unconfirmed")
        self._dyn_charge_release_pending = False
        self._dyn_charge_pause_released = True

    def _battery_charge_power_constraints(self):
        """Read actuator limits in watts for both planning and dispatch."""
        entity_id = self.config_entry.data.get("inverter_force_charge_power_entity")
        if not entity_id:
            return None, None, None
        state = self.hass.states.get(entity_id)
        if not isinstance(state, State) or state.state in ("unknown", "unavailable"):
            return None
        unit = state.attributes.get("unit_of_measurement")
        if unit not in (None, "", "W", "kW"):
            return None
        multiplier = 1000 if unit == "kW" else 1
        raw_minimum = state.attributes.get("min", 0)
        raw_maximum = state.attributes.get("max")
        raw_step = state.attributes.get("step", 1 / multiplier)
        if any(isinstance(value, bool) for value in (raw_minimum, raw_maximum, raw_step)):
            return None
        minimum, maximum, step = _finite(raw_minimum), _finite(raw_maximum), _finite(raw_step)
        if (minimum is None or minimum < 0 or step is None or step <= 0
                or (raw_maximum is not None and (maximum is None or maximum <= 0 or minimum > maximum))):
            return None
        minimum, step = minimum * multiplier, step * multiplier
        maximum = maximum * multiplier if maximum is not None else None
        if not all(math.isfinite(value) for value in (minimum, step, maximum) if value is not None):
            return None
        return minimum, maximum, step

    def _feasible_battery_power(self, watts):
        """Round down to actuator steps so a plan cannot request extra energy."""
        if not self.config_entry.data.get("inverter_force_charge_power_entity"):
            return watts
        constraints = self._battery_charge_power_constraints()
        if constraints is None:
            return None
        minimum, maximum, step = constraints
        value = min(watts, maximum) if maximum is not None else watts
        value = minimum + math.floor((value - minimum + 1e-9) / step) * step
        return value if value >= minimum else None

    def _shared_discharge_mapping(self):
        """Return the confirmed numeric mapping for the shared discharge limit."""
        entity = self.config_entry.data.get("battery_max_discharge_entity")
        for controller in (getattr(self, "_battery_hold_ctl", None), getattr(self, "_battery_discharge_template", None)):
            if (controller is not None and controller.confirmation_level == "physical"
                    and isinstance(controller.config, InverterGridChargeConfig)
                    and controller.config.enable_entity_id == entity
                    and entity.split(".", 1)[0] in {"number", "input_number"}):
                return controller.config
        return None

    def _uses_shared_numeric_hold(self):
        """A remembered consumer capability never enables economic holding."""
        mapping = self._shared_discharge_mapping()
        data = self.config_entry.data
        return bool(mapping and self._battery_hold_ctl is not None
                    and data.get("battery_hold_verified")
                    and data.get("battery_hold_entity") == mapping.enable_entity_id
                    and self._battery_hold_ctl.config == mapping)

    @staticmethod
    def _discharge_unit_multiplier(state):
        if not isinstance(state, State) or state.state in {"unknown", "unavailable"}:
            raise ValueError("Discharge control or feedback is unavailable")
        unit = state.attributes.get("unit_of_measurement")
        if unit not in {None, "", "W", "kW"}:
            raise ValueError("Unsupported discharge power unit")
        return 1000. if unit == "kW" else 1.

    async def _numeric_hold_still_requested(self):
        if (not self._uses_shared_numeric_hold() or not self._battery_numeric_hold_requested
                or not self.enabled or self._battery_unloading
                or self.force_charge or self._battery_numeric_hold_deadline is None
                or dt_util.utcnow() >= self._battery_numeric_hold_deadline or self._battery_soc_now() is None):
            return False
        data = self.config_entry.data
        for key in ("pv_power", "load_power"):
            watts = self._battery_read_number(data.get(key), power=True,
                                             max_age=data.get("battery_input_max_age_seconds", 300))
            if watts is None or watts < 0:
                return False
        slot = await self._battery_action_after_wait("hold", self._latest_tariff, self._latest_power_state)
        if slot is None:
            return False
        self._battery_numeric_hold_deadline = min(
            self._battery_numeric_hold_deadline, self._battery_hold_window_end(slot),
        )
        return dt_util.utcnow() < self._battery_numeric_hold_deadline

    async def _apply_shared_discharge_limit(self, action, *, watchdog_generation=None):
        """Serialize economic hold, consumer caps and their confirmed cleanup."""
        self._init_battery_control()
        async with self._battery_lock:
            if watchdog_generation is not None and (
                watchdog_generation != self._battery_watchdog_generation
                or self._battery_discharge_last_action is not action
            ):
                # The boundary callback releases and reacquires this lock. A
                # newer consumer request must win even if it simply released
                # its cap and therefore did not arm another watchdog.
                return
            if self._battery_waiting_for_startup():
                self._defer_battery_startup_recovery()
                return
            if self._battery_recovery:
                return
            if not self.enabled or self._battery_unloading:
                await self._release_battery_locked("disabled")
                return
            if not self._battery_storage_ready:
                self._battery_control_state = "fault"
                self._battery_control_reason = "ownership_storage_unavailable"
                return
            self._battery_discharge_last_action = action
            try:
                mapping = self._shared_discharge_mapping()
                if mapping is None:
                    raise ValueError("Shared discharge control needs physical feedback")
                state = self.hass.states.get(mapping.enable_entity_id)
                multiplier = self._discharge_unit_multiplier(state)
                feedback = self.hass.states.get(mapping.enable_feedback_entity_id or mapping.enable_entity_id)
                feedback_multiplier = self._discharge_unit_multiplier(feedback)
                fallback = _finite(mapping.enable_disengage_value)
                hold = _finite(mapping.enable_engage_value)
                if fallback is None or hold is None or not 0 <= hold < fallback:
                    raise ValueError("Invalid shared discharge hold/release values")
                fallback, hold = fallback * multiplier, hold * multiplier
                attrs = state.attributes
                raw_limits = (attrs.get("min", 0), attrs.get("max", fallback / multiplier),
                              attrs.get("step", 1 / multiplier))
                if any(isinstance(value, bool) or _finite(value) is None for value in raw_limits):
                    raise ValueError("Invalid shared discharge actuator limits")
                minimum, maximum, step = (float(value) * multiplier for value in raw_limits)
                if not (0 <= minimum <= maximum and step > 0 and minimum <= fallback <= maximum):
                    raise ValueError("Invalid shared discharge actuator range")
                requested = (action.max_discharge_watts if action.should_limit and action.max_discharge_watts is not None
                             else self.config_entry.data.get("battery_max_discharge_default"))
                if requested is None:
                    requested = fallback
                if isinstance(requested, bool) or _finite(requested) is None or float(requested) < 0:
                    raise ValueError("Invalid ordinary discharge limit")
                ordinary = min(fallback, maximum, max(minimum, float(requested)))
                ordinary = minimum + math.floor((ordinary - minimum + 1e-9) / step) * step
                # An invalidated economic hold hands directly to the ordinary
                # consumer cap on the second pass, without a default-power pulse.
                for _ in range(2):
                    requested_hold = await self._numeric_hold_still_requested()
                    if not requested_hold:
                        self._battery_numeric_hold_requested = False
                    if requested_hold and (not minimum <= hold <= maximum
                                           or abs((hold - minimum) / step - round((hold - minimum) / step)) > 1e-6):
                        raise ValueError("Configured hold value is not an executable setpoint")
                    target = min(ordinary, hold) if requested_hold else ordinary
                    if target < fallback:
                        current = self._battery_discharge_owner
                        # Preserve the original fallback mapping while changing a cap.
                        original = current.config if self._battery_discharge_cleanup_pending and current else mapping
                        effective = replace(original, enable_engage_value=str(target / multiplier),
                                            enable_feedback_engage_value=str(target / feedback_multiplier))
                        controller = InverterGridChargeController(self.hass, effective)
                        if (current is not None and self._battery_discharge_cleanup_pending
                                and current.config == effective and self._last_discharge_limit == target):
                            await current.verify_engaged(0)
                        else:
                            self._battery_discharge_owner = controller
                            self._battery_discharge_cleanup_pending = True
                            await self._save_battery_ownership()
                            self._persist_battery_controls()
                            if not self.enabled or self._battery_unloading:
                                await self._release_battery_locked("request_changed")
                                return
                            if requested_hold:
                                if not self._battery_hold_engaged:
                                    await self._release_solar_charge_cap()
                                if not await self._numeric_hold_still_requested():
                                    self._battery_numeric_hold_requested = False
                                    continue
                            await controller.engage(0)
                        self._last_discharge_limit = target
                    else:
                        controller = (self._battery_discharge_owner if self._battery_discharge_cleanup_pending
                                      else None) or InverterGridChargeController(self.hass, mapping)
                        if self._battery_discharge_cleanup_pending or self._last_discharge_limit != fallback:
                            await controller.disengage()
                        else:
                            await controller.verify_disengaged()
                        had_discharge = self._battery_discharge_cleanup_pending
                        self._battery_discharge_cleanup_pending = False
                        try:
                            await self._save_battery_ownership()
                            self._persist_battery_controls()
                        except BaseException:
                            self._battery_discharge_cleanup_pending = had_discharge
                            raise
                        self._last_discharge_limit = fallback
                        self._battery_discharge_owner = None
                    if not self.enabled or self._battery_unloading:
                        await self._release_battery_locked("request_changed")
                        return
                    if requested_hold and not await self._numeric_hold_still_requested():
                        self._battery_numeric_hold_requested = False
                        continue
                    break
                self._battery_hold_engaged = requested_hold or self._battery_hold_cleanup_pending
                if requested_hold:
                    self._battery_control_state = "holding"
                    self._battery_control_reason = "later_expensive_window"
                elif self._battery_control_state == "holding" and not self._battery_hold_cleanup_pending:
                    self._battery_control_state = "self_consumption"
                    self._battery_control_reason = "self_consumption"
                if self._battery_discharge_cleanup_pending:
                    self._arm_shared_discharge_watchdog()
            except asyncio.CancelledError:
                self._battery_recovery = True
                self._arm_battery_watchdog(dt_util.utcnow() + timedelta(seconds=30))
                raise
            except Exception as err:
                _LOGGER.warning("Shared discharge command failed: %s", err)
                self._battery_hold_retry_after = dt_util.utcnow() + timedelta(minutes=5)
                self._last_discharge_limit = None
                await self._release_battery_locked("discharge_limit_unconfirmed")

    def _arm_shared_discharge_watchdog(self):
        """At a hold boundary hand back to the last consumer limit directly."""
        if self._grid_charge_engaged:
            slot = self._current_battery_slot()
            self._arm_battery_watchdog(slot.end if slot else None)
        elif self._battery_hold_cleanup_pending:
            slot = self._current_battery_slot()
            self._arm_battery_watchdog(self._battery_hold_window_end(slot) or dt_util.utcnow())
        elif self._battery_hold_engaged and self._battery_numeric_hold_deadline is not None:
            end = self._battery_numeric_hold_deadline
            seconds = (end - dt_util.utcnow()).total_seconds()
            freshness = self.config_entry.data.get("battery_input_max_age_seconds", 300)
            soc = self.hass.states.get(self.config_entry.data.get("battery_soc"))
            if isinstance(soc, State):
                stamp = getattr(soc, "last_reported", None) or soc.last_updated
                freshness -= max(0., (dt_util.utcnow() - stamp).total_seconds())
            if 0 < seconds < freshness:
                self._cancel_battery_watchdog()
                generation = self._battery_watchdog_generation
                async def handoff():
                    if generation == self._battery_watchdog_generation:
                        self._interrupt_battery_planning()
                    async with self._battery_lock:
                        if (generation != self._battery_watchdog_generation
                                or self._battery_numeric_hold_deadline != end or dt_util.utcnow() < end):
                            return
                        self._battery_numeric_hold_requested = False
                        self._battery_numeric_hold_deadline = None
                        action = self._battery_discharge_last_action
                    if action is not None:
                        await self._apply_shared_discharge_limit(action, watchdog_generation=generation)
                self._battery_watchdog = self.hass.loop.call_later(
                    seconds, lambda: self.hass.async_create_task(handoff()))
            else:
                self._arm_battery_watchdog(require_fresh_soc=True)
        else:
            self._arm_battery_watchdog(require_fresh_soc=False)

    def _battery_load_policy_allows_run(self, appliance_id, data, now):
        """Mirror immediate gates relevant to an already observed active run."""
        if self.appliance_overrides.get(appliance_id, False) or data.get("helper_only"):
            return True
        for key in ("ev_connected_entity", "enable_condition_entity"):
            if key == "enable_condition_entity" and data.get("enable_condition_mode", "start_only") != "while_running":
                continue
            if entity := data.get(key):
                state = self.hass.states.get(entity)
                if not isinstance(state, State) or state.state not in ("on", "true", "True", "1"):
                    return False
        target = _finite(data.get("ev_target_soc"))
        soc = self._battery_read_number(data.get("ev_soc_entity"), max_age=300)
        if target is not None and soc is not None and soc >= target:
            return False
        if data.get("on_only") and not data.get("dynamic_current"):
            return True  # Fixed on-only runs bypass the operating-time gate.
        try:
            start = time.fromisoformat(data["start_after"]) if data.get("start_after") else None
            end = time.fromisoformat(data["end_before"]) if data.get("end_before") else None
        except (TypeError, ValueError):
            return False
        return self.optimizer._is_within_time_window(now.time(), start, end)

    def _battery_load_funding(self, appliance_id, data, now, tariff):
        """Identify the present funding policy and its next known transition."""
        threshold = _finite(data.get("cheap_price_threshold"))
        if threshold is None:
            threshold = _finite(tariff.cheap_price_threshold)
        funded = bool(not self.appliance_overrides.get(appliance_id, False)
                      and not data.get("helper_only")
                      and not (data.get("on_only") and not data.get("dynamic_current"))
                      and data.get("allow_grid_supplement") and threshold is not None
                      and (tariff.current_price <= threshold or tariff.current_price < tariff.feed_in_tariff))
        if not funded:
            return False, None
        cursor = now
        for window in sorted(tariff.windows, key=lambda row: row.start):
            if window.end <= now:
                continue
            if window.start > cursor:
                break
            if not (window.price <= threshold or window.price < tariff.feed_in_tariff):
                return True, cursor
            cursor = max(cursor, window.end)
        # The end of supplied data does not promise the end of cheap funding.
        return True, None

    def _battery_load_solar_cannot_continue(self, appliance_id, data, when):
        """Allow a tariff stop estimate only when forecast solar cannot sustain it."""
        forecast = getattr(self, "_forecast_data", None)
        profile = self._battery_load_profile()
        if forecast is None or profile is None:
            return False
        rows = [row for row in forecast.hourly_breakdown if row.start <= when < row.end]
        if len(rows) != 1:
            return False
        row = rows[0]
        duration = (row.end.astimezone(timezone.utc) - row.start.astimezone(timezone.utc)).total_seconds()
        solar = _finite(row.expected_kwh)
        factor = _finite(self.config_entry.data.get(CONF_BATTERY_PV_FORECAST_FACTOR, DEFAULT_BATTERY_PV_FORECAST_FACTOR))
        if duration <= 0 or solar is None or solar < 0 or factor is None or not .1 <= factor <= 1:
            return False
        minimum = _finite(data.get("nominal_power"))
        if data.get("dynamic_current"):
            amps = _finite(data.get("min_current", 6))
            voltage = _finite(self.config_entry.data.get("grid_voltage", 230))
            phases = self._effective_phases(appliance_id, data)
            minimum = amps * voltage * phases if amps is not None and voltage is not None else None
        if minimum is None or minimum <= 0:
            return False
        available = solar * factor * 3_600_000 / duration - profile[when.astimezone(dt_util.now().tzinfo).hour]
        return available + 1e-6 < minimum

    def _battery_load_run_end(self, appliance_id, data, now, tariff):
        """Bound an observed run by actual stopping rules, never minimum demand.

        Cheap-window continuation is an estimate for a currently running load.
        An advisory appliance plan does not promise a future run or a stop.
        """
        bounds = []
        override = self.appliance_overrides.get(appliance_id, False)
        on_only = data.get("on_only", False)
        helper_only = data.get("helper_only", False)
        runtime_entity = data.get("remaining_runtime_entity")
        if runtime_entity and not override and not on_only and not helper_only:
            remaining = self._battery_read_number(runtime_entity, max_age=300)
            if remaining is None or remaining <= 0:
                return None, False
            state = self.hass.states.get(runtime_entity)
            stamp = getattr(state, "last_reported", None) or state.last_updated
            bounds.append(stamp + timedelta(minutes=remaining))
        maximum = self.appliance_max_daily_runtime.get(appliance_id, data.get("max_daily_runtime"))
        if maximum is not None:
            maximum = _finite(maximum)
            state = self.appliance_states.get(appliance_id)
            if maximum is None or state is None:
                return None, False
            end = now.astimezone(timezone.utc) + timedelta(minutes=maximum) - state.runtime_today
            midnight = datetime.combine(now.date() + timedelta(days=1), time(), tzinfo=now.tzinfo)
            # The daily counter resets at local midnight; today's remaining
            # allowance cannot promise a stop on the following day.
            if end < midnight:
                bounds.append(end)
        if not override and not helper_only and not (on_only and not data.get("dynamic_current")) and data.get("end_before"):
            try:
                end_time = time.fromisoformat(data["end_before"])
                end = datetime.combine(now.date(), end_time, tzinfo=now.tzinfo)
                start_time = time.fromisoformat(data["start_after"]) if data.get("start_after") else None
            except (TypeError, ValueError):
                return None, False
            if start_time is not None and start_time >= end_time and now.time() >= start_time:
                end += timedelta(days=1)
            bounds.append(end)
        funded, funding_end = self._battery_load_funding(appliance_id, data, now, tariff)
        if (funding_end is not None and not on_only
                and self._battery_load_solar_cannot_continue(appliance_id, data, funding_end)):
            bounds.append(funding_end)
        end = min(bounds) if bounds else None
        if on_only and funded and funding_end is not None and (end is None or end > funding_end):
            # Dynamic on-only loads can continue at another current after cheap
            # funding ends. Do not invent that future demand or retain its cap.
            return None, funded
        return (end if end is not None and end > now else None), funded

    def _reset_battery_load_forecast(self):
        """Discard observations whose household/EV composition is unknown."""
        self._battery_recent_house_load.reset()
        self._battery_load_sources = None
        self._battery_observed_load_until = None
        self._battery_observed_load_floor = 0.
        self._battery_observed_load_horizon = None
        self._battery_observed_load_reported_at = None
        self._battery_observed_house_runs = ()
        self._battery_observed_house_until = None

    def _battery_load_context(self, now, tariff, profile):
        """Project observed major loads without adding household history twice.

        Unmapped demand earns a two-to-thirty-minute outlook from its duration.
        A higher raw reading has a separate sixty-second observation window.
        Managed metered runs can extend to a known policy boundary. EV demand is
        separate because it was excluded when learning the household profile.
        """
        from .battery_planner import BatteryLoadCommitment

        self._battery_load_commitments = ()
        self._battery_load_assumptions = ()

        def invalid(reason):
            self._reset_battery_load_forecast()
            return (), (), reason

        age = self.config_entry.data.get("battery_input_max_age_seconds", 300)
        load_entity = self.config_entry.data.get("load_power")
        total = self._battery_read_number(load_entity, max_age=age, power=True)
        if total is None or total < 0:
            return invalid("load_power_missing_or_stale")
        load_state = self.hass.states.get(load_entity)
        reported_at = getattr(load_state, "last_reported", None) or load_state.last_updated
        ev_total = 0.0
        metered_house = 0.0
        ev_runs = []
        house_runs = []
        unbounded_house = []
        limited_runs = []
        signature = []
        sources = []
        assumptions = set()

        # The normal discharge controller clamps its zero request to this native
        # minimum. This is a rate limit, not verified full hold capability.
        cap = None
        cap_maximum = None
        cap_entity = self.config_entry.data.get("battery_max_discharge_entity")
        cap_state = self.hass.states.get(cap_entity) if cap_entity else None
        shared_mapping = self._shared_discharge_mapping()
        cap_step = None
        if isinstance(cap_state, State) and _finite(cap_state.state) is not None:
            unit = str(cap_state.attributes.get("unit_of_measurement", "W")).lower()
            if shared_mapping and unit in ("none", ""):
                unit = "w"
            minimum = _finite(cap_state.attributes.get("min"))
            if minimum is not None and minimum >= 0 and unit in ("w", "kw"):
                multiplier = 1000 if unit == "kw" else 1
                cap = minimum * multiplier
                maximum = _finite(cap_state.attributes.get("max"))
                cap_maximum = maximum * multiplier if maximum is not None else None
                if shared_mapping:
                    fallback = _finite(shared_mapping.enable_disengage_value)
                    step = _finite(cap_state.attributes.get("step", 1 / multiplier))
                    if fallback is None or step is None or step <= 0:
                        return invalid("load_discharge_limit_unavailable")
                    cap_maximum = min(cap_maximum, fallback * multiplier) if cap_maximum is not None else fallback * multiplier
                    cap_step = step * multiplier

        for appliance_id, subentry in getattr(self.config_entry, "subentries", {}).items():
            data = subentry.data
            is_ev = bool(data.get("ev_soc_entity") or data.get("ev_connected_entity"))
            entity_id = data.get("appliance_entity")
            state = self.hass.states.get(entity_id) if entity_id else None
            off = isinstance(state, State) and state.state in ("off", "0", "false", "False")
            watts = self._battery_read_number(data.get("actual_power_entity"), max_age=age, power=True)
            sources.append((appliance_id, entity_id, data.get("actual_power_entity"), is_ev,
                            watts is not None and watts >= 0))
            if watts is None or watts < 0:
                if is_ev and not off:
                    return invalid("active_ev_power_missing_or_stale")
                continue  # Non-EV draw is still captured in aggregate house power.
            if is_ev:
                ev_total += watts
            else:
                metered_house += watts
            if watts <= 0:
                continue
            managed = (self.appliance_enabled.get(appliance_id, True)
                       and not getattr(self, "appliance_paused", {}).get(appliance_id, False))
            override = self.appliance_overrides.get(appliance_id, False)
            managed = managed or override
            if is_ev and (not managed or off):
                return invalid("active_ev_unmanaged")
            policy_allows = managed and self._battery_load_policy_allows_run(appliance_id, data, now)
            if is_ev and (not policy_allows or not isinstance(state, State) or state.state in ("unknown", "unavailable")):
                return invalid("active_ev_policy_uncertain")
            end, funded = self._battery_load_run_end(appliance_id, data, now, tariff) if policy_allows else (None, False)
            if is_ev and end is None:
                return invalid("active_ev_duration_unknown")
            funding_end = self._battery_load_funding(appliance_id, data, now, tariff)[1] if funded else None
            limit = None
            after_limit = None
            after_requested = (_finite(data.get("battery_discharge_override"))
                               if policy_allows and data.get("is_big_consumer") else None)
            requested = 0. if funded else after_requested
            if requested is not None and cap_entity:
                if cap is None:
                    return invalid("load_discharge_limit_unavailable")
                if requested != 0 and unit == "kw" and not shared_mapping:
                    # The existing writer sends watts without converting a
                    # nonzero request to kW. Do not predict an unsupported cap.
                    return invalid("load_discharge_limit_units_unsupported")
                limit = max(requested, cap)
                if cap_maximum is not None:
                    limit = min(limit, cap_maximum)
                if cap_step:
                    limit = cap + math.floor((limit - cap + 1e-9) / cap_step) * cap_step
                if after_requested is not None:
                    if after_requested != 0 and unit == "kw" and not shared_mapping:
                        return invalid("load_discharge_limit_units_unsupported")
                    after_limit = max(after_requested, cap)
                    if cap_maximum is not None:
                        after_limit = min(after_limit, cap_maximum)
                    if cap_step:
                        after_limit = cap + math.floor((after_limit - cap + 1e-9) / cap_step) * cap_step
            # Ignore normal meter jitter, while a real start/stop or material
            # power/policy change immediately invalidates a cached battery plan.
            projected_w = max(100., round(watts / 100) * 100.)
            signature.append((appliance_id, projected_w, managed, override, end, funded, limit, after_limit,
                              tuple((key, repr(value)) for key, value in sorted(data.items()))))
            if is_ev:
                if funding_end is not None and funding_end < end and limit != after_limit:
                    ev_runs.append(BatteryLoadCommitment(now, funding_end, projected_w, limit))
                    ev_runs.append(BatteryLoadCommitment(funding_end, end, projected_w, after_limit))
                else:
                    ev_runs.append(BatteryLoadCommitment(now, end, projected_w, limit))
            elif end is not None:
                house_runs.append((end, projected_w))
            else:
                unbounded_house.append((appliance_id, watts))
            if not is_ev and limit is not None:
                limited_runs.append((appliance_id, end, limit, funding_end))
                if funding_end is not None and end is not None and funding_end < end and after_limit is not None:
                    ev_runs.append(BatteryLoadCommitment(funding_end, end, 0., after_limit))
            if end is not None:
                assumptions.add("active_run_until_policy_or_runtime_bound")

        # Inconsistent simultaneous meters must not invent negative base demand.
        if ev_total > total + 100:
            return invalid("active_ev_power_exceeds_house_load")
        observed_house = max(0., total - ev_total)
        unmetered_base = max(0., observed_house - metered_house)
        source_key = (load_entity, tuple(sorted(sources)))
        if source_key != self._battery_load_sources:
            self._reset_battery_load_forecast()
            self._battery_load_sources = source_key
        try:
            background = self._battery_recent_house_load.observe(
                reported_at, unmetered_base, min(unmetered_base, profile[now.hour]),
                reference_watts=max(0., profile[now.hour] - metered_house),
            )
        except ValueError:
            return invalid("load_power_missing_or_stale")
        observed_floor = max(0., round((metered_house + background) / 100) * 100.)
        raw_floor = max(0., round(observed_house / 100) * 100.)
        reported_at = reported_at.astimezone(timezone.utc)
        # A separately metered run keeps its existing immediate outlook even
        # when policy supplies no exact stop. Unknown background has its own
        # shorter horizon and cannot extend through this measured-load path.
        known_power = max(0., round(sum(watts for _, watts in unbounded_house) / 100) * 100.)
        known_signature = (tuple(app_id for app_id, _ in unbounded_house), known_power) if known_power else ()
        known_until = self._battery_observed_house_until
        if known_signature:
            if (known_until is None or known_until <= now
                    or known_signature != self._battery_observed_house_runs):
                known_until = reported_at + timedelta(minutes=30)
            if known_until > now:
                house_runs.append((known_until, known_power))
                assumptions.add("observed_metered_household_30_minute_persistence")
        else:
            known_until = None
        self._battery_observed_house_until = known_until
        self._battery_observed_house_runs = known_signature
        short_end = reported_at + timedelta(seconds=60)
        # Avoid replan-only boundaries hidden below the household profile, but
        # retain a raw observation that crosses into a lower local-hour profile.
        short_profile = min(profile[now.hour], profile[(short_end - timedelta(microseconds=1)).astimezone(now.tzinfo).hour])
        short_active = raw_floor > max(observed_floor, short_profile) and short_end > now
        # Metered runs get their total floor below, respecting each real end.
        floor_active = not house_runs and observed_floor > profile[now.hour]
        floor_level = observed_floor if floor_active else 0.
        reference = self._battery_recent_house_load.background_reference_watts
        horizon_seconds = self._battery_recent_house_load.persistence_seconds
        background_active = bool(house_runs) and round(background / 100) > round(reference / 100)
        projection_active = floor_active or background_active
        unknown_limits = tuple((app_id, limit, funding_end) for app_id, end, limit, funding_end in limited_runs if end is None)
        until = self._battery_observed_load_until
        previous_report = self._battery_observed_load_reported_at
        fresh_report = previous_report is None or reported_at != previous_report
        if previous_report is not None and reported_at < previous_report:
            until = None
        projection_level = max(floor_level, round(background / 100) * 100. if background_active else 0.)
        if projection_active:
            if fresh_report and (until is None or until <= now
                                 or projection_level > self._battery_observed_load_floor
                                 or horizon_seconds != self._battery_observed_load_horizon):
                until = reported_at + timedelta(seconds=horizon_seconds)
        else:
            until = None
        self._battery_observed_load_until = until
        self._battery_observed_load_floor = projection_level
        self._battery_observed_load_horizon = horizon_seconds
        self._battery_observed_load_reported_at = reported_at
        projection_current = until is not None and until > now
        limit_until = self._battery_observed_limits_until
        if unknown_limits:
            if limit_until is None or limit_until <= now or unknown_limits != self._battery_observed_limits:
                limit_until = now.astimezone(timezone.utc) + timedelta(minutes=30)
        else:
            limit_until = None
        self._battery_observed_limits_until = limit_until
        self._battery_observed_limits = unknown_limits
        commitments = list(ev_runs)
        for _, end, limit, funding_end in limited_runs:
            limit_end = end or limit_until
            if funding_end is not None:
                limit_end = min(limit_end, funding_end)
            commitments.append(BatteryLoadCommitment(now, limit_end, 0., limit))
        if floor_active and projection_current:
            commitments.append(BatteryLoadCommitment(now, until, 0., household_power_floor_w=observed_floor))
        if projection_current:
            assumptions.add("recent_household_mean_adaptive_persistence")
        if short_active:
            commitments.append(BatteryLoadCommitment(now, short_end, 0., household_power_floor_w=raw_floor))
            assumptions.add("observed_household_60_second_persistence")
        if unknown_limits:
            assumptions.add("observed_30_minute_discharge_limit")
        # Combine simultaneous metered household runs into a TOTAL household
        # floor. Taking max(profile, floor) keeps their historical contribution
        # from being charged twice. Unmapped excess is not projected all day.
        boundaries = {now, *(end for end, _ in house_runs)}
        if background_active and projection_current:
            boundaries.add(until)
        hour = now.replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc) + timedelta(hours=1)
        last = max(boundaries)
        if projection_current and until < last:
            boundaries.add(until)
        while hour < last:
            boundaries.add(hour)
            hour += timedelta(hours=1)
        boundaries = sorted(boundaries)
        for left, right in zip(boundaries, boundaries[1:]):
            active_w = sum(watts for end, watts in house_runs if end > left)
            projected_background = (background if projection_current and left < until
                                    else min(reference, profile[left.astimezone(now.tzinfo).hour]))
            floor = active_w + projected_background
            commitments.append(BatteryLoadCommitment(left, right, 0., household_power_floor_w=floor))
        self._battery_load_commitments = tuple(commitments)
        self._battery_load_assumptions = tuple(sorted(assumptions))
        base_key = (round(background / 100) * 100., round(reference / 100) * 100.) if house_runs else None
        short_key = (raw_floor, short_end) if short_active else None
        key = (tuple(signature), observed_floor if projection_current else None, until if projection_current else None,
               cap, base_key, short_key, limit_until, known_until)
        return self._battery_load_commitments, key, None

    def _battery_planning_request(self, tariff_info, power_state):
        """Validate observations before passing immutable inputs to pure planning."""
        from .battery_planner import BatteryPlanningConfig
        d = self.config_entry.data
        factor = d.get(CONF_BATTERY_PV_FORECAST_FACTOR, DEFAULT_BATTERY_PV_FORECAST_FACTOR)
        if (isinstance(factor, bool) or not isinstance(factor, (int, float))
                or not math.isfinite(factor) or not .1 <= factor <= 1.):
            self._battery_grid_plan = None
            return "invalid_pv_forecast_factor", None
        age = d.get("battery_input_max_age_seconds", 300)
        # Use the live State object: REST serialization can cache old report
        # timestamps for unchanged zero readings even while polls keep arriving.
        for key in ("load_power", "pv_power"):
            current_power = self._battery_read_number(d.get(key), max_age=age, power=True)
            if current_power is None or current_power < 0:
                self._battery_grid_plan = None
                if key == "load_power":
                    self._reset_battery_load_forecast()
                return f"{key}_missing_or_stale", None
        if tariff_info is None or _finite(tariff_info.current_price) is None:
            self._battery_grid_plan = None
            return "price_unavailable", None
        raw_price_limit = d.get("battery_charge_price_threshold", tariff_info.battery_charge_price_threshold)
        price_limit = _finite(raw_price_limit)
        if isinstance(raw_price_limit, bool) or price_limit is None:
            self._battery_grid_plan = None
            return "invalid_charge_price_limit", None
        source = self.hass.states.get(d.get("price_sensor"))
        if not isinstance(source, State) or self._battery_read_number(
            d.get("price_sensor"), max_age=max(3900, d.get("battery_forecast_max_age_seconds", 21600)),
        ) is None:
            self._battery_grid_plan = None
            return "price_missing_or_stale", None
        if abs(float(source.state) - tariff_info.current_price) > .00001:
            return "price_snapshot_changed", None
        now = dt_util.now()
        price_now = [w for w in tariff_info.windows if w.start <= now < w.end]
        if (len(price_now) != 1
                or abs(price_now[0].price - tariff_info.current_price) > _FOUR_DECIMAL_PRICE_TOLERANCE):
            self._battery_grid_plan = None
            return "price_window_mismatch", None
        forecast = getattr(self, "_forecast_data", None)
        if forecast is None or not forecast.hourly_breakdown:
            self._battery_grid_plan = None
            return "forecast_unavailable", None
        sources = [*getattr(self, "_forecast_entities", []), *getattr(self, "_forecast_tomorrow_entities", [])]
        if not sources:
            return "forecast_unavailable", None
        for entity_id in sources:
            state = self.hass.states.get(entity_id)
            if not isinstance(state, State) or state.state in ("unknown", "unavailable"):
                return "forecast_unavailable", None
            stamp = getattr(state, "last_reported", None) or state.last_updated
            if not -5 <= (dt_util.utcnow() - stamp).total_seconds() <= d.get("battery_forecast_max_age_seconds", 21600):
                return "forecast_stale", None
        profile = self._battery_load_profile()
        if profile is None:
            self._battery_grid_plan = None
            return "household_profile_incomplete", None
        commitments, load_key, load_error = self._battery_load_context(now, tariff_info, profile)
        if load_error:
            self._battery_grid_plan = None
            return load_error, None
        charge_constraints = self._battery_charge_power_constraints()
        if charge_constraints is None:
            self._battery_grid_plan = None
            return "invalid_charge_power_constraints", None
        soc = self._battery_soc_now()
        if soc is None:
            return "soc_missing_or_stale", None
        hold = bool(self._battery_hold_ctl and self._battery_hold_ctl.confirmation_level == "physical")
        restart_delay = d.get("grid_charge_engage_min_duration_minutes", 5)
        restart_at = self._battery_retry_after
        if restart_at is not None and restart_at <= dt_util.utcnow():
            restart_at = None
        # Keep the chosen slot target stable between planner runs; re-evaluate
        # immediately when any tariff/forecast/configuration input changes.
        key = (tuple((w.start, w.end, w.price) for w in tariff_info.windows),
               tuple((f.start, f.end, f.expected_kwh) for f in forecast.hourly_breakdown),
               hold, load_key, charge_constraints, restart_delay, restart_at,
               tuple((k, repr(v)) for k, v in sorted(d.items()) if k.startswith("battery_") or k == "min_battery_soc"),
               price_limit)
        config = BatteryPlanningConfig(
            capacity_kwh=d.get("battery_capacity", 0), reserve_soc=d.get("min_battery_soc", 10),
            grid_target_soc=d.get("battery_grid_target_soc", 80),
            max_charge_power_w=d.get("battery_grid_charge_power_w", 0),
            max_pv_charge_power_w=d.get("battery_max_charge_power_w"),
            max_discharge_power_w=d.get("battery_max_discharge_default", d.get("battery_grid_charge_power_w", 0)),
            roundtrip_efficiency=d.get("battery_roundtrip_efficiency", .85),
            wear_cost_per_kwh=d.get("battery_wear_cost_per_kwh", 0),
            charge_price_limit=price_limit, hold_supported=hold,
            forced_charge_min_power_w=charge_constraints[0],
            forced_charge_max_power_w=charge_constraints[1],
            forced_charge_power_step_w=charge_constraints[2],
            grid_restart_delay_minutes=restart_delay,
            grid_charge_not_before=restart_at,
            grid_charge_active=bool(self._grid_charge_engaged),
        )
        # Calibration belongs to grid-purchase planning only. The shared
        # provider forecast still drives independent solar/appliance plans.
        grid_forecast = [replace(row, expected_kwh=row.expected_kwh * factor,
                                 expected_watts=row.expected_watts * factor)
                         for row in forecast.hourly_breakdown]
        return None, _BatteryPlanningRequest(
            key, now, soc, tuple(tariff_info.windows), tuple(grid_forecast),
            tuple(profile), config, tuple(commitments),
        )

    def _battery_plan_needs_refresh(self, request):
        stale = (self._battery_plan_created is None
                 or (request.now - self._battery_plan_created).total_seconds() >= 300)
        return request.key != self._battery_plan_key or self._current_battery_slot() is None or stale

    def _commit_battery_grid_plan(self, request, plan):
        self._battery_grid_plan = plan
        self._battery_plan_key = request.key
        self._battery_plan_created = request.now
        return None if plan.valid else plan.reason

    def _interrupt_battery_planning(self):
        """Release the safety lock without waiting for a detached CPU search."""
        if self._battery_planning_abort is not None:
            self._battery_planning_abort.set()

    async def _build_battery_plan_async(self, request):
        """An owned stop interrupts waiting; an abandoned worker has no HA state."""
        aborted = asyncio.Event()
        self._battery_planning_abort = aborted
        pending = []
        try:
            worker = asyncio.ensure_future(self.hass.async_add_executor_job(request.build))
            stop = asyncio.create_task(aborted.wait())
            pending = [worker, stop]
            await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            if aborted.is_set():
                return None
            return await worker
        finally:
            if self._battery_planning_abort is aborted:
                self._battery_planning_abort = None
            for task in pending:
                if not task.done():
                    task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    def _refresh_battery_grid_plan(self, tariff_info, power_state):
        """Synchronous helper for offline analysis; runtime uses the executor."""
        reason, request = self._battery_planning_request(tariff_info, power_state)
        if reason:
            return reason
        if self._battery_plan_needs_refresh(request):
            return self._commit_battery_grid_plan(request, request.build())
        return None if self._battery_grid_plan.valid else self._battery_grid_plan.reason

    def _automatic_battery_planning_requested(self):
        return (self.enabled and not self._battery_unloading and not self.force_charge
                and self._battery_storage_ready and not self._battery_recovery
                and self.config_entry.data.get("auto_battery_grid_charge")
                and self.config_entry.data.get("allow_grid_charging"))

    async def _async_refresh_battery_grid_plan(self, tariff_info, power_state):
        """Yield expensive searches, committing only still-current input data."""
        def reject(reason):
            self._battery_grid_plan = None
            self._battery_plan_key = None
            return reason

        try:
            for _ in range(2):
                if not self._automatic_battery_planning_requested():
                    return reject("request_changed")
                # The coordinator may publish a newer provider snapshot while
                # this request waits. Follow-up validation must not revert it.
                if self._latest_tariff is not None:
                    tariff_info = self._latest_tariff
                reason, request = self._battery_planning_request(tariff_info, power_state)
                if reason:
                    return reject(reason)
                if not self._battery_plan_needs_refresh(request):
                    return None if self._battery_grid_plan.valid else self._battery_grid_plan.reason
                latest_tariff = self._latest_tariff
                plan = await self._build_battery_plan_async(request)
                if plan is None:
                    return reject("planning_interrupted")
                if not self._automatic_battery_planning_requested():
                    return reject("request_changed")
                if self._latest_tariff is not latest_tariff and self._latest_tariff is not None:
                    tariff_info = self._latest_tariff
                reason, current = self._battery_planning_request(tariff_info, power_state)
                if reason:
                    return reject(reason)
                covers_now = (not plan.valid or (plan.slots and
                              plan.slots[0].start <= dt_util.utcnow() < plan.slots[0].end))
                if current.inputs == request.inputs and covers_now:
                    return self._commit_battery_grid_plan(request, plan)
                reject("planning_input_changed")
            return reject("planning_input_changed")
        except asyncio.CancelledError:
            reject("planning_cancelled")
            if (self._grid_charge_cleanup_pending or self._battery_hold_cleanup_pending
                    or self._battery_discharge_cleanup_pending):
                self._battery_recovery = True
                self._arm_battery_watchdog(dt_util.utcnow())
            raise
        except Exception:
            _LOGGER.exception("Battery planning failed; owned controls must release")
            return reject("planning_failed")

    def _observe_battery_load(self):
        """Learn non-EV household watts by local hour; never learn missing as zero."""
        d = self.config_entry.data
        now = dt_util.now()
        watts = self._battery_read_number(d.get("load_power"), max_age=d.get("battery_input_max_age_seconds", 300), power=True)
        for subentry in getattr(self.config_entry, "subentries", {}).values():
            if subentry.data.get("ev_soc_entity") or subentry.data.get("ev_connected_entity"):
                state = self.hass.states.get(subentry.data.get("appliance_entity"))
                if isinstance(state, State) and state.state in ("off", "0"):
                    continue
                ev = self._battery_read_number(subentry.data.get("actual_power_entity"), max_age=300, power=True)
                if ev is None:
                    watts = None
                    break
                if watts is not None:
                    watts -= ev
        if watts is not None and watts < 0:
            watts = None
        previous = self._battery_load_last
        self._battery_load_last = (now, watts)
        if watts is None or previous is None or previous[1] is None or previous[1] < 0:
            return
        duration = (now.astimezone(timezone.utc) - previous[0].astimezone(timezone.utc)).total_seconds()
        if not 0 < duration <= 120 or now.hour != previous[0].hour:
            return
        row = self._battery_load_hours[now.hour]
        # A bounded moving estimate retains recent seasonal household demand.
        weight = min(row[1], 7 * 3600.)
        row[0] = (row[0] * weight + (watts + previous[1]) / 2 * duration) / (weight + duration)
        row[1] = min(weight + duration, 7 * 3600.)
        row[2] = now.isoformat()
        if self._battery_load_store is not None:
            self._battery_load_store.async_delay_save(lambda: {"hours": self._battery_load_hours}, 300)

    def _battery_load_profile(self):
        explicit = self.config_entry.data.get("battery_load_profile_w")
        if explicit is not None:
            if not isinstance(explicit, list) or len(explicit) != 24:
                return None
            if any(_finite(v) is None or float(v) < 0 for v in explicit):
                return None
            return [float(v) for v in explicit]
        now = dt_util.utcnow()
        for watts, seconds, stamp in self._battery_load_hours:
            if seconds < 1800 or stamp is None:
                return None
            try:
                age = (now - datetime.fromisoformat(stamp)).total_seconds()
            except (ValueError, TypeError):
                return None
            if not 0 <= age <= 7 * 86400:
                return None
        return [row[0] for row in self._battery_load_hours]

    async def async_restore_battery_load(self):
        self._init_battery_control()
        await self._restore_battery_ownership()
        if self._battery_recovery:
            await self.async_stop_battery_controls("startup_recovery")
        self._battery_load_store = Store(self.hass, 1, f"pv_excess_control.{self.config_entry.entry_id}.battery_load")
        try:
            saved = await self._battery_load_store.async_load()
            rows = saved.get("hours") if isinstance(saved, dict) else None
            if isinstance(rows, list) and len(rows) == 24 and all(
                isinstance(r, list) and len(r) == 3 and _finite(r[0]) is not None and r[0] >= 0
                and _finite(r[1]) is not None and 0 <= r[1] <= 7 * 3600
                and (r[2] is None or isinstance(r[2], str)) for r in rows
            ):
                self._battery_load_hours = rows
        except (HomeAssistantError, OSError, ValueError, TypeError):
            self._battery_load_store = None
            _LOGGER.exception("Could not restore household load profile")

    async def async_save_battery_load(self):
        self._init_battery_control()
        if self._battery_load_store is not None:
            try:
                await self._battery_load_store.async_save({"hours": self._battery_load_hours})
            except (HomeAssistantError, OSError, ValueError, TypeError):
                _LOGGER.exception("Could not save household load profile")

    def battery_control_diagnostics(self):
        self._init_battery_control()
        plan = self._battery_grid_plan
        measured_flow = self._measured_battery_grid_flow()
        return {
            "battery_control_state": self._battery_control_state,
            "battery_control_reason": self._battery_control_reason,
            "battery_restart_available_at": (self._battery_retry_after.isoformat()
                                             if self._battery_retry_after is not None
                                             and self._battery_retry_after > dt_util.utcnow() else None),
            "battery_grid_charge_observed_power_w": round(measured_flow[0], 1) if measured_flow else None,
            "battery_charge_cleanup_pending": self._grid_charge_cleanup_pending,
            "battery_hold_cleanup_pending": self._battery_hold_cleanup_pending,
            "battery_discharge_cleanup_pending": self._battery_discharge_cleanup_pending,
            "battery_hold_supported": bool(self._battery_hold_ctl and self._battery_hold_ctl.confirmation_level == "physical"),
            "battery_confirmation": self._inverter_ctl.confirmation_level if self._inverter_ctl else "unconfigured",
            "battery_grid_energy_kwh": round(plan.grid_energy_kwh, 3) if plan and plan.valid else None,
            "battery_grid_target_soc": round(plan.grid_target_soc, 1) if plan and plan.valid else None,
            "battery_estimated_savings": round(plan.estimated_savings, 3) if plan and plan.valid else None,
            "battery_load_profile_ready": self._battery_load_profile() is not None,
            "battery_load_assumptions": list(self._battery_load_assumptions),
            "battery_load_commitments": [
                {"start": row.start.isoformat(), "end": row.end.isoformat(),
                 "external_power_w": row.power_w, "household_power_floor_w": row.household_power_floor_w,
                 "max_discharge_power_w": row.max_discharge_power_w}
                for row in self._battery_load_commitments
            ],
            "battery_plan": [{"start": s.start.isoformat(), "end": s.end.isoformat(), "action": s.action,
                              "grid_charge_kwh": round(s.grid_charge_kwh, 3), "soc_end": round(s.soc_end, 1)}
                             for s in plan.slots] if plan and plan.valid else [],
        }
