"""Number platform for PV Excess Control."""
from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_APPLIANCE_NAME,
    CONF_CHEAP_PRICE_THRESHOLD,
    CONF_MIN_DAILY_RUNTIME,
    CONF_MAX_DAILY_RUNTIME,
    DOMAIN,
    MAX_PRIORITY,
    MIN_PRIORITY,
)
from .coordinator import PvExcessCoordinator
from .entity_lifecycle import add_entities_by_subentry, device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PV Excess Control number entities."""
    coordinator: PvExcessCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    entities: list[NumberEntity] = [
        GlobalPriceThresholdNumber(coordinator, "cheap_price_threshold"),
        GlobalPriceThresholdNumber(coordinator, "battery_charge_price_threshold"),
    ]

    # Per-appliance number entities: priority, min/max daily runtime
    subentries = getattr(config_entry, "subentries", {})
    for subentry_id, subentry in subentries.items():
        appliance_name = subentry.data.get(CONF_APPLIANCE_NAME, f"Appliance {subentry_id}")
        entities.append(AppliancePriorityNumber(coordinator, subentry_id, appliance_name))
        entities.append(ApplianceMinDailyRuntimeNumber(coordinator, subentry_id, appliance_name))
        entities.append(ApplianceMaxDailyRuntimeNumber(coordinator, subentry_id, appliance_name))
        entities.append(ApplianceCheapPriceThresholdNumber(coordinator, subentry_id, appliance_name))

    add_entities_by_subentry(async_add_entities, entities)


class AppliancePriorityNumber(CoordinatorEntity[PvExcessCoordinator], NumberEntity):
    """Per-appliance priority number entity."""

    _attr_has_entity_name = True
    _attr_native_min_value = float(MIN_PRIORITY)
    _attr_native_max_value = float(MAX_PRIORITY)
    _attr_native_step = 1.0
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:priority-high"

    def __init__(
        self,
        coordinator: PvExcessCoordinator,
        appliance_id: str,
        appliance_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._appliance_id = appliance_id
        self._appliance_name = appliance_name
        self._attr_name = "Priority"
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{appliance_id}_priority"
        )

    @property
    def device_info(self) -> DeviceInfo:
        return device_info(
            self.coordinator.config_entry,
            getattr(self, "_appliance_id", None),
            getattr(self, "_appliance_name", None),
        )

    @property
    def native_value(self) -> float:
        return float(
            self.coordinator.appliance_priorities.get(self._appliance_id, 500)
        )

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.appliance_priorities[self._appliance_id] = int(value)
        # Persist to config entry subentry data so value survives restarts
        try:
            subentries = getattr(self.coordinator.config_entry, "subentries", {})
            subentry = subentries.get(self._appliance_id)
            if subentry is not None:
                new_data = dict(subentry.data)
                new_data["appliance_priority"] = int(value)
                self.hass.config_entries.async_update_subentry(
                    self.coordinator.config_entry, subentry, data=new_data
                )
        except Exception:
            # async_update_subentry may not exist in older HA versions;
            # runtime override still works until restart
            _LOGGER.debug("Could not persist priority for %s (HA version may not support subentry updates)", self._appliance_id)
        self.async_write_ha_state()


class ApplianceMinDailyRuntimeNumber(CoordinatorEntity[PvExcessCoordinator], NumberEntity):
    """Per-appliance minimum daily runtime (minutes).

    0 means "disabled" (no minimum). Writes persist to the subentry so
    the value survives restarts. If the user writes a value above the
    current max, it is silently clamped down to max to maintain
    min <= max. The Max entity handles the symmetric case.
    """

    _attr_has_entity_name = True
    _attr_native_min_value = 0.0
    _attr_native_max_value = 1440.0
    _attr_native_step = 1.0
    _attr_native_unit_of_measurement = "min"
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:timer-sand"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: PvExcessCoordinator,
        appliance_id: str,
        appliance_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._appliance_id = appliance_id
        self._appliance_name = appliance_name
        self._attr_name = "Min Daily Runtime"
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{appliance_id}_min_daily_runtime"
        )

    @property
    def device_info(self) -> DeviceInfo:
        return device_info(
            self.coordinator.config_entry,
            getattr(self, "_appliance_id", None),
            getattr(self, "_appliance_name", None),
        )

    @property
    def native_value(self) -> float:
        raw = self.coordinator.appliance_min_daily_runtime.get(self._appliance_id)
        return 0.0 if raw is None else float(raw)

    async def async_set_native_value(self, value: float) -> None:
        new_value: int | None = None if int(value) == 0 else int(value)
        subentry = self.coordinator.config_entry.subentries.get(self._appliance_id)
        if new_value is not None and subentry and subentry.data.get("remaining_runtime_entity"):
            raise HomeAssistantError("A fixed minimum cannot be combined with a remaining-runtime entity")
        if new_value is not None:
            max_effective = self._effective_max()
            if max_effective is not None and new_value > max_effective:
                new_value = max_effective
        self.coordinator.appliance_min_daily_runtime[self._appliance_id] = new_value
        await self._persist_to_subentry(new_value)
        self.async_write_ha_state()

    def _effective_max(self) -> int | None:
        """Read current max from the runtime dict, falling through to subentry.data."""
        runtime = self.coordinator.appliance_max_daily_runtime
        if self._appliance_id in runtime:
            return runtime[self._appliance_id]
        subentries = getattr(self.coordinator.config_entry, "subentries", {})
        subentry = subentries.get(self._appliance_id) if subentries else None
        if subentry is None:
            return None
        return subentry.data.get(CONF_MAX_DAILY_RUNTIME)

    async def _persist_to_subentry(self, new_value: int | None) -> None:
        try:
            subentries = getattr(self.coordinator.config_entry, "subentries", {})
            subentry = subentries.get(self._appliance_id)
            if subentry is not None:
                new_data = dict(subentry.data)
                if new_value is None:
                    new_data.pop(CONF_MIN_DAILY_RUNTIME, None)
                else:
                    new_data[CONF_MIN_DAILY_RUNTIME] = new_value
                self.hass.config_entries.async_update_subentry(
                    self.coordinator.config_entry, subentry, data=new_data
                )
        except Exception:
            _LOGGER.debug(
                "Could not persist min_daily_runtime for %s (HA version may not support subentry updates)",
                self._appliance_id,
            )


class ApplianceMaxDailyRuntimeNumber(CoordinatorEntity[PvExcessCoordinator], NumberEntity):
    """Per-appliance maximum daily runtime (minutes).

    0 means "disabled" (no maximum). Writes persist to the subentry so
    the value survives restarts. If the user writes a value below the
    current min, the min is silently lowered to match before the max
    is written, so min <= max always holds. The Min entity's state
    refreshes on its next scheduled update.
    """

    _attr_has_entity_name = True
    _attr_native_min_value = 0.0
    _attr_native_max_value = 1440.0
    _attr_native_step = 1.0
    _attr_native_unit_of_measurement = "min"
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:timer-alert-outline"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: PvExcessCoordinator,
        appliance_id: str,
        appliance_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._appliance_id = appliance_id
        self._appliance_name = appliance_name
        self._attr_name = "Max Daily Runtime"
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{appliance_id}_max_daily_runtime"
        )

    @property
    def device_info(self) -> DeviceInfo:
        return device_info(
            self.coordinator.config_entry,
            getattr(self, "_appliance_id", None),
            getattr(self, "_appliance_name", None),
        )

    @property
    def native_value(self) -> float:
        raw = self.coordinator.appliance_max_daily_runtime.get(self._appliance_id)
        return 0.0 if raw is None else float(raw)

    async def async_set_native_value(self, value: float) -> None:
        new_value: int | None = None if int(value) == 0 else int(value)
        subentry = self.coordinator.config_entry.subentries.get(self._appliance_id)
        if new_value is None and subentry and subentry.data.get("require_contiguous_runtime"):
            raise HomeAssistantError("Continuous runtime requires a positive daily maximum")
        if new_value is not None:
            min_effective = self._effective_min()
            if min_effective is not None and new_value < min_effective:
                # Lower the sibling (min) to match the new max before writing self.
                self.coordinator.appliance_min_daily_runtime[self._appliance_id] = new_value
                await self._persist_min_to_subentry(new_value)
        self.coordinator.appliance_max_daily_runtime[self._appliance_id] = new_value
        await self._persist_to_subentry(new_value)
        self.async_write_ha_state()

    def _effective_min(self) -> int | None:
        """Read current min from the runtime dict, falling through to subentry.data."""
        runtime = self.coordinator.appliance_min_daily_runtime
        if self._appliance_id in runtime:
            return runtime[self._appliance_id]
        subentries = getattr(self.coordinator.config_entry, "subentries", {})
        subentry = subentries.get(self._appliance_id) if subentries else None
        if subentry is None:
            return None
        return subentry.data.get(CONF_MIN_DAILY_RUNTIME)

    async def _persist_min_to_subentry(self, new_value: int | None) -> None:
        """Write the sibling min_daily_runtime into subentry.data."""
        try:
            subentries = getattr(self.coordinator.config_entry, "subentries", {})
            subentry = subentries.get(self._appliance_id)
            if subentry is not None:
                new_data = dict(subentry.data)
                if new_value is None:
                    new_data.pop(CONF_MIN_DAILY_RUNTIME, None)
                else:
                    new_data[CONF_MIN_DAILY_RUNTIME] = new_value
                self.hass.config_entries.async_update_subentry(
                    self.coordinator.config_entry, subentry, data=new_data
                )
        except Exception:
            _LOGGER.debug(
                "Could not persist sibling min_daily_runtime for %s (HA version may not support subentry updates)",
                self._appliance_id,
            )

    async def _persist_to_subentry(self, new_value: int | None) -> None:
        try:
            subentries = getattr(self.coordinator.config_entry, "subentries", {})
            subentry = subentries.get(self._appliance_id)
            if subentry is not None:
                new_data = dict(subentry.data)
                if new_value is None:
                    new_data.pop(CONF_MAX_DAILY_RUNTIME, None)
                else:
                    new_data[CONF_MAX_DAILY_RUNTIME] = new_value
                self.hass.config_entries.async_update_subentry(
                    self.coordinator.config_entry, subentry, data=new_data
                )
        except Exception:
            _LOGGER.debug(
                "Could not persist max_daily_runtime for %s (HA version may not support subentry updates)",
                self._appliance_id,
            )


class ApplianceCheapPriceThresholdNumber(CoordinatorEntity[PvExcessCoordinator], NumberEntity):
    """Per-appliance cheap price threshold (currency/kWh).

    Reads the per-appliance override from subentry.data when set, otherwise
    falls back to the global threshold on the main config entry so users
    see the effective value being applied. Writes always create a
    per-appliance override on the subentry. To remove the per-appliance
    override and fall back to the global threshold, edit the appliance
    via the config flow and clear the field there.
    """

    _attr_has_entity_name = True
    _attr_native_min_value = -1000.0
    _attr_native_max_value = 1000.0
    _attr_native_step = 0.001
    _attr_native_unit_of_measurement = "currency/kWh"
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:cash-edit"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: PvExcessCoordinator,
        appliance_id: str,
        appliance_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._appliance_id = appliance_id
        self._appliance_name = appliance_name
        self._attr_name = "Cheap Price Threshold"
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{appliance_id}_cheap_price_threshold"
        )

    @property
    def device_info(self) -> DeviceInfo:
        return device_info(
            self.coordinator.config_entry,
            getattr(self, "_appliance_id", None),
            getattr(self, "_appliance_name", None),
        )

    @property
    def native_value(self) -> float:
        subentries = getattr(self.coordinator.config_entry, "subentries", {})
        subentry = subentries.get(self._appliance_id) if subentries else None
        if subentry is not None and CONF_CHEAP_PRICE_THRESHOLD in subentry.data:
            return float(subentry.data[CONF_CHEAP_PRICE_THRESHOLD])
        return float(
            self.coordinator.config_entry.data.get(CONF_CHEAP_PRICE_THRESHOLD, 0.0)
        )

    async def async_set_native_value(self, value: float) -> None:
        try:
            subentries = getattr(self.coordinator.config_entry, "subentries", {})
            subentry = subentries.get(self._appliance_id)
            if subentry is not None:
                new_data = dict(subentry.data)
                new_data[CONF_CHEAP_PRICE_THRESHOLD] = float(value)
                self.hass.config_entries.async_update_subentry(
                    self.coordinator.config_entry, subentry, data=new_data
                )
        except Exception:
            _LOGGER.debug(
                "Could not persist cheap_price_threshold for %s (HA version may not support subentry updates)",
                self._appliance_id,
            )
        self.async_write_ha_state()


class GlobalPriceThresholdNumber(CoordinatorEntity[PvExcessCoordinator], NumberEntity):
    """Writable end-customer price limit shared by the controller and planner."""

    _attr_has_entity_name = True
    _attr_native_min_value = -1000.0
    _attr_native_max_value = 1000.0
    _attr_native_step = 0.001
    _attr_native_unit_of_measurement = "currency/kWh"
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:cash-edit"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_translation_key = key
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_global_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        return device_info(self.coordinator.config_entry)

    @property
    def native_value(self) -> float:
        return float(self.coordinator.config_entry.data.get(self._key, 0.0))

    async def async_set_native_value(self, value: float) -> None:
        entry = self.coordinator.config_entry
        self.hass.config_entries.async_update_entry(entry, data={**entry.data, self._key: float(value)})
        self.coordinator.current_plan = None
        self.async_write_ha_state()
