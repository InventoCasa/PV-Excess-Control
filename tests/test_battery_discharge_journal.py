"""Shared numeric discharge ownership survives restart alongside other controls."""
from dataclasses import asdict, replace

import pytest

from custom_components.pv_excess_control.models import InverterGridChargeConfig
from tests.test_battery_journal import charge_config, hold_config, journal


def discharge_config():
    return InverterGridChargeConfig("input_number.discharge", "10", "5000",
                                   enable_feedback_entity_id="sensor.discharge_limit")


async def test_numeric_discharge_roundtrip_preserves_all_three_owners(hass, hass_storage):
    discharge = discharge_config()
    await journal(hass).async_save(charge_config(), hold_config(), discharge_config=discharge)
    restored = journal(hass)
    assert await restored.async_load() == (charge_config(), hold_config())
    assert restored.discharge_config == discharge
    assert hass_storage["pv_excess_control.entry.battery_ownership"]["data"]["discharge"] == asdict(discharge)


async def test_legacy_record_has_no_numeric_discharge_owner(hass):
    await journal(hass).async_save(charge_config(), None)
    restored = journal(hass)
    assert await restored.async_load() == (charge_config(), None)
    assert restored.discharge_config is None


async def test_clearing_discharge_owner_preserves_owned_charge(hass):
    await journal(hass).async_save(charge_config(), None, discharge_config=discharge_config())
    await journal(hass).async_save(charge_config(), None)
    restored = journal(hass)
    assert await restored.async_load() == (charge_config(), None)
    assert restored.discharge_config is None


@pytest.mark.parametrize("discharge", [
    {}, "invalid", asdict(hold_config()),
    {**asdict(discharge_config()), "enable_disengage_value": "NaN"},
])
async def test_malformed_discharge_record_blocks_recovery_without_erasing(hass, hass_storage, discharge):
    key = "pv_excess_control.entry.battery_ownership"
    payload = {"charge": None, "hold": None, "discharge": discharge}
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": payload}
    with pytest.raises(ValueError):
        await journal(hass).async_load()
    assert hass_storage[key]["data"] is payload


async def test_verified_mapping_survives_clearing_active_discharge_owner(hass):
    mapping = discharge_config()
    await journal(hass).async_save(None, None, discharge_config=mapping, discharge_mapping=mapping)
    await journal(hass).async_save(None, None, discharge_mapping=mapping)
    restored = journal(hass)
    assert await restored.async_load() == (None, None)
    assert restored.discharge_config is None
    assert restored.discharge_mapping == mapping


async def test_native_number_without_separate_feedback_survives_recovery(hass):
    mapping = InverterGridChargeConfig("number.native_discharge", ".01", "5")
    await journal(hass).async_save(None, None, discharge_config=mapping, discharge_mapping=mapping)
    restored = journal(hass)
    assert await restored.async_load() == (None, None)
    assert restored.discharge_config == mapping
    assert restored.discharge_mapping == mapping


@pytest.mark.parametrize("feedback", [None, "input_number.discharge", "input_number.feedback_helper"])
async def test_discharge_helper_still_requires_separate_physical_feedback(hass, feedback):
    mapping = replace(discharge_config(), enable_feedback_entity_id=feedback)
    with pytest.raises(ValueError):
        await journal(hass).async_save(None, None, discharge_config=mapping)
