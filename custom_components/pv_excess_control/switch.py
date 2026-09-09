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

from .const import CONF_APPLIANCE_NAME, CONF_BATTERY_GRID_CHARGE_POWER_W, DOMAIN
from .coordinator import PvExcessCoordinator
from .entity_lifecycle import add_entities_by_subentry, device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PV Excess Control switch entities."""
    coordinator: PvExcessCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    entities: list[SwitchEntity] = [
        ControlEnabledSwitch(coordinator),
        ForceChargeSwitch(coordinator),
    ]

    # Per-appliance switches
    subentries = getattr(config_entry, "subentries", {})
    for subentry_id, subentry in subentries.items():
        appliance_name = subentry.data.get(CONF_APPLIANCE_NAME, f"Appliance {subentry_id}")
        entities.append(ApplianceEnabledSwitch(coordinator, subentry_id, appliance_name))
        entities.append(ApplianceOverrideSwitch(coordinator, subentry_id, appliance_name))
        entities.append(AppliancePausedSwitch(coordinator, subentry_id, appliance_name))
        entities.append(ApplianceGridSupplementSwitch(coordinator, subentry_id, appliance_name))

    add_entities_by_subentry(async_add_entities, entities)


class _PvExcessSwitchBase(CoordinatorEntity[PvExcessCoordinator], SwitchEntity):
    """Base class for PV Excess Control switch entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PvExcessCoordinator) -> None:
        super().__init__(coordinator)

    @property
    def device_info(self) -> DeviceInfo:
        return device_info(
            self.coordinator.config_entry,
            getattr(self, "_appliance_id", None),
            getattr(self, "_appliance_name", None),
        )

    def _persist(self, key: str, value, **updates) -> None:
        """Persist state to config_entry.data so it survives restarts."""
        try:
            new_data = dict(self.coordinator.config_entry.data)
            new_data[key] = value
            new_data.update(updates)
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
        self._attr_name = "Enabled"
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{appliance_id}_enabled"
        )

    @property
    def is_on(self) -> bool:
        return self.coordinator.appliance_enabled.get(self._appliance_id, True)

    def _persist_disabled_list(self) -> None:
        """Persist the list of disabled appliance IDs to config_entry.data."""
        disabled = [
            aid for aid, enabled in self.coordinator.appliance_enabled.items()
            if not enabled
        ]
        self._persist("disabled_appliances", disabled, overridden_appliances=[
            aid for aid, overridden in self.coordinator.appliance_overrides.items() if overridden
        ])

    async def async_turn_on(self, **kwargs) -> None:
        self.coordinator.cancel_pending_stop(self._appliance_id)
        self.coordinator.appliance_enabled[self._appliance_id] = True
        self._persist_disabled_list()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.appliance_enabled[self._appliance_id] = False
        self.coordinator.appliance_overrides[self._appliance_id] = False
        self._persist_disabled_list()
        await self.coordinator.async_stop_appliance(self._appliance_id)
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
        self._attr_name = "Override"
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{appliance_id}_override"
        )

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
        self.coordinator.cancel_pending_stop(self._appliance_id)
        self.coordinator.appliance_overrides[self._appliance_id] = True
        self._persist_overridden_list()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.appliance_overrides[self._appliance_id] = False
        self._persist_overridden_list()
        if not self.coordinator.appliance_enabled.get(self._appliance_id, True):
            await self.coordinator.async_stop_appliance(self._appliance_id)
        self.async_write_ha_state()


class AppliancePausedSwitch(_PvExcessSwitchBase):
    """Suspend automatic control without changing the physical appliance."""

    _attr_icon = "mdi:pause-circle-outline"
    _attr_translation_key = "appliance_paused"

    def __init__(self, coordinator, appliance_id: str, appliance_name: str) -> None:
        super().__init__(coordinator)
        self._appliance_id = appliance_id
        self._appliance_name = appliance_name
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{appliance_id}_paused"

    @property
    def is_on(self) -> bool:
        return getattr(self.coordinator, "appliance_paused", {}).get(self._appliance_id, False)

    async def _set_paused(self, paused: bool) -> None:
        if not hasattr(self.coordinator, "appliance_paused"):
            self.coordinator.appliance_paused = {}
        self.coordinator.appliance_paused[self._appliance_id] = paused
        self._persist("paused_appliances", [
            aid for aid, value in self.coordinator.appliance_paused.items() if value
        ])
        self.coordinator.current_plan = None
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        await self._set_paused(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._set_paused(False)


class ApplianceGridSupplementSwitch(_PvExcessSwitchBase):
    """Allow this consumer to use explicitly priced grid supplementation."""

    _attr_translation_key = "allow_grid_supplement"
    _attr_icon = "mdi:transmission-tower-import"

    def __init__(self, coordinator, appliance_id: str, appliance_name: str) -> None:
        super().__init__(coordinator)
        self._appliance_id = appliance_id
        self._appliance_name = appliance_name
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{appliance_id}_allow_grid_supplement"

    @property
    def is_on(self) -> bool:
        subentry = self.coordinator.config_entry.subentries.get(self._appliance_id)
        return bool(subentry and subentry.data.get("allow_grid_supplement", False))

    async def _set_allowed(self, allowed: bool) -> None:
        entry = self.coordinator.config_entry
        subentry = entry.subentries[self._appliance_id]
        data = {**subentry.data, "allow_grid_supplement": allowed}
        self.hass.config_entries.async_update_subentry(entry, subentry, data=data)
        self.coordinator.current_plan = None
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        await self._set_allowed(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._set_allowed(False)
