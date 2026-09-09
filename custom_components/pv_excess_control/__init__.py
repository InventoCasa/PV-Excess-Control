"""The PV Excess Control integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_change

from .const import (
    CONF_BATTERY_CHARGE_PRICE_THRESHOLD,
    CONF_BATTERY_MAX_CHARGE_POWER_W,
    CONF_BATTERY_STRATEGY,
    CONF_CHEAP_PRICE_THRESHOLD,
    CONF_DYNAMIC_BATTERY_CHARGE_ENABLED,
    CONF_PLAN_INFLUENCE,
    DOMAIN,
)
from .coordinator import PvExcessCoordinator
from .entity_lifecycle import async_reconcile_appliance_entities

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.BINARY_SENSOR,
    Platform.SELECT,
]

# Keys that represent runtime controls (switches, selects and numbers).
# Changes to ONLY these keys should NOT trigger a full integration reload.
_RUNTIME_STATE_KEYS = frozenset({
    "control_enabled", "force_charge", CONF_BATTERY_STRATEGY,
    CONF_PLAN_INFLUENCE, CONF_CHEAP_PRICE_THRESHOLD, CONF_BATTERY_CHARGE_PRICE_THRESHOLD,
    "disabled_appliances", "overridden_appliances", "paused_appliances",
    "_pending_stop_appliances",
    "_grid_charge_engaged",
})


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle config entry or subentry updates by reloading.

    If only runtime-state keys (control_enabled, force_charge,
    battery_strategy) changed, skip the reload to avoid a 2-minute
    optimisation blackout.
    """
    snapshot_key = f"{entry.entry_id}_config_snapshot"
    subentry_count_key = f"{entry.entry_id}_subentry_count"
    subentry_snapshot_key = f"{entry.entry_id}_subentries_snapshot"
    domain_data = hass.data.get(DOMAIN, {})
    old_snapshot = domain_data.get(snapshot_key)

    if old_snapshot is not None:
        new_data = dict(entry.data)
        # Compare data excluding runtime-state keys
        old_structural = {k: v for k, v in old_snapshot.items() if k not in _RUNTIME_STATE_KEYS}
        new_structural = {k: v for k, v in new_data.items() if k not in _RUNTIME_STATE_KEYS}

        # Also check if subentry count changed (subentry add/remove)
        old_subentry_count = domain_data.get(subentry_count_key, 0)
        new_subentry_count = len(getattr(entry, "subentries", {}))

        new_subentries = {sid: dict(sub.data) for sid, sub in entry.subentries.items()}
        old_subentries = domain_data.get(subentry_snapshot_key)
        same_ids = (
            set(old_subentries) == set(new_subentries)
            if old_subentries is not None else old_subentry_count == new_subentry_count
        )
        if old_structural == new_structural and same_ids:
            _LOGGER.debug(
                "Config entry updated (runtime state only), skipping reload"
            )
            # Update the snapshot so future comparisons are correct
            domain_data[snapshot_key] = new_data
            domain_data[subentry_count_key] = new_subentry_count
            domain_data[subentry_snapshot_key] = new_subentries
            coordinator = domain_data.get(entry.entry_id)
            if coordinator is not None:
                coordinator.update_from_subentries()
                price_changed = any(
                    old_snapshot.get(key) != new_data.get(key)
                    for key in (CONF_CHEAP_PRICE_THRESHOLD, CONF_BATTERY_CHARGE_PRICE_THRESHOLD)
                )
                if old_subentries != new_subentries or price_changed:
                    coordinator.current_plan = None
                    coordinator._planner_counter = int(
                        coordinator._planner_interval / coordinator.update_interval.total_seconds()
                    )
                    for sid, data in new_subentries.items():
                        previous = (old_subentries or {}).get(sid, {})
                        if any(previous.get(key) != data.get(key) for key in ("appliance_entity", "current_entity")):
                            coordinator._last_applied_current.pop(sid, None)
            return

    _LOGGER.info("Config entry updated (structural change), reloading integration")
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up PV Excess Control from a config entry."""
    coordinator = PvExcessCoordinator(hass, entry)
    await coordinator.async_restore_daily_state()
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Store a snapshot of config data so the update listener can detect
    # whether a change is structural (needs reload) or runtime-only.
    hass.data[DOMAIN][f"{entry.entry_id}_config_snapshot"] = dict(entry.data)
    hass.data[DOMAIN][f"{entry.entry_id}_subentry_count"] = len(getattr(entry, "subentries", {}))

    hass.data[DOMAIN][f"{entry.entry_id}_subentries_snapshot"] = {
        sid: dict(sub.data) for sid, sub in entry.subentries.items()
    }

    # Listen for config/subentry changes and reload when they happen
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # Register midnight reset for daily counters
    async def _midnight_reset(now):
        await coordinator.async_handle_midnight()

    entry.async_on_unload(
        async_track_time_change(hass, _midnight_reset, hour=0, minute=0, second=0)
    )

    async_reconcile_appliance_entities(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coord = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coord is not None:
        await coord.async_save_daily_state()
    if coord is not None and entry.data.get(CONF_DYNAMIC_BATTERY_CHARGE_ENABLED, False):
        max_w = int(entry.data.get(CONF_BATTERY_MAX_CHARGE_POWER_W, 0) or 0)
        if max_w > 0:
            try:
                await coord._write_battery_max_charge(max_w)
            except Exception:
                _LOGGER.exception("Failed to release battery max-charge cap on unload")

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        hass.data[DOMAIN].pop(f"{entry.entry_id}_config_snapshot", None)
        hass.data[DOMAIN].pop(f"{entry.entry_id}_subentry_count", None)
        hass.data[DOMAIN].pop(f"{entry.entry_id}_subentries_snapshot", None)

    return unload_ok
