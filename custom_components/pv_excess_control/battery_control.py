"""Home Assistant adapter for safe, forecast-based battery grid charging.

This coordinator mixin owns measurements, persisted cleanup and service dispatch.
Economic planning remains in the HA-independent battery_planner module.
"""
from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import CONF_BATTERY_PV_FORECAST_FACTOR, DEFAULT_BATTERY_PV_FORECAST_FACTOR
from .inverter_control import InverterGridChargeController
from .models import InverterGridChargeConfig

_LOGGER = logging.getLogger(__name__)

# Some tariff providers round future-window EUR/kWh prices to four decimals
# while retaining full precision in the live sensor. Permit half that step.
_FOUR_DECIMAL_PRICE_TOLERANCE = .00005 + 1e-12


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
        self._battery_recovery = self._grid_charge_cleanup_pending or self._battery_hold_cleanup_pending
        self._battery_hold_engaged = False
        self._battery_hold_ctl = None
        self._battery_watchdog = None
        self._battery_retry_after = None
        self._battery_hold_retry_after = None
        self._battery_target_latched = None
        self._battery_target_slot = None
        self._battery_grid_plan = None
        self._battery_plan_key = None
        self._battery_plan_created = None
        self._battery_charge_power = None
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
        if data.get("battery_hold_verified") and data.get("battery_hold_entity"):
            try:
                self._battery_hold_ctl = InverterGridChargeController(self.hass, InverterGridChargeConfig(
                    enable_entity_id=data["battery_hold_entity"],
                    enable_engage_value=data.get("battery_hold_engage_value", ""),
                    enable_disengage_value=data.get("battery_hold_release_value", ""),
                    enable_feedback_entity_id=data.get("battery_hold_feedback_entity"),
                    enable_feedback_engage_value=data.get("battery_hold_feedback_engage_value"),
                    enable_feedback_disengage_value=data.get("battery_hold_feedback_release_value"),
                ))
            except ValueError:
                _LOGGER.exception("Invalid battery hold mapping")
        if self._battery_hold_cleanup_pending:
            self._battery_hold_owner = self._battery_hold_ctl

    async def _restore_battery_ownership(self):
        try:
            charge, hold = await self._battery_journal.async_load()
            if self._battery_journal.loaded_record_exists is True:
                # The awaited journal outranks delayed configuration flags.
                self._grid_charge_engaged = False
                self._grid_charge_cleanup_pending = charge is not None
                self._battery_hold_cleanup_pending = hold is not None
                self._battery_charge_owner = None
                self._battery_hold_owner = None
            if charge is not None:
                self._battery_charge_owner = InverterGridChargeController(self.hass, charge)
                self._grid_charge_cleanup_pending = True
            if hold is not None:
                self._battery_hold_owner = InverterGridChargeController(self.hass, hold)
                self._battery_hold_cleanup_pending = True
            self._battery_recovery = self._grid_charge_cleanup_pending or self._battery_hold_cleanup_pending
            self._battery_storage_ready = True
        except (HomeAssistantError, OSError, ValueError, TypeError):
            self._battery_storage_ready = False
            self._battery_control_state = "fault"
            self._battery_control_reason = "ownership_storage_unavailable"
            _LOGGER.exception("Could not restore battery ownership; starts blocked")

    async def _save_battery_ownership(self):
        if not self._battery_storage_ready:
            raise RuntimeError("Battery ownership storage unavailable")
        await self._battery_journal.async_save(
            self._battery_charge_owner.config if self._grid_charge_cleanup_pending and self._battery_charge_owner else None,
            self._battery_hold_owner.config if self._battery_hold_cleanup_pending and self._battery_hold_owner else None,
        )

    def _persist_battery_controls(self):
        data = dict(self.config_entry.data)
        data.update({
            "_grid_charge_engaged": self._grid_charge_engaged,
            "_grid_charge_cleanup_pending": self._grid_charge_cleanup_pending,
            "_battery_hold_cleanup_pending": self._battery_hold_cleanup_pending,
        })
        keys = ("_grid_charge_engaged", "_grid_charge_cleanup_pending", "_battery_hold_cleanup_pending")
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
        if self._battery_watchdog is not None:
            self._battery_watchdog.cancel()
            self._battery_watchdog = None

    def _arm_battery_watchdog(self, end=None):
        """A stalled update loop must not leave an owned charge/hold running."""
        self._cancel_battery_watchdog()
        seconds = float(self.config_entry.data.get("battery_input_max_age_seconds", 300))
        soc_state = self.hass.states.get(self.config_entry.data.get("battery_soc"))
        if isinstance(soc_state, State):
            stamp = getattr(soc_state, "last_reported", None) or soc_state.last_updated
            seconds -= max(0., (dt_util.utcnow() - stamp).total_seconds())
        if end is not None:
            seconds = min(seconds, (end - dt_util.utcnow()).total_seconds())
        def expired():
            self._battery_watchdog = None
            self.hass.async_create_task(self.async_stop_battery_controls("watchdog_expired"))
        self._battery_watchdog = self.hass.loop.call_later(max(.1, seconds), expired)

    async def _release_battery_locked(self, reason):
        """Cancellation also leaves an independently retryable cleanup."""
        try:
            return await self._release_battery_locked_impl(reason)
        except asyncio.CancelledError:
            self._battery_recovery = True
            self._arm_battery_watchdog(dt_util.utcnow() + timedelta(seconds=30))
            raise

    async def _release_battery_locked_impl(self, reason):
        """Try every owned release; retain ownership until confirmed."""
        self._cancel_battery_watchdog()
        ok = True
        was_engaged = self._grid_charge_engaged
        had_charge = self._grid_charge_cleanup_pending or self._grid_charge_engaged
        had_hold = self._battery_hold_cleanup_pending
        if had_charge:
            self._grid_charge_cleanup_pending = True
            try:
                controller = self._battery_charge_owner or self._inverter_ctl
                if controller is None:
                    raise ValueError("Missing controller for pending battery release")
                await controller.disengage()
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
        if (had_charge or had_hold) and self._battery_storage_ready:
            try:
                await self._save_battery_ownership()
            except asyncio.CancelledError:
                self._grid_charge_cleanup_pending |= had_charge
                self._battery_hold_cleanup_pending |= had_hold
                raise
            except Exception as err:
                self._grid_charge_cleanup_pending |= had_charge
                self._battery_hold_cleanup_pending |= had_hold
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
            self._battery_watchdog = self.hass.loop.call_later(
                30, lambda: self.hass.async_create_task(self.async_stop_battery_controls(reason)),
            )
        if ok and was_engaged:
            await self._notify_grid_charge("disengaged", reason)
        return ok

    async def _notify_grid_charge(self, event, value):
        method = getattr(getattr(self, "notifications", None), f"notify_battery_grid_charge_{event}", None)
        if method is not None:
            try:
                await method(value)
            except Exception:
                _LOGGER.exception("Battery charging notification failed")

    async def async_stop_battery_controls(self, reason="disabled"):
        self._init_battery_control()
        async with self._battery_lock:
            return await self._release_battery_locked(reason)

    async def async_prepare_battery_unload(self):
        self._init_battery_control()
        self._battery_unloading = True
        result = await self.async_stop_battery_controls("unload")
        await self.async_save_battery_load()
        return result

    async def _battery_preflight(self, power_state=None):
        self._init_battery_control()
        self._observe_battery_load()
        if self._battery_recovery or not self.enabled or self._battery_unloading:
            await self.async_stop_battery_controls("startup_recovery" if self._battery_recovery else "disabled")
        elif (self._grid_charge_engaged or self._battery_hold_engaged) and self._battery_soc_now() is None:
            await self.async_stop_battery_controls("soc_missing_or_stale")

    def auto_should_engage_now(self):
        self._init_battery_control()
        if (not self.enabled or self._battery_unloading or not self._battery_storage_ready
                or not self.config_entry.data.get("auto_battery_grid_charge")
                or not self.config_entry.data.get("allow_grid_charging")):
            return False
        slot = self._current_battery_slot()
        return bool(slot and slot.action == "charge" and self._battery_soc_now() is not None)

    def _current_battery_slot(self):
        plan = self._battery_grid_plan
        if plan is None or not plan.valid:
            return None
        now = dt_util.utcnow()
        return next((slot for slot in plan.slots if slot.start <= now < slot.end), None)

    async def _run_grid_charge_state_machine(self, tariff_info, power_state):
        self._init_battery_control()
        async with self._battery_lock:
            if self._battery_recovery:
                await self._release_battery_locked("startup_recovery")
                return
            if not self.enabled or self._battery_unloading:
                await self._release_battery_locked("disabled")
                return
            if not self._battery_storage_ready:
                await self._release_battery_locked("ownership_storage_unavailable")
                return
            soc = self._battery_soc_now()
            if soc is None:
                await self._release_battery_locked("soc_missing_or_stale")
                return
            d = self.config_entry.data
            requested_power = _finite(d.get("battery_grid_charge_power_w"))
            if self._inverter_ctl is None or requested_power is None or requested_power <= 0:
                await self._release_battery_locked("charge_control_unavailable")
                return
            if self._inverter_ctl.confirmation_level != "physical":
                await self._release_battery_locked("physical_feedback_required")
                return
            manual = self.force_charge
            slot = None
            target = 100.
            if manual:
                action = "charge" if soc < target else "self_consumption"
            elif d.get("auto_battery_grid_charge") and d.get("allow_grid_charging"):
                if not d.get("inverter_force_charge_power_entity"):
                    await self._release_battery_locked("adjustable_power_control_required")
                    return
                native_charge_limit = _finite(d.get("battery_max_charge_power_w"))
                if native_charge_limit is None or native_charge_limit <= 0:
                    await self._release_battery_locked("native_charge_limit_required")
                    return
                try:
                    reason = self._refresh_battery_grid_plan(tariff_info, power_state)
                except (ValueError, TypeError, AttributeError, ArithmeticError) as err:
                    _LOGGER.warning("Battery planning input rejected: %s", err)
                    reason = "invalid_planning_input"
                if reason:
                    self._battery_grid_plan = None
                    self._battery_plan_key = None
                    await self._release_battery_locked(reason)
                    return
                slot = self._current_battery_slot()
                action = slot.action if slot else "self_consumption"
                if slot is not None:
                    current_pv = self._battery_read_number(d.get("pv_power"), power=True)
                    current_load = self._battery_read_number(d.get("load_power"), power=True)
                    current_surplus = max(0., current_pv - current_load)
                    requested_power = min(native_charge_limit, requested_power + current_surplus, slot.battery_charge_power_w)
                    target = min(float(d.get("battery_grid_target_soc", 80)), slot.soc_end)
                    if action == "charge" and tariff_info.current_price > tariff_info.battery_charge_price_threshold:
                        action = "self_consumption"
            else:
                action = "self_consumption"
            now = dt_util.utcnow()
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
                if self._battery_retry_after is not None and now < self._battery_retry_after:
                    action = "self_consumption"
            if action != "charge" and self._grid_charge_engaged:
                self._battery_retry_after = now + timedelta(minutes=d.get("grid_charge_engage_min_duration_minutes", 5))
            if action == "charge":
                requested_power = self._feasible_battery_power(requested_power)
                if requested_power is None or requested_power <= 0:
                    await self._release_battery_locked("invalid_planned_power")
                    return
                if self._battery_hold_cleanup_pending and not await self._release_battery_locked("leave_hold"):
                    return
                try:
                    was_engaged = self._grid_charge_engaged
                    if self._grid_charge_engaged and self._battery_charge_power == requested_power:
                        await self._inverter_ctl.verify_engaged(requested_power)
                    else:
                        self._grid_charge_cleanup_pending = True
                        self._battery_charge_owner = self._inverter_ctl
                        await self._save_battery_ownership()
                        self._persist_battery_controls()
                        if (not self.enabled or self._battery_unloading or self.force_charge != manual
                                or self._battery_soc_now() is None):
                            await self._release_battery_locked("request_changed")
                            return
                        await self._release_solar_charge_cap()
                        if not manual:
                            refreshed = self._battery_action_after_wait("charge", tariff_info, power_state)
                            if refreshed is None:
                                await self._release_battery_locked("planning_input_changed")
                                return
                            slot = refreshed
                            target = min(float(d.get("battery_grid_target_soc", 80)), slot.soc_end)
                            pv = self._battery_read_number(d.get("pv_power"), power=True)
                            load = self._battery_read_number(d.get("load_power"), power=True)
                            requested_power = self._feasible_battery_power(min(
                                requested_power, slot.battery_charge_power_w,
                                float(d["battery_grid_charge_power_w"]) + max(0., pv - load),
                            ))
                            if requested_power is None or requested_power <= 0:
                                await self._release_battery_locked("invalid_planned_power")
                                return
                        if (not self.enabled or self._battery_unloading or self.force_charge != manual
                                or self._battery_soc_now() is None or self._battery_soc_now() >= target
                                or (slot is not None and dt_util.utcnow() >= slot.end)):
                            await self._release_battery_locked("request_changed")
                            return
                        await self._inverter_ctl.engage(requested_power)
                        self._grid_charge_engaged = True
                        self._battery_charge_power = requested_power
                        self._persist_battery_controls()
                    if (not self.enabled or self._battery_unloading or self.force_charge != manual
                            or self._battery_soc_now() is None
                            or (slot is not None and dt_util.utcnow() >= slot.end)):
                        await self._release_battery_locked("request_changed")
                        return
                    self._battery_control_state = "charging"
                    self._battery_control_reason = "manual" if manual else "economic_plan"
                    self._arm_battery_watchdog(slot.end if slot else None)
                    if not was_engaged:
                        await self._notify_grid_charge("engaged", requested_power)
                except asyncio.CancelledError:
                    self._battery_recovery = True
                    self._arm_battery_watchdog(dt_util.utcnow())
                    raise
                except Exception as err:
                    _LOGGER.warning("Battery start/confirmation failed: %s", err)
                    self._battery_retry_after = now + timedelta(minutes=5)
                    await self._release_battery_locked("charge_unconfirmed")
                return
            if action == "hold" and self._battery_hold_ctl is not None:
                if self._battery_hold_retry_after is not None and now < self._battery_hold_retry_after:
                    await self._release_battery_locked("retry_backoff")
                    return
                if self._grid_charge_cleanup_pending and not await self._release_battery_locked("enter_hold"):
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
                                or self._battery_soc_now() is None or dt_util.utcnow() >= slot.end):
                            await self._release_battery_locked("request_changed")
                            return
                        await self._release_solar_charge_cap()
                        refreshed = self._battery_action_after_wait("hold", tariff_info, power_state)
                        if refreshed is None:
                            await self._release_battery_locked("planning_input_changed")
                            return
                        slot = refreshed
                        if (not self.enabled or self._battery_unloading or self.force_charge
                                or self._battery_soc_now() is None or dt_util.utcnow() >= slot.end):
                            await self._release_battery_locked("request_changed")
                            return
                        await self._battery_hold_ctl.engage(0)
                        self._battery_hold_engaged = True
                    if (not self.enabled or self._battery_unloading or self.force_charge
                            or self._battery_soc_now() is None or dt_util.utcnow() >= slot.end):
                        await self._release_battery_locked("request_changed")
                        return
                    self._battery_control_state = "holding"
                    self._battery_control_reason = "later_expensive_window"
                    self._arm_battery_watchdog(slot.end)
                except asyncio.CancelledError:
                    self._battery_recovery = True
                    self._arm_battery_watchdog(dt_util.utcnow())
                    raise
                except Exception as err:
                    _LOGGER.warning("Battery hold unconfirmed: %s", err)
                    self._battery_plan_key = None
                    self._battery_hold_retry_after = now + timedelta(minutes=5)
                    await self._release_battery_locked("hold_unconfirmed")
                return
            released = await self._release_battery_locked("target_reached" if soc >= target else "self_consumption")
            if released and d.get("auto_battery_grid_charge"):
                self._battery_control_state = "self_consumption"

    def _battery_action_after_wait(self, action, tariff_info, power_state):
        """Revalidate inputs after asynchronous preparation before a new write."""
        try:
            reason = self._refresh_battery_grid_plan(tariff_info, power_state)
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

    def _feasible_battery_power(self, watts):
        """Round down to actuator steps so a plan cannot request extra energy."""
        entity_id = self.config_entry.data.get("inverter_force_charge_power_entity")
        if not entity_id:
            return watts
        state = self.hass.states.get(entity_id)
        if not isinstance(state, State) or state.state in ("unknown", "unavailable"):
            return None
        multiplier = 1000 if str(state.attributes.get("unit_of_measurement", "W")).lower() == "kw" else 1
        minimum = _finite(state.attributes.get("min", 0))
        maximum = _finite(state.attributes.get("max", watts / multiplier))
        step = _finite(state.attributes.get("step", 1 / multiplier))
        if minimum is None or maximum is None or step is None or step <= 0:
            return None
        value = min(watts / multiplier, maximum)
        value = minimum + math.floor((value - minimum + 1e-9) / step) * step
        return value * multiplier if value >= minimum else None

    def _refresh_battery_grid_plan(self, tariff_info, power_state):
        """Validate observations before passing immutable inputs to pure planning."""
        from .battery_planner import BatteryPlanningConfig, build_battery_grid_plan
        d = self.config_entry.data
        factor = d.get(CONF_BATTERY_PV_FORECAST_FACTOR, DEFAULT_BATTERY_PV_FORECAST_FACTOR)
        if (isinstance(factor, bool) or not isinstance(factor, (int, float))
                or not math.isfinite(factor) or not .1 <= factor <= 1.):
            self._battery_grid_plan = None
            return "invalid_pv_forecast_factor"
        age = d.get("battery_input_max_age_seconds", 300)
        # Use the live State object: REST serialization can cache old report
        # timestamps for unchanged zero readings even while polls keep arriving.
        for key in ("load_power", "pv_power"):
            current_power = self._battery_read_number(d.get(key), max_age=age, power=True)
            if current_power is None or current_power < 0:
                self._battery_grid_plan = None
                return f"{key}_missing_or_stale"
        if tariff_info is None or _finite(tariff_info.current_price) is None:
            self._battery_grid_plan = None
            return "price_unavailable"
        source = self.hass.states.get(d.get("price_sensor"))
        if not isinstance(source, State) or self._battery_read_number(
            d.get("price_sensor"), max_age=max(3900, d.get("battery_forecast_max_age_seconds", 21600)),
        ) is None:
            self._battery_grid_plan = None
            return "price_missing_or_stale"
        if abs(float(source.state) - tariff_info.current_price) > .00001:
            return "price_snapshot_changed"
        now = dt_util.now()
        price_now = [w for w in tariff_info.windows if w.start <= now < w.end]
        if (len(price_now) != 1
                or abs(price_now[0].price - tariff_info.current_price) > _FOUR_DECIMAL_PRICE_TOLERANCE):
            self._battery_grid_plan = None
            return "price_window_mismatch"
        forecast = getattr(self, "_forecast_data", None)
        if forecast is None or not forecast.hourly_breakdown:
            self._battery_grid_plan = None
            return "forecast_unavailable"
        sources = [*getattr(self, "_forecast_entities", []), *getattr(self, "_forecast_tomorrow_entities", [])]
        if not sources:
            return "forecast_unavailable"
        for entity_id in sources:
            state = self.hass.states.get(entity_id)
            if not isinstance(state, State) or state.state in ("unknown", "unavailable"):
                return "forecast_unavailable"
            stamp = getattr(state, "last_reported", None) or state.last_updated
            if not -5 <= (dt_util.utcnow() - stamp).total_seconds() <= d.get("battery_forecast_max_age_seconds", 21600):
                return "forecast_stale"
        profile = self._battery_load_profile()
        if profile is None:
            self._battery_grid_plan = None
            return "household_profile_incomplete"
        soc = self._battery_soc_now()
        hold = bool(self._battery_hold_ctl and self._battery_hold_ctl.confirmation_level == "physical")
        # Keep the chosen slot target stable between planner runs; re-evaluate
        # immediately when any tariff/forecast/configuration input changes.
        key = (tuple((w.start, w.end, w.price) for w in tariff_info.windows),
               tuple((f.start, f.end, f.expected_kwh) for f in forecast.hourly_breakdown),
               hold,
               tuple((k, repr(v)) for k, v in sorted(d.items()) if k.startswith("battery_") or k == "min_battery_soc"),
               tariff_info.battery_charge_price_threshold)
        slot = self._current_battery_slot()
        stale = self._battery_plan_created is None or (now - self._battery_plan_created).total_seconds() >= 300
        if key != self._battery_plan_key or slot is None or stale:
            config = BatteryPlanningConfig(
                capacity_kwh=d.get("battery_capacity", 0), reserve_soc=d.get("min_battery_soc", 10),
                grid_target_soc=d.get("battery_grid_target_soc", 80),
                max_charge_power_w=d.get("battery_grid_charge_power_w", 0),
                max_pv_charge_power_w=d.get("battery_max_charge_power_w"),
                max_discharge_power_w=d.get("battery_max_discharge_default", d.get("battery_grid_charge_power_w", 0)),
                roundtrip_efficiency=d.get("battery_roundtrip_efficiency", .85),
                wear_cost_per_kwh=d.get("battery_wear_cost_per_kwh", 0),
                charge_price_limit=tariff_info.battery_charge_price_threshold, hold_supported=hold,
            )
            # Calibration belongs to grid-purchase planning only. The shared
            # provider forecast still drives independent solar/appliance plans.
            grid_forecast = [replace(row, expected_kwh=row.expected_kwh * factor,
                                     expected_watts=row.expected_watts * factor)
                             for row in forecast.hourly_breakdown]
            self._battery_grid_plan = build_battery_grid_plan(now, soc, tariff_info.windows, grid_forecast, profile, config)
            self._battery_plan_key = key
            self._battery_plan_created = now
        return None if self._battery_grid_plan.valid else self._battery_grid_plan.reason

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
        return {
            "battery_control_state": self._battery_control_state,
            "battery_control_reason": self._battery_control_reason,
            "battery_charge_cleanup_pending": self._grid_charge_cleanup_pending,
            "battery_hold_cleanup_pending": self._battery_hold_cleanup_pending,
            "battery_hold_supported": bool(self._battery_hold_ctl and self._battery_hold_ctl.confirmation_level == "physical"),
            "battery_confirmation": self._inverter_ctl.confirmation_level if self._inverter_ctl else "unconfigured",
            "battery_grid_energy_kwh": round(plan.grid_energy_kwh, 3) if plan and plan.valid else None,
            "battery_grid_target_soc": round(plan.grid_target_soc, 1) if plan and plan.valid else None,
            "battery_estimated_savings": round(plan.estimated_savings, 3) if plan and plan.valid else None,
            "battery_load_profile_ready": self._battery_load_profile() is not None,
            "battery_plan": [{"start": s.start.isoformat(), "end": s.end.isoformat(), "action": s.action,
                              "grid_charge_kwh": round(s.grid_charge_kwh, 3), "soc_end": round(s.soc_end, 1)}
                             for s in plan.slots] if plan and plan.valid else [],
        }
