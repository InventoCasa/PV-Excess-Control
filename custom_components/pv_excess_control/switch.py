"""Switch platform for PV Excess Control."""
from __future__ import annotations

import logging
import time as _time

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_APPLIANCE_NAME, CONF_BATTERY_GRID_CHARGE_POWER_W, DOMAIN, MANUFACTURER
from .coordinator import PvExcessCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PV Excess Control switch entities."""
    coordinator: PvExcessCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    # System-level switches (no subentry association)
    async_add_entities([
        ControlEnabledSwitch(coordinator),
        ForceChargeSwitch(coordinator),
    ])

    # Per-appliance switches
    subentries = getattr(config_entry, "subentries", {})
    for subentry_id, subentry in subentries.items():
        appliance_name = subentry.data.get(CONF_APPLIANCE_NAME, f"Appliance {subentry_id}")
        async_add_entities([
            ApplianceEnabledSwitch(coordinator, subentry_id, appliance_name),
            ApplianceOverrideSwitch(coordinator, subentry_id, appliance_name),
        ], config_subentry_id=subentry_id)


class _PvExcessSwitchBase(CoordinatorEntity[PvExcessCoordinator], SwitchEntity):
    """Base class for PV Excess Control switch entities."""

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

    def _persist(self, key: str, value) -> None:
        """Persist state to config_entry.data so it survives restarts."""
        try:
            new_data = dict(self.coordinator.config_entry.data)
            new_data[key] = value
            self.hass.config_entries.async_update_entry(
                self.coordinator.config_entry, data=new_data
            )
        except Exception as err:
            _LOGGER.warning("Could not persist %s change: %s", key, err)


class ControlEnabledSwitch(_PvExcessSwitchBase):
    """Master enable/disable switch for the controller."""

    _attr_name = "Control Enabled"
    _attr_icon = "mdi:power"

    def __init__(self, coordinator: PvExcessCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_control_enabled"

    @property
    def is_on(self) -> bool:
        return self.coordinator.enabled

    async def async_turn_on(self, **kwargs) -> None:
        self.coordinator.enabled = True
        self._persist("control_enabled", True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.enabled = False
        self._persist("control_enabled", False)
        self.async_write_ha_state()


class ForceChargeSwitch(_PvExcessSwitchBase):
    """Switch to force battery charging by shedding all managed appliances."""

    _attr_name = "Force Charge"
    _attr_icon = "mdi:battery-charging"

    def __init__(self, coordinator: PvExcessCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_force_charge"

    @property
    def is_on(self) -> bool:
        return self.coordinator.force_charge

    async def async_turn_on(self, **kwargs) -> None:
        self.coordinator.force_charge = True
        self._persist("force_charge", True)
        if (
            self.coordinator._inverter_ctl is not None
            and not self.coordinator._grid_charge_engaged
        ):
            power_w = self.coordinator.config_entry.data.get(CONF_BATTERY_GRID_CHARGE_POWER_W)
            if power_w is not None:
                await self.coordinator._inverter_ctl.engage(power_w)
                self.coordinator._grid_charge_engaged = True
                self.coordinator._grid_charge_engage_ts = _time.monotonic()
                self.coordinator._persist_grid_charge_state(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.force_charge = False
        self._persist("force_charge", False)
        if (
            self.coordinator._grid_charge_engaged
            and self.coordinator._inverter_ctl is not None
            and not self.coordinator.auto_should_engage_now()
        ):
            await self.coordinator._inverter_ctl.disengage()
            self.coordinator._grid_charge_engaged = False
            self.coordinator._grid_charge_engage_ts = None
            self.coordinator._persist_grid_charge_state(False)
        self.async_write_ha_state()


class ApplianceEnabledSwitch(_PvExcessSwitchBase):
    """Per-appliance enable/disable switch."""

    _attr_icon = "mdi:toggle-switch"

    def __init__(
        self,
        coordinator: PvExcessCoordinator,
        appliance_id: str,
        appliance_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._appliance_id = appliance_id
        self._appliance_name = appliance_name
        self._attr_name = f"{appliance_name} Enabled"
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{appliance_id}_enabled"
        )
        self._attr_config_subentry_id = appliance_id

    @property
    def is_on(self) -> bool:
        return self.coordinator.appliance_enabled.get(self._appliance_id, True)

    def _persist_disabled_list(self) -> None:
        """Persist the list of disabled appliance IDs to config_entry.data."""
        disabled = [
            aid for aid, enabled in self.coordinator.appliance_enabled.items()
            if not enabled
        ]
        self._persist("disabled_appliances", disabled)

    async def async_turn_on(self, **kwargs) -> None:
        self.coordinator.appliance_enabled[self._appliance_id] = True
        self._persist_disabled_list()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.appliance_enabled[self._appliance_id] = False
        self._persist_disabled_list()
        # Turn off the physical appliance when disabled
        config = self.coordinator._get_appliance_config_by_id(self._appliance_id)
        if config and config.entity_id:
            entity_id = config.entity_id
            domain = entity_id.split(".")[0] if "." in entity_id else "switch"
            try:
                await self.hass.services.async_call(
                    domain, "turn_off", {"entity_id": entity_id}, blocking=True,
                )
            except Exception:
                pass  # Best effort
        self.async_write_ha_state()


class ApplianceOverrideSwitch(_PvExcessSwitchBase):
    """Per-appliance manual override switch."""

    _attr_icon = "mdi:hand-back-right"

    def __init__(
        self,
        coordinator: PvExcessCoordinator,
        appliance_id: str,
        appliance_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._appliance_id = appliance_id
        self._appliance_name = appliance_name
        self._attr_name = f"{appliance_name} Override"
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{appliance_id}_override"
        )
        self._attr_config_subentry_id = appliance_id

    @property
    def is_on(self) -> bool:
        return self.coordinator.appliance_overrides.get(self._appliance_id, False)

    def _persist_overridden_list(self) -> None:
        """Persist the list of overridden appliance IDs to config_entry.data."""
        overridden = [
            aid for aid, ov in self.coordinator.appliance_overrides.items()
            if ov
        ]
        self._persist("overridden_appliances", overridden)

    async def async_turn_on(self, **kwargs) -> None:
        self.coordinator.appliance_overrides[self._appliance_id] = True
        self._persist_overridden_list()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.appliance_overrides[self._appliance_id] = False
        self._persist_overridden_list()
        self.async_write_ha_state()
