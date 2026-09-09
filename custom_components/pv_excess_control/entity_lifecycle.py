"""Shared entity ownership and migration for appliance subentries."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity import DeviceInfo, Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_APPLIANCE_NAME, DOMAIN, MANUFACTURER


def device_info(
    entry: ConfigEntry, appliance_id: str | None = None, appliance_name: str | None = None
) -> DeviceInfo:
    """Give every appliance a logical device with one subentry owner."""
    if appliance_id is None:
        return DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="PV Excess Control",
            manufacturer=MANUFACTURER,
        )
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_appliance_{appliance_id}")},
        name=appliance_name or f"Appliance {appliance_id}",
        manufacturer=MANUFACTURER,
        via_device=(DOMAIN, entry.entry_id),
    )


def add_entities_by_subentry(
    async_add_entities: AddEntitiesCallback, entities: Iterable[Entity]
) -> None:
    """Register each appliance group under its own config subentry."""
    groups: dict[str | None, list[Entity]] = defaultdict(list)
    for entity in entities:
        groups[getattr(entity, "_appliance_id", None)].append(entity)
    for subentry_id, group in groups.items():
        if subentry_id is None:
            async_add_entities(group)
        else:
            async_add_entities(group, config_subentry_id=subentry_id)


# Match only unique IDs emitted by our appliance platforms. Sensor IDs have
# historically included an additional "appliance_" prefix; retain that format.
_APPLIANCE_SUFFIXES = {
    "sensor": ("power", "runtime_today", "energy_today", "activations_today", "status"),
    "binary_sensor": ("active",),
    "number": ("priority", "min_daily_runtime", "max_daily_runtime", "cheap_price_threshold"),
    "switch": ("enabled", "override", "paused", "allow_grid_supplement"),
}


def _appliance_id(entry_id: str, entity: er.RegistryEntry) -> str | None:
    """Recognize our appliance IDs without relying on subentry ID length."""
    if entity.domain == "switch" and entity.unique_id in {
        f"{entry_id}_control_enabled",
        f"{entry_id}_force_charge",
    }:
        return None
    if entity.domain == "number" and entity.unique_id in {f"{entry_id}_global_cheap_price_threshold", f"{entry_id}_global_battery_charge_price_threshold"}:
        return None
    prefix = f"{entry_id}_appliance_" if entity.domain == "sensor" else f"{entry_id}_"
    if not entity.unique_id.startswith(prefix):
        return None
    remainder = entity.unique_id[len(prefix) :]
    for suffix in _APPLIANCE_SUFFIXES.get(entity.domain, ()):
        ending = f"_{suffix}"
        if remainder.endswith(ending):
            return remainder[: -len(ending)] or None
    return None


@callback
def async_reconcile_appliance_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Migrate legacy registry ownership before forwarding entity platforms.

    Update existing records in place so custom entity IDs, names, disablement,
    areas and automation references survive. Remove only recognized orphan
    records belonging to this integration and config entry.
    """
    registry = er.async_get(hass)
    devices = dr.async_get(hass)
    # Platforms are forwarded concurrently, so create the parent before any
    # appliance platform can reference it, including on a fresh installation.
    if devices.async_get_device(identifiers={(DOMAIN, entry.entry_id)}) is None:
        devices.async_get_or_create(config_entry_id=entry.entry_id, **device_info(entry))
    subentries = entry.subentries
    orphan_devices: dict[str, str] = {}
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.platform != DOMAIN or (aid := _appliance_id(entry.entry_id, entity)) is None:
            continue
        if aid not in subentries:
            if entity.device_id:
                orphan_devices[entity.device_id] = aid
            registry.async_remove(entity.entity_id)
            continue
        info = device_info(entry, aid, subentries[aid].data.get(CONF_APPLIANCE_NAME))
        device = devices.async_get_or_create(
            config_entry_id=entry.entry_id,
            config_subentry_id=aid,
            **info,
        )
        registry.async_update_entity(
            entity.entity_id,
            config_subentry_id=aid,
            device_id=device.id,
        )

    for device_id, aid in orphan_devices.items():
        device = devices.async_get(device_id)
        if (
            device is None
            or device.identifiers != {(DOMAIN, f"{entry.entry_id}_appliance_{aid}")}
            or device.connections
            or er.async_entries_for_device(registry, device_id, include_disabled_entities=True)
        ):
            continue
        # HA 2026.8 moved from a set of owners to one config entry per device.
        owner = getattr(device, "config_entry_id", None)
        if owner == entry.entry_id or (owner is None and device.config_entries == {entry.entry_id}):
            devices.async_remove_device(device_id)
