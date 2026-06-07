"""Sensor entities for PV Excess Control.

Exposes coordinator data as Home Assistant sensor entities:
- System-level sensors (excess power, plan confidence, etc.)
- Per-appliance sensors (power, runtime, energy, status)
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_APPLIANCE_NAME, DOMAIN, MANUFACTURER
from .coordinator import PvExcessCoordinator
from .models import BatteryDischargeAction
from .status_formatter import FormattedStatus, format_status

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PV Excess Control sensor entities from a config entry."""
    coordinator: PvExcessCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    # System-level sensors (no subentry association)
    async_add_entities([
        PvExcessPowerSensor(coordinator),
        PvPlanConfidenceSensor(coordinator),
    ])

    # Per-appliance sensors from subentries
    subentries = getattr(config_entry, "subentries", {})
    for subentry_id, subentry in subentries.items():
        appliance_name = subentry.data.get(CONF_APPLIANCE_NAME, f"Appliance {subentry_id}")
        async_add_entities([
            PvAppliancePowerSensor(coordinator, subentry_id, appliance_name),
            PvApplianceRuntimeSensor(coordinator, subentry_id, appliance_name),
            PvApplianceEnergySensor(coordinator, subentry_id, appliance_name),
            PvApplianceActivationsSensor(coordinator, subentry_id, appliance_name),
            PvApplianceStatusSensor(coordinator, subentry_id, appliance_name),
        ], config_subentry_id=subentry_id)


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class PvExcessBaseSensor(CoordinatorEntity[PvExcessCoordinator], SensorEntity):
    """Base class for PV Excess Control sensors."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PvExcessCoordinator,
        unique_id_suffix: str,
        name: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{unique_id_suffix}"
        self._attr_name = name

    @property
    def device_info(self) -> DeviceInfo:
        """Associate all sensors with the PV Excess Control device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.config_entry.entry_id)},
            name="PV Excess Control",
            manufacturer=MANUFACTURER,
        )

    @property
    def _data(self) -> dict[str, Any] | None:
        """Return coordinator data, or None if not yet available."""
        return self.coordinator.data


# ---------------------------------------------------------------------------
# System sensors
# ---------------------------------------------------------------------------


class PvExcessPowerSensor(PvExcessBaseSensor):
    """Sensor reporting current PV excess power in Watts."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, coordinator: PvExcessCoordinator) -> None:
        super().__init__(coordinator, "excess_power", "Excess Power")

    @property
    def native_value(self) -> float | None:
        """Return the excess power in Watts."""
        data = self._data
        if data is None:
            return None
        power_state = data.get("power_state")
        if power_state is None:
            return None
        return power_state.excess_power


class PvPlanConfidenceSensor(PvExcessBaseSensor):
    """Sensor reporting planner confidence as a percentage."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator: PvExcessCoordinator) -> None:
        super().__init__(coordinator, "plan_confidence", "Plan Confidence")

    @property
    def native_value(self) -> float | None:
        """Return plan confidence (0–100)."""
        data = self._data
        if data is None:
            return None
        plan = data.get("current_plan")
        if plan is None:
            return None
        # confidence is 0.0–1.0; convert to percentage
        return round(plan.confidence * 100, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose plan entries so the frontend can display them."""
        data = self.coordinator.data
        if data is None:
            return None
        plan = data.get("current_plan")
        if plan is None or not plan.entries:
            return None
        configs = data.get("appliance_configs") or {}
        entries = []
        for entry in plan.entries:
            e: dict[str, Any] = {
                "appliance_id": entry.appliance_id,
                "appliance_name": configs[entry.appliance_id].name if entry.appliance_id in configs else entry.appliance_id,
                "action": entry.action.value if hasattr(entry.action, 'value') else str(entry.action),
                "reason": entry.reason,
            }
            if entry.window:
                e["window_start"] = entry.window.start.isoformat() if entry.window.start else None
                e["window_end"] = entry.window.end.isoformat() if entry.window.end else None
            entries.append(e)
        return {"plan_entries": entries, "plan_entry_count": len(plan.entries)}


# ---------------------------------------------------------------------------
# Per-appliance sensors
# ---------------------------------------------------------------------------


class PvApplianceBaseSensor(PvExcessBaseSensor):
    """Base class for per-appliance sensors."""

    def __init__(
        self,
        coordinator: PvExcessCoordinator,
        appliance_id: str,
        appliance_name: str,
        suffix: str,
        sensor_label: str,
    ) -> None:
        unique_id_suffix = f"appliance_{appliance_id}_{suffix}"
        name = f"{appliance_name} {sensor_label}"
        super().__init__(coordinator, unique_id_suffix, name)
        self._appliance_id = appliance_id
        self._attr_config_subentry_id = appliance_id

    def _appliance_state(self):
        """Return the ApplianceState for this appliance, or None."""
        data = self._data
        if data is None:
            return None
        appliance_states = data.get("appliance_states", {})
        return appliance_states.get(self._appliance_id)


class PvAppliancePowerSensor(PvApplianceBaseSensor):
    """Sensor reporting current power draw of an appliance."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(
        self,
        coordinator: PvExcessCoordinator,
        appliance_id: str,
        appliance_name: str,
    ) -> None:
        super().__init__(coordinator, appliance_id, appliance_name, "power", "Power")

    @property
    def native_value(self) -> float | None:
        """Return current power draw in Watts."""
        state = self._appliance_state()
        if state is None:
            return None
        return state.current_power


