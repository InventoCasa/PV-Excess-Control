"""Durable ownership records retain actuator mappings until confirmed release."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.pv_excess_control.inverter_control import InverterGridChargeController
from custom_components.pv_excess_control.models import InverterGridChargeConfig


def charge_config():
    return InverterGridChargeConfig(
        enable_entity_id="input_select.charge",
        enable_engage_value="Forced charge",
        enable_disengage_value="Stop",
        mode_entity_id="input_select.mode",
        mode_engage_value="Forced",
        mode_disengage_value="Self",
        power_entity_id="input_number.power",
        enable_feedback_entity_id="sensor.charge",
        mode_feedback_entity_id="sensor.mode",
        power_feedback_entity_id="sensor.power",
    )


def hold_config():
    return InverterGridChargeConfig(
        "switch.hold", "on", "off",
        enable_feedback_entity_id="sensor.hold_status",
        enable_feedback_engage_value="Blocked",
        enable_feedback_disengage_value="Allowed",
    )


def journal(hass, entry_id="entry"):
    from custom_components.pv_excess_control.battery_journal import BatteryOwnershipJournal
    return BatteryOwnershipJournal(hass, entry_id)


async def test_missing_journal_has_no_owned_controls(hass):
    assert await journal(hass).async_load() == (None, None)


async def test_storage_roundtrip_preserves_both_original_actuator_mappings(hass, hass_storage):
    charge, hold = charge_config(), hold_config()
    await journal(hass).async_save(charge, hold)
    # A new object models restart without depending on current configuration.
    assert await journal(hass).async_load() == (charge, hold)
    saved = hass_storage["pv_excess_control.entry.battery_ownership"]
    assert saved["version"] == 1
    assert saved["data"] == {"charge": asdict(charge), "hold": asdict(hold)}


async def test_clearing_one_ownership_preserves_the_other(hass):
    await journal(hass).async_save(charge_config(), hold_config())
    await journal(hass).async_save(None, hold_config())
    assert await journal(hass).async_load() == (None, hold_config())
    await journal(hass).async_save(None, None)
    assert await journal(hass).async_load() == (None, None)


async def test_save_waits_for_storage_completion(hass, monkeypatch, tmp_path):
    from custom_components.pv_excess_control import battery_journal
    entered, finish = asyncio.Event(), asyncio.Event()
    saved = []

    async def save(data):
        entered.set()
        await finish.wait()
        saved.append(data)

    store = MagicMock(
        path=str(tmp_path / "battery_ownership"),
        async_save=AsyncMock(side_effect=save),
        async_load=AsyncMock(side_effect=lambda: saved[-1]),
    )
    monkeypatch.setattr(battery_journal, "Store", lambda *args: store)
    task = asyncio.create_task(journal(hass).async_save(charge_config(), None))
    await entered.wait()
    assert not task.done()
    assert saved == []
    finish.set()
    await task
    assert saved == [{"charge": asdict(charge_config()), "hold": None}]


async def test_storage_failure_propagates_before_commands_can_follow(hass, monkeypatch):
    from custom_components.pv_excess_control import battery_journal
    store = MagicMock(async_save=AsyncMock(side_effect=OSError("disk unavailable")))
    monkeypatch.setattr(battery_journal, "Store", lambda *args: store)
    with pytest.raises(OSError, match="disk unavailable"):
        await journal(hass).async_save(charge_config(), None)


async def test_load_failure_propagates_without_clearing_storage(hass, monkeypatch, tmp_path):
    from custom_components.pv_excess_control import battery_journal
    store = MagicMock(
        path=str(tmp_path / "missing"),
        async_load=AsyncMock(side_effect=OSError("disk unavailable")),
    )
    monkeypatch.setattr(battery_journal, "Store", lambda *args: store)
    with pytest.raises(OSError, match="disk unavailable"):
        await journal(hass).async_load()
    store.async_save.assert_not_called()


@pytest.mark.parametrize("payload", [
    {}, [], {"charge": None}, {"charge": None, "hold": None, "unknown": {}},
    {"charge": "bad", "hold": None},
    {"charge": None, "hold": {"enable_entity_id": "switch.hold"}},
    {"charge": {**asdict(charge_config()), "enable_entity_id": "light.kitchen"}, "hold": None},
    {"charge": {**asdict(charge_config()), "mode_entity_id": "sensor.mode"}, "hold": None},
    {"charge": {**asdict(charge_config()), "power_entity_id": "switch.power"}, "hold": None},
    {"charge": {**asdict(charge_config()), "enable_feedback_entity_id": "invalid"}, "hold": None},
    {"charge": {**asdict(charge_config()), "timeout_seconds": float("nan")}, "hold": None},
    {"charge": {**asdict(charge_config()), "timeout_seconds": True}, "hold": None},
    {"charge": {**asdict(charge_config()), "power_tolerance_w": -1}, "hold": None},
    {"charge": {**asdict(charge_config()), "mode_disengage_value": None}, "hold": None},
    {"charge": {**asdict(charge_config()), "enable_disengage_value": "Forced charge"}, "hold": None},
    {"charge": {**asdict(charge_config()), "extra_field": 1}, "hold": None},
    {"charge": None, "hold": asdict(InverterGridChargeConfig("switch.hold", "Blocked", "Allowed"))},
    {"charge": None, "hold": asdict(InverterGridChargeConfig("switch.hold", "false", "off"))},
])
async def test_malformed_ownership_fails_closed_without_erasing_record(hass, hass_storage, payload):
    key = "pv_excess_control.entry.battery_ownership"
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": payload}
    with pytest.raises(ValueError):
        await journal(hass).async_load()
    assert hass_storage[key]["data"] is payload


async def test_invalid_save_does_not_replace_last_known_ownership(hass):
    await journal(hass).async_save(charge_config(), None)
    bad = InverterGridChargeConfig("light.kitchen", "on", "off")
    with pytest.raises(ValueError):
        await journal(hass).async_save(bad, None)
    assert await journal(hass).async_load() == (charge_config(), None)


def test_controller_exposes_immutable_configuration():
    config = charge_config()
    controller = InverterGridChargeController(MagicMock(), config)
    assert controller.config == config
    with pytest.raises(AttributeError):
        controller.config = hold_config()


async def test_swallowed_storage_write_error_cannot_confirm_new_ownership(hass, monkeypatch, tmp_path):
    from custom_components.pv_excess_control import battery_journal
    store = MagicMock(
        path=str(tmp_path / "battery_ownership"),
        async_save=AsyncMock(return_value=None),
        async_load=AsyncMock(return_value={"charge": None, "hold": None}),
    )
    monkeypatch.setattr(battery_journal, "Store", lambda *args: store)
    with pytest.raises(OSError, match="persist"):
        await journal(hass).async_save(charge_config(), None)


async def test_preexisting_but_unreadable_journal_is_not_treated_as_absent(hass, monkeypatch, tmp_path):
    from custom_components.pv_excess_control import battery_journal
    path = tmp_path / "battery_ownership"
    path.write_text('{"version":1,"data":{"charge":null,"hold":null}}')
    store = MagicMock(path=str(path), async_load=AsyncMock(return_value=None))
    monkeypatch.setattr(battery_journal, "Store", lambda *args: store)
    with pytest.raises(ValueError, match="unreadable"):
        await journal(hass).async_load()
    store.async_save.assert_not_called()


async def test_explicitly_cleared_record_is_distinct_from_missing_legacy_journal(hass):
    first = journal(hass)
    assert first.loaded_record_exists is False
    assert await first.async_load() == (None, None)
    assert first.loaded_record_exists is False
    await first.async_save(None, None)
    restored = journal(hass)
    assert restored.loaded_record_exists is False
    assert await restored.async_load() == (None, None)
    assert restored.loaded_record_exists is True


async def test_malformed_json_stays_at_original_path_across_repeated_loads(hass, monkeypatch, tmp_path):
    from custom_components.pv_excess_control import battery_journal
    path = tmp_path / "battery_ownership"
    corrupt = '{"version": 1, "data": {"charge": '
    path.write_text(corrupt)

    async def load_like_ha():
        # HA preserves corrupt bytes by renaming the record, then returns None.
        # Recovery must reject syntax before that would hide future ownership.
        if path.exists():
            path.rename(path.with_suffix(".corrupt"))
        return None

    store = MagicMock(path=str(path), async_load=AsyncMock(side_effect=load_like_ha))
    monkeypatch.setattr(battery_journal, "Store", lambda *args: store)
    for _ in range(2):
        with pytest.raises(ValueError):
            await journal(hass).async_load()
        assert path.is_file()
        assert path.read_text() == corrupt
    store.async_load.assert_not_called()


async def test_save_readback_does_not_rename_a_corrupt_ownership_record(hass, monkeypatch, tmp_path):
    from custom_components.pv_excess_control import battery_journal
    path = tmp_path / "battery_ownership"

    async def save_corrupt(_):
        path.write_text('{"data":')

    async def load_like_ha():
        path.rename(path.with_suffix(".corrupt"))
        return None

    store = MagicMock(
        path=str(path), async_save=AsyncMock(side_effect=save_corrupt),
        async_load=AsyncMock(side_effect=load_like_ha),
    )
    monkeypatch.setattr(battery_journal, "Store", lambda *args: store)
    with pytest.raises((ValueError, OSError)):
        await journal(hass).async_save(charge_config(), None)
    assert path.is_file()
    assert path.read_text() == '{"data":'
    store.async_load.assert_not_called()
