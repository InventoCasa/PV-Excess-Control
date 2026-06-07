"""Binary sensor platform for PV Excess Control."""
from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_APPLIANCE_NAME, DOMAIN, MANUFACTURER
from .coordinator import PvExcessCoordinator

_LOGGER = logging.getLogger(__name__)

# Minimum excess power threshold to avoid flickering at night (e.g. small
# battery-to-grid exports).  The binary sensor only turns ON when excess
# power exceeds this value.
EXCESS_AVAILABLE_THRESHOLD = 50  # Watts


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PV Excess Control binary sensor entities."""
    coordinator: PvExcessCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    # System-level binary sensors (no subentry association)
    async_add_entities([
        ExcessAvailableBinarySensor(coordinator),
    ])

    # Per-appliance active sensors
    subentries = getattr(config_entry, "subentries", {})
    for subentry_id, subentry in subentries.items():
        appliance_name = subentry.data.get(CONF_APPLIANCE_NAME, f"Appliance {subentry_id}")
        async_add_entities([
            ApplianceActiveBinarySensor(coordinator, subentry_id, appliance_name),
        ], config_subentry_id=subentry_id)


class _PvExcessBinarySensorBase(
    CoordinatorEntity[PvExcessCoordinator], BinarySensorEntity
):
    """Base class for PV Excess Control binary sensor entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PvExcessCoordinator) -> None:
        super().__init__(coordinator)

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.config_entry.entry_id)},
            name="PV Excess Control",
            manufacturer=MANUFACTURER,
        )


class ExcessAvailableBinarySensor(_PvExcessBinarySensorBase):
    """Binary sensor that is ON when excess PV power is available.

    Uses averaged excess from power history (not instantaneous) to prevent
    flickering from brief battery-to-grid exports or transient spikes.
    """

    _attr_name = "Excess Available"
    _attr_icon = "mdi:solar-power"

    def __init__(self, coordinator: PvExcessCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_excess_available"

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data or {}
        history = data.get("power_history", [])
        if not history:
            power_state = data.get("power_state")
            if power_state is None:
                return False
            if power_state.excess_power is None:
                # Unknown excess — conservatively report "no usable excess."
                return False
            return power_state.excess_power > EXCESS_AVAILABLE_THRESHOLD
        # Use averaged excess from history to smooth out transient spikes.
        # Skip None samples (sensor was unavailable that cycle).
        good = [
            ps.excess_power
            for ps in history
            if ps.excess_power is not None
        ]
        if not good:
            return False
        avg_excess = sum(good) / len(good)
        return avg_excess > EXCESS_AVAILABLE_THRESHOLD


class ApplianceActiveBinarySensor(_PvExcessBinarySensorBase):
    """Binary sensor that is ON when an appliance is currently active."""

    _attr_icon = "mdi:lightning-bolt"

    def __init__(
        self,
        coordinator: PvExcessCoordinator,
        appliance_id: str,
        appliance_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._appliance_id = appliance_id
        self._appliance_name = appliance_name
        self._attr_name = f"{appliance_name} Active"
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{appliance_id}_active"
        )
        self._attr_config_subentry_id = appliance_id

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data or {}
        appliance_states = data.get("appliance_states", {})
        appliance_state = appliance_states.get(self._appliance_id)
        if appliance_state is None:
            return False
        return appliance_state.is_on