class PvApplianceRuntimeSensor(PvApplianceBaseSensor):
    """Sensor reporting today's runtime of an appliance."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.HOURS

    def __init__(
        self,
        coordinator: PvExcessCoordinator,
        appliance_id: str,
        appliance_name: str,
    ) -> None:
        super().__init__(coordinator, appliance_id, appliance_name, "runtime_today", "Runtime Today")

    @property
    def native_value(self) -> float | None:
        """Return today's runtime in hours."""
        state = self._appliance_state()
        if state is None:
            return None
        runtime: timedelta = state.runtime_today
        return round(runtime.total_seconds() / 3600, 4)


class PvApplianceEnergySensor(PvApplianceBaseSensor):
    """Sensor reporting today's energy consumption of an appliance."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(
        self,
        coordinator: PvExcessCoordinator,
        appliance_id: str,
        appliance_name: str,
    ) -> None:
        super().__init__(coordinator, appliance_id, appliance_name, "energy_today", "Energy Today")

    @property
    def last_reset(self):
        """Return the start of today (timezone-aware) for daily-resetting energy counter."""
        from homeassistant.util import dt as dt_util
        return dt_util.start_of_local_day()

    @property
    def native_value(self) -> float | None:
        """Return today's energy consumption in kWh."""
        state = self._appliance_state()
        if state is None:
            return None
        return state.energy_today


class PvApplianceActivationsSensor(PvApplianceBaseSensor):
    """Sensor reporting today's activation count of an appliance."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: PvExcessCoordinator,
        appliance_id: str,
        appliance_name: str,
    ) -> None:
        super().__init__(coordinator, appliance_id, appliance_name, "activations_today", "Activations Today")

    @property
    def native_value(self) -> int | None:
        """Return today's activation count."""
        state = self._appliance_state()
        if state is None:
            return None
        return state.activations_today


