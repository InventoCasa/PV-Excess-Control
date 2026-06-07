"""The PV Excess Control integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_change

from .const import CONF_BATTERY_STRATEGY, DOMAIN
from .coordinator import PvExcessCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.BINARY_SENSOR,
    Platform.SELECT,
]

# Keys that represent runtime state (toggled via switches/selects).
# Changes to ONLY these keys should NOT trigger a full integration reload.
_RUNTIME_STATE_KEYS = frozenset({
    "control_enabled", "force_charge", CONF_BATTERY_STRATEGY,
    "disabled_appliances", "overridden_appliances",
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

        if old_structural == new_structural and old_subentry_count == new_subentry_count:
            _LOGGER.debug(
                "Config entry updated (runtime state only), skipping reload"
            )
            # Update the snapshot so future comparisons are correct
            domain_data[snapshot_key] = new_data
            domain_data[subentry_count_key] = new_subentry_count
            return

    _LOGGER.info("Config entry updated (structural change), reloading integration")
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up PV Excess Control from a config entry."""
    coordinator = PvExcessCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Store a snapshot of config data so the update listener can detect
    # whether a change is structural (needs reload) or runtime-only.
    hass.data[DOMAIN][f"{entry.entry_id}_config_snapshot"] = dict(entry.data)
    hass.data[DOMAIN][f"{entry.entry_id}_subentry_count"] = len(getattr(entry, "subentries", {}))

    # Listen for config/subentry changes and reload when they happen
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # Register midnight reset for daily counters
    async def _midnight_reset(now):
        # Send daily summary before resetting counters
        try:
            await coordinator.notifications.notify_daily_summary(
                coordinator.analytics.self_consumption_ratio,
                coordinator.analytics.savings_today,
                coordinator.analytics.solar_consumed_kwh,
            )
        except Exception:
            _LOGGER.exception("Failed to send daily summary notification")
        coordinator.reset_daily()
        await coordinator.async_request_refresh()

    entry.async_on_unload(
        async_track_time_change(hass, _midnight_reset, hour=0, minute=0, second=0)
    )

    # Clean up orphaned entities from deleted subentries (appliances).
    # Unique IDs have the form "<entry_id>_<subentry_id>_<suffix>"; both IDs are
    # 26-char ULIDs, so we skip the entry_id itself to avoid deleting system entities.
    ent_reg = er.async_get(hass)
    active_subentry_ids = set(getattr(entry, "subentries", {}).keys())
    for reg_entry in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        parts = reg_entry.unique_id.split("_")
        for part in parts:
            if len(part) == 26 and part != entry.entry_id and part not in active_subentry_ids:
                _LOGGER.info("Removing orphaned entity %s", reg_entry.entity_id)
                ent_reg.async_remove(reg_entry.entity_id)
                break

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        hass.data[DOMAIN].pop(f"{entry.entry_id}_config_snapshot", None)
        hass.data[DOMAIN].pop(f"{entry.entry_id}_subentry_count", None)

    return unload_ok
