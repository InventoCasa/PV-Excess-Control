"""Real Home Assistant setup, control, reload and registry regression tests."""

from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from homeassistant.helpers import entity_registry as er

from custom_components.pv_excess_control.const import DOMAIN


@pytest.fixture
async def configured_entry(hass, enable_custom_integrations):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "pv_power": "sensor.pv",
            "grid_export_power": "sensor.export",
            "load_power": "sensor.load",
            "controller_interval": 30,
        },
        subentries_data=[
            {
                "subentry_id": "pump",
                "subentry_type": "appliance",
                "unique_id": None,
                "title": "Pump",
                "data": {
                    "appliance_entity": "switch.physical_pump",
                    "appliance_name": "Pump",
                    "nominal_power": 1000,
                    "min_daily_runtime": 60,
                    "appliance_priority": 500,
                },
            }
        ],
    )
    entry.add_to_hass(hass)
    for eid, value in [("sensor.pv", "3500"), ("sensor.export", "400"), ("sensor.load", "3100")]:
        hass.states.async_set(eid, value, {"unit_of_measurement": "W"})
    hass.states.async_set("switch.physical_pump", "on")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    yield entry
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_runtime_control_and_options_reach_same_coordinator(hass, configured_entry):
    entry = configured_entry
    coord = hass.data[DOMAIN][entry.entry_id]
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "number", DOMAIN, f"{entry.entry_id}_pump_min_daily_runtime"
    )
    assert entity_id is not None
    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": 90}, blocking=True
    )
    await hass.async_block_till_done()
    assert entry.subentries["pump"].data["min_daily_runtime"] == 90
    assert coord._get_appliance_configs()[0].min_daily_runtime == timedelta(minutes=90)
    sub = entry.subentries["pump"]
    data = dict(sub.data)
    data["appliance_priority"] = 100
    data["min_daily_runtime"] = 120
    hass.config_entries.async_update_subentry(entry, sub, data=data)
    await hass.async_block_till_done()
    assert hass.data[DOMAIN][entry.entry_id] is coord
    cfg = coord._get_appliance_configs()[0]
    assert (cfg.priority, cfg.min_daily_runtime) == (100, timedelta(hours=2))


async def test_reload_keeps_paused_appliance_counters_and_entity_ids(hass, configured_entry):
    entry = configured_entry
    coord = hass.data[DOMAIN][entry.entry_id]
    coord.appliance_states["pump"].runtime_today = timedelta(hours=2)
    coord.appliance_states["pump"].energy_today = 2.0
    registry = er.async_get(hass)
    before = {
        e.unique_id: e.entity_id
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    pause = registry.async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}_pump_paused")
    await hass.services.async_call("switch", "turn_on", {"entity_id": pause}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get("switch.physical_pump").state == "on"
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    replacement = hass.data[DOMAIN][entry.entry_id]
    assert replacement.appliance_paused["pump"]
    assert replacement.appliance_states["pump"].runtime_today == timedelta(hours=2)
    assert replacement.appliance_states["pump"].energy_today == 2
    assert {
        e.unique_id: e.entity_id
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    } == before