class PvApplianceStatusSensor(PvApplianceBaseSensor):
    """Sensor reporting the composed status for an appliance."""

    def __init__(
        self,
        coordinator: PvExcessCoordinator,
        appliance_id: str,
        appliance_name: str,
    ) -> None:
        super().__init__(coordinator, appliance_id, appliance_name, "status", "Status")
        # Per-cycle cache: HA reads native_value and extra_state_attributes
        # back-to-back during a single state refresh, and `_compose` calls
        # `format_status` which itself calls `datetime.now()` for the
        # cooldown computation. Without caching the two reads could see
        # slightly different `now` values, producing inconsistent rendering
        # between the state string and the cooldown_seconds_remaining
        # attribute. Cache by `id(coordinator.data)` so a new coordinator
        # update always invalidates.
        self._compose_cache_key: int = 0
        self._compose_cache_value: FormattedStatus | None = None

    def _compose(self) -> FormattedStatus | None:
        """Run the formatter against current coordinator data.

        Returns None when there is no data at all. During the startup
        grace period, returns a synthetic FormattedStatus with a fixed
        text and empty attributes. Cached per coordinator-data-cycle.
        """
        data = self._data
        cache_key = id(data) if data is not None else 0
        if self._compose_cache_key == cache_key:
            return self._compose_cache_value

        result = self._compose_inner(data)
        self._compose_cache_key = cache_key
        self._compose_cache_value = result
        return result

    def _compose_inner(self, data: dict[str, Any] | None) -> FormattedStatus | None:
        """Uncached compose body. Always called via `_compose`."""
        if data is None:
            return None

        grace_remaining = data.get("grace_period_remaining")
        if grace_remaining is not None and grace_remaining > 0:
            # Use math.ceil so the countdown never shows "0s remaining"
            # while the grace branch is still active. ceil(0.4)=1,
            # ceil(83.7)=84 — the user always sees a positive integer
            # countdown until the very last cycle.
            return FormattedStatus(
                text=(
                    f"Startup grace period - {math.ceil(grace_remaining)}s "
                    f"remaining before decisions begin"
                ),
                action="idle",
                overrides_plan=False,
                cooldown_seconds_remaining=None,
                switch_deferred=False,
                headroom_watts=None,
                plan_action=None,
                plan_window_start=None,
                plan_window_end=None,
            )

        decisions = data.get("control_decisions", [])
        decision = next(
            (d for d in decisions if d.appliance_id == self._appliance_id),
            None,
        )
        if decision is None:
            return None

        appliance_states = data.get("appliance_states", {})
        state = appliance_states.get(self._appliance_id)
        appliance_configs = data.get("appliance_configs", {})
        config = appliance_configs.get(self._appliance_id)
        if state is None or config is None:
            # Fall back to raw reason if we can't look up the surrounding
            # context — attributes will be defaults. This branch indicates
            # a structural mismatch between coordinator state and configs
            # (e.g. an appliance was removed but its decision is still in
            # the list). Truncate to the HA state limit defensively.
            _LOGGER.warning(
                "Status sensor for %s: appliance state or config missing "
                "(state=%s, config=%s); falling back to bare decision reason",
                self._appliance_id,
                "present" if state is not None else "missing",
                "present" if config is not None else "missing",
            )
            text = decision.reason
            if len(text) > 255:
                text = text[: 255 - 3] + "..."
            return FormattedStatus(
                text=text,
                action=decision.action.value,
                overrides_plan=decision.overrides_plan,
                cooldown_seconds_remaining=None,
                switch_deferred=False,
                headroom_watts=None,
                plan_action=None,
                plan_window_start=None,
                plan_window_end=None,
            )

        battery_action = data.get("battery_discharge_action")
        if battery_action is None:
            battery_action = BatteryDischargeAction(should_limit=False)

        plan = data.get("current_plan")
        return format_status(
            decision,
            state,
            config,
            switch_interval=config.switch_interval,
            battery_action=battery_action,
            plan=plan,
            now=datetime.now(),
        )

    @property
    def native_value(self) -> str | None:
        fs = self._compose()
        return fs.text if fs else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fs = self._compose()
        if fs is None:
            return None
        return {
            "action": fs.action,
            "overrides_plan": fs.overrides_plan,
            "cooldown_seconds_remaining": fs.cooldown_seconds_remaining,
            "switch_deferred": fs.switch_deferred,
            "headroom_watts": fs.headroom_watts,
            "plan_action": fs.plan_action,
            "plan_window_start": (
                fs.plan_window_start.isoformat() if fs.plan_window_start else None
            ),
            "plan_window_end": (
                fs.plan_window_end.isoformat() if fs.plan_window_end else None
            ),
        }
