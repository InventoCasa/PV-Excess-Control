"""Optional restart metadata cannot prevent recovery of owned hardware."""
from dataclasses import asdict
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from homeassistant.util import dt as dt_util

from custom_components.pv_excess_control.battery_journal import BatteryOwnershipJournal
from tests.test_battery_control_safety import prepared
from tests.test_battery_journal import charge_config, hold_config
from tests.test_battery_discharge_journal import discharge_config


def save_payload(hass_storage, entry_id, deadline, *, charge=None, hold=None):
    key = f'pv_excess_control.{entry_id}.battery_ownership'
    hass_storage[key] = {'version': 1, 'minor_version': 1, 'key': key,
                         'data': {'charge': asdict(charge) if charge else None,
                                  'hold': asdict(hold) if hold else None,
                                  'charge_retry_after': deadline}}


@pytest.mark.parametrize('raw', [True, 42, 'bad', '2030-01-01T12:00:00', [], {},
                                 '2099-01-01T12:00:00+00:00', '9999-12-31T23:59:59-12:00'])
async def test_invalid_optional_deadline_preserves_valid_cleanup_maps(hass, hass_storage, freezer, raw):
    freezer.move_to('2030-01-01T12:00:00Z')
    charge, hold = charge_config(), hold_config()
    save_payload(hass_storage, 'recovery', raw, charge=charge, hold=hold)
    journal = BatteryOwnershipJournal(hass, 'recovery')
    assert await journal.async_load() == (charge, hold)
    assert journal.loaded_record_exists
    assert journal.charge_retry_after is None
    assert journal.charge_retry_after_invalid


@pytest.mark.parametrize('seconds,invalid', [(-600, False), (3600, False), (3660, False), (3661, True)])
async def test_restored_clock_bound_allows_configured_delay_and_small_skew(hass, hass_storage, freezer, seconds, invalid):
    freezer.move_to('2030-01-01T12:00:00Z')
    deadline = dt_util.utcnow() + timedelta(seconds=seconds)
    save_payload(hass_storage, 'recovery', deadline.isoformat())
    journal = BatteryOwnershipJournal(hass, 'recovery')
    assert await journal.async_load() == (None, None)
    assert journal.charge_retry_after_invalid is invalid
    assert journal.charge_retry_after == (None if invalid else deadline)


async def test_bad_deadline_is_repaired_before_storage_allows_new_starts(
    hass, hass_storage, coordinator_factory, mock_inverter_controller, freezer,
):
    freezer.move_to('2030-01-01T12:00:00Z')
    coord, _ = prepared(coordinator_factory, mock_inverter_controller,
                        grid_charge_engage_min_duration_minutes=8)
    charge = charge_config()
    save_payload(hass_storage, coord.config_entry.entry_id, 'bad', charge=charge)
    coord._battery_journal = BatteryOwnershipJournal(hass, coord.config_entry.entry_id)
    original_save = coord._battery_journal.async_save
    async def check_gate(*args, **kwargs):
        assert not coord._battery_storage_ready
        assert not coord.auto_should_engage_now()
        await original_save(*args, **kwargs)
    coord._battery_journal.async_save = AsyncMock(side_effect=check_gate)
    await coord._restore_battery_ownership()
    assert coord._battery_storage_ready
    assert coord._grid_charge_cleanup_pending
    assert coord._battery_charge_owner.config == charge
    assert coord._battery_retry_after == dt_util.utcnow() + timedelta(minutes=8)
    verified = BatteryOwnershipJournal(hass, coord.config_entry.entry_id)
    assert await verified.async_load() == (charge, None)
    assert verified.charge_retry_after == coord._battery_retry_after
    assert not verified.charge_retry_after_invalid


async def test_failed_deadline_repair_blocks_starts_but_keeps_owned_cleanup_available(
    hass, hass_storage, coordinator_factory, mock_inverter_controller, freezer,
):
    freezer.move_to('2030-01-01T12:00:00Z')
    coord, _ = prepared(coordinator_factory, mock_inverter_controller)
    charge = charge_config()
    save_payload(hass_storage, coord.config_entry.entry_id, 'bad', charge=charge)
    journal = BatteryOwnershipJournal(hass, coord.config_entry.entry_id)
    journal.async_save = AsyncMock(side_effect=OSError('disk unavailable'))
    coord._battery_journal = journal
    await coord._restore_battery_ownership()
    assert not coord._battery_storage_ready
    assert coord._grid_charge_cleanup_pending
    assert coord._battery_charge_owner.config == charge
    coord._battery_charge_owner.disengage = AsyncMock()
    assert await coord.async_stop_battery_controls('disabled')
    coord._battery_charge_owner.disengage.assert_awaited_once()
    assert not coord.auto_should_engage_now()


async def test_bad_optional_deadline_never_weakens_invalid_mapping_rejection(hass, hass_storage, freezer):
    freezer.move_to('2030-01-01T12:00:00Z')
    key = 'pv_excess_control.recovery.battery_ownership'
    save_payload(hass_storage, 'recovery', 'bad')
    hass_storage[key]['data']['charge'] = {'enable_entity_id': 'light.invalid'}
    with pytest.raises(ValueError):
        await BatteryOwnershipJournal(hass, 'recovery').async_load()


async def test_deadline_repair_preserves_shared_discharge_ownership_and_mapping(
    hass, hass_storage, coordinator_factory, mock_inverter_controller, freezer,
):
    freezer.move_to('2030-01-01T12:00:00Z')
    coord, _ = prepared(coordinator_factory, mock_inverter_controller)
    mapping = discharge_config()
    save_payload(hass_storage, coord.config_entry.entry_id, '2099-01-01T00:00:00Z')
    key = f'pv_excess_control.{coord.config_entry.entry_id}.battery_ownership'
    hass_storage[key]['data'].update(discharge=asdict(mapping), discharge_mapping=asdict(mapping))
    coord._battery_journal = BatteryOwnershipJournal(hass, coord.config_entry.entry_id)
    await coord._restore_battery_ownership()
    assert coord._battery_storage_ready
    assert coord._battery_discharge_cleanup_pending
    assert coord._battery_discharge_owner.config == mapping
    restored = BatteryOwnershipJournal(hass, coord.config_entry.entry_id)
    assert await restored.async_load() == (None, None)
    assert restored.discharge_config == mapping
    assert restored.discharge_mapping == mapping
    assert not restored.charge_retry_after_invalid
