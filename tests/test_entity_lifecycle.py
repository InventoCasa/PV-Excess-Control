"""Regression coverage for appliance ownership and registry migration."""

from importlib import import_module
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.pv_excess_control.const import CONF_APPLIANCE_NAME, DOMAIN


@pytest.mark.parametrize(
    "platform,root_count,appliance_count",
    [
        ("sensor", 3, 5),
        ("binary_sensor", 1, 1),
        ("switch", 2, 4),
        ("number", 2, 4),
        ("select", 2, 0),
    ],
)
async def test_platform_registers_distinct_appliance_ownership(
    platform, root_count, appliance_count
):
    """Two appliances must not assign their subentries to the root device."""
    module = import_module(f"custom_components.{DOMAIN}.{platform}")
    entry = SimpleNamespace(
        entry_id="entry",
        subentries={
            aid: SimpleNamespace(data={CONF_APPLIANCE_NAME: name})
            for aid, name in (("washer", "Washer"), ("heater", "Heater"))
        },
    )
    coordinator = MagicMock(config_entry=entry)
    hass = SimpleNamespace(data={DOMAIN: {entry.entry_id: coordinator}})
    registrations = []

    def add_entities(entities, *, config_subentry_id=None):
        registrations.extend((entity, config_subentry_id) for entity in entities)

    await module.async_setup_entry(hass, entry, add_entities)
    assert len(registrations) == root_count + 2 * appliance_count
    devices = {}
    for entity, subentry_id in registrations:
        expected_id = getattr(entity, "_appliance_id", None)
        assert subentry_id == expected_id
        identifiers = entity.device_info["identifiers"]
        devices.setdefault(subentry_id, identifiers)
        assert devices[subentry_id] == identifiers
        if subentry_id is not None:
            assert identifiers != {(DOMAIN, entry.entry_id)}
            # HA prefixes entity labels with their appliance device name.
            if isinstance(getattr(entity, "_attr_name", None), str):
                assert not entity.name.startswith(
                    entry.subentries[subentry_id].data[CONF_APPLIANCE_NAME]
                )
            assert entity.device_info["via_device"] == (DOMAIN, entry.entry_id)
            expected_prefix = "entry_appliance_" if platform == "sensor" else "entry_"
            assert entity.unique_id.startswith(f"{expected_prefix}{subentry_id}_")
    if appliance_count:
        assert devices["washer"] != devices["heater"]
    if root_count:
        assert devices[None] == {(DOMAIN, entry.entry_id)}
    if platform == "number":
        assert "entry_washer_cheap_price_threshold" in {
            entity.unique_id for entity, _ in registrations
        }


@pytest.fixture
def lifecycle_registries(hass, entity_registry, device_registry):
    """Real HA registries containing the pre-subentry entity layout."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="entry",
        subentries_data=[
            {
                "subentry_id": aid,
                "subentry_type": "appliance",
                "unique_id": None,
                "title": aid.title(),
                "data": {CONF_APPLIANCE_NAME: aid.title()},
            }
            for aid in ("washer", "heater")
        ],
    )
    entry.add_to_hass(hass)
    root = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name="PV Excess Control",
    )
    return entry, root, entity_registry, device_registry


async def test_registry_migration_preserves_entities_and_customizations(hass, lifecycle_registries):
    from custom_components.pv_excess_control import entity_lifecycle
    from homeassistant.helpers import entity_registry as er

    entry, root, registry, devices = lifecycle_registries
    original = []
    for aid in entry.subentries:
        for platform, suffix in (("sensor", "runtime_today"), ("number", "cheap_price_threshold")):
            unique_id = (
                f"entry_appliance_{aid}_{suffix}"
                if platform == "sensor"
                else f"entry_{aid}_{suffix}"
            )
            entity = registry.async_get_or_create(
                platform,
                DOMAIN,
                unique_id,
                config_entry=entry,
                device_id=root.id,
                suggested_object_id=f"custom_{aid}_{suffix}",
            )
            entity = registry.async_update_entity(
                entity.entity_id,
                name="User name",
                icon="mdi:star",
                disabled_by=er.RegistryEntryDisabler.USER,
            )
            original.append((aid, entity))
    root_entity = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "entry_excess_power",
        config_entry=entry,
        device_id=root.id,
    )

    migrate = entity_lifecycle.async_reconcile_appliance_entities
    migrate(hass, entry)
    assigned_devices = {}
    for aid, before in original:
        after = registry.async_get(before.entity_id)
        assert after.config_subentry_id == aid
        assert after.unique_id == before.unique_id
        assert (after.name, after.icon, after.disabled_by) == (
            before.name,
            before.icon,
            before.disabled_by,
        )
        assert after.device_id != root.id
        assigned_devices[aid] = after.device_id
        device = devices.async_get(after.device_id)
        if hasattr(device, "config_subentry_id"):
            assert device.config_subentry_id == aid
        else:
            assert device.config_entries_subentries[entry.entry_id] == {aid}
        assert device.via_device_id == root.id
    assert assigned_devices["washer"] != assigned_devices["heater"]
    assert registry.async_get(root_entity.entity_id) == root_entity
    snapshot = dict(registry.entities)
    migrate(hass, entry)
    assert dict(registry.entities) == snapshot


async def test_registry_migration_removes_only_known_owned_orphans(hass, lifecycle_registries):
    from custom_components.pv_excess_control import entity_lifecycle
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry, root, registry, devices = lifecycle_registries
    other = MockConfigEntry(domain=DOMAIN, entry_id="other")
    other.add_to_hass(hass)
    stale_device = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "entry_appliance_retired")},
    )
    orphan = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "entry_appliance_retired_runtime_today",
        config_entry=entry,
        device_id=stale_device.id,
    )
    orphan_control = registry.async_get_or_create(
        "switch",
        DOMAIN,
        "entry_retired_enabled",
        config_entry=entry,
        device_id=root.id,
    )
    preserved = []
    for domain, platform, unique_id, owner in [
        ("sensor", DOMAIN, "entry_excess_power", entry),
        ("switch", DOMAIN, "entry_control_enabled", entry),
        ("switch", DOMAIN, "entry_force_charge", entry),
        ("sensor", DOMAIN, "entry_appliance_retired_unknown", entry),
        ("switch", DOMAIN, "entry_retired_priority", entry),
        ("sensor", "unrelated", "entry_appliance_retired_runtime_today", entry),
        ("number", DOMAIN, "entry_retired_priority", other),
        ("sensor", DOMAIN, "A" * 26, entry),
    ]:
        preserved.append(
            registry.async_get_or_create(
                domain,
                platform,
                unique_id,
                config_entry=owner,
            )
        )
    migrate = entity_lifecycle.async_reconcile_appliance_entities
    migrate(hass, entry)
    assert registry.async_get(orphan.entity_id) is None
    assert registry.async_get(orphan_control.entity_id) is None
    assert devices.async_get(stale_device.id) is None
    assert devices.async_get(root.id) == root
    for entity in preserved:
        assert registry.async_get(entity.entity_id) == entity


async def test_fresh_setup_creates_root_before_parallel_platform_setup(
    hass, entity_registry, device_registry
):
    from custom_components.pv_excess_control.entity_lifecycle import (
        async_reconcile_appliance_entities,
    )
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain=DOMAIN, entry_id="fresh")
    entry.add_to_hass(hass)
    async_reconcile_appliance_entities(hass, entry)
    root = device_registry.async_get_device(identifiers={(DOMAIN, "fresh")})
    assert root is not None
    if hasattr(root, "config_subentry_id"):
        assert root.config_subentry_id is None
    else:
        assert root.config_entries_subentries[entry.entry_id] == {None}


@pytest.mark.parametrize(
    "protection", ["foreign_owner", "extra_identifier", "connection", "remaining_entity"]
)
async def test_orphan_cleanup_retains_devices_with_other_uses(
    hass, lifecycle_registries, protection
):
    from custom_components.pv_excess_control.entity_lifecycle import (
        async_reconcile_appliance_entities,
    )
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry, _, registry, devices = lifecycle_registries
    other = MockConfigEntry(domain=DOMAIN, entry_id="other")
    other.add_to_hass(hass)
    identifiers = {(DOMAIN, "entry_appliance_retired")}
    if protection == "extra_identifier":
        identifiers.add((DOMAIN, "keep_me"))
    device = devices.async_get_or_create(
        config_entry_id=other.entry_id if protection == "foreign_owner" else entry.entry_id,
        identifiers=identifiers,
        connections={("mac", "12:34:56:78:90:ab")} if protection == "connection" else set(),
    )
    orphan = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "entry_appliance_retired_runtime_today",
        config_entry=entry,
        device_id=device.id,
    )
    if protection == "remaining_entity":
        registry.async_get_or_create(
            "sensor",
            DOMAIN,
            "unrecognized_entity",
            config_entry=entry,
            device_id=device.id,
        )
    async_reconcile_appliance_entities(hass, entry)
    assert registry.async_get(orphan.entity_id) is None
    assert devices.async_get(device.id) is not None


async def test_removing_subentry_removes_its_entities_without_touching_other_appliance(
    hass, lifecycle_registries
):
    from custom_components.pv_excess_control.entity_lifecycle import (
        async_reconcile_appliance_entities,
    )

    entry, root, registry, devices = lifecycle_registries
    appliance_entities = {
        aid: registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"entry_appliance_{aid}_power",
            config_entry=entry,
            device_id=root.id,
        )
        for aid in entry.subentries
    }
    async_reconcile_appliance_entities(hass, entry)
    washer_device_id = registry.async_get(appliance_entities["washer"].entity_id).device_id
    heater_before = registry.async_get(appliance_entities["heater"].entity_id)
    assert hass.config_entries.async_remove_subentry(entry, "washer")
    async_reconcile_appliance_entities(hass, entry)
    assert registry.async_get(appliance_entities["washer"].entity_id) is None
    assert devices.async_get(washer_device_id) is None
    assert registry.async_get(heater_before.entity_id) == heater_before
    assert devices.async_get(root.id) is not None
