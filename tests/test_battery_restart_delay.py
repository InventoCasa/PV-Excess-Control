"""Confirmed charge stops enforce one durable restart deadline on every path."""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import State
from homeassistant.util import dt as dt_util

from custom_components.pv_excess_control.battery_journal import BatteryOwnershipJournal
from tests.test_battery_control_safety import entity, planned, prepared
from custom_components.pv_excess_control.switch import ForceChargeSwitch


@pytest.fixture(autouse=True)
def cleanup_watchdogs(monkeypatch):
    from custom_components.pv_excess_control.battery_control import BatteryControlMixin
    original = BatteryControlMixin._init_battery_control
    coordinators = []
    def initialize(self):
        original(self)
        if self not in coordinators:
            coordinators.append(self)
    monkeypatch.setattr(BatteryControlMixin, "_init_battery_control", initialize)
    yield
    for coord in coordinators:
        coord._cancel_battery_watchdog()


@pytest.mark.parametrize("reason", ["watchdog_expired", "planning_input_changed", "unload", "disabled"])
async def test_every_confirmed_owned_stop_starts_restart_delay(
    coordinator_factory, mock_inverter_controller, freezer, reason,
):
    freezer.move_to("2030-01-01T12:00:00Z")
    coord, _ = prepared(coordinator_factory, mock_inverter_controller)
    await entity(ForceChargeSwitch, coord).async_turn_on()
    freezer.tick(20)
    assert await coord.async_stop_battery_controls(reason)
    assert coord._battery_retry_after == dt_util.utcnow() + timedelta(minutes=5)
    assert coord._battery_journal.async_save.call_args.kwargs["charge_retry_after"] == coord._battery_retry_after


async def test_restart_deadline_starts_after_hardware_confirmation(
    coordinator_factory, mock_inverter_controller, freezer,
):
    freezer.move_to("2030-01-01T12:00:00Z")
    coord, _ = prepared(coordinator_factory, mock_inverter_controller)
    await entity(ForceChargeSwitch, coord).async_turn_on()
    async def confirm():
        assert coord._battery_retry_after is None
        freezer.tick(25)
    mock_inverter_controller.disengage.side_effect = confirm
    assert await coord.async_stop_battery_controls()
    assert coord._battery_retry_after == dt_util.utcnow() + timedelta(minutes=5)


async def test_watchdog_stop_blocks_immediate_economic_restart(
    coordinator_factory, mock_inverter_controller, freezer,
):
    freezer.move_to("2030-01-01T12:00:00Z")
    coord, _, tariff = planned(coordinator_factory, mock_inverter_controller)
    await coord._run_grid_charge_state_machine(tariff, None)
    callback = coord._battery_watchdog._callback
    coord._battery_watchdog.cancel()
    callback()
    await asyncio.sleep(0)
    await coord._run_grid_charge_state_machine(tariff, None)
    assert not coord._grid_charge_engaged
    assert coord._battery_control_reason == "restart_delay"
    assert coord.auto_should_engage_now() is False
    mock_inverter_controller.engage.assert_awaited_once()
    coord._cancel_battery_watchdog()


async def test_missing_input_stop_preserves_restart_delay_after_input_recovers(
    coordinator_factory, mock_inverter_controller, freezer,
):
    freezer.move_to("2030-01-01T12:00:00Z")
    coord, states, tariff = planned(coordinator_factory, mock_inverter_controller)
    await coord._run_grid_charge_state_machine(tariff, None)
    original = states["sensor.load_power"]
    states["sensor.load_power"] = State("sensor.load_power", "unavailable")
    await coord._run_grid_charge_state_machine(tariff, None)
    assert not coord._grid_charge_engaged
    states["sensor.load_power"] = original
    await coord._run_grid_charge_state_machine(tariff, None)
    assert coord._battery_control_reason == "restart_delay"
    mock_inverter_controller.engage.assert_awaited_once()
    coord._cancel_battery_watchdog()


async def test_failed_stop_does_not_start_delay_until_confirmed(
    coordinator_factory, mock_inverter_controller, freezer,
):
    freezer.move_to("2030-01-01T12:00:00Z")
    coord, _ = prepared(coordinator_factory, mock_inverter_controller)
    await entity(ForceChargeSwitch, coord).async_turn_on()
    mock_inverter_controller.disengage.side_effect = RuntimeError("not confirmed")
    assert not await coord.async_stop_battery_controls()
    assert coord._battery_retry_after is None
    freezer.tick(15)
    mock_inverter_controller.disengage.side_effect = None
    assert await coord.async_stop_battery_controls()
    assert coord._battery_retry_after == dt_util.utcnow() + timedelta(minutes=5)


@pytest.mark.parametrize("minutes", [0, 2, 8])
async def test_configured_restart_delay_and_existing_longer_backoff_are_preserved(
    coordinator_factory, mock_inverter_controller, freezer, minutes,
):
    freezer.move_to("2030-01-01T12:00:00Z")
    coord, _ = prepared(coordinator_factory, mock_inverter_controller,
                        grid_charge_engage_min_duration_minutes=minutes)
    await entity(ForceChargeSwitch, coord).async_turn_on()
    previous = dt_util.utcnow() + timedelta(minutes=6)
    coord._battery_retry_after = previous
    coord._battery_plan_key = ("old-plan",)
    assert await coord.async_stop_battery_controls()
    assert coord._battery_retry_after == max(previous, dt_util.utcnow() + timedelta(minutes=minutes))
    assert coord._battery_plan_key is None


async def test_unowned_or_hold_only_stop_does_not_create_or_extend_delay(
    coordinator_factory, mock_inverter_controller, freezer,
):
    freezer.move_to("2030-01-01T12:00:00Z")
    coord, _ = prepared(coordinator_factory, mock_inverter_controller)
    assert await coord.async_stop_battery_controls()
    assert coord._battery_retry_after is None
    previous = dt_util.utcnow() + timedelta(minutes=5)
    coord._battery_retry_after = previous
    coord._battery_hold_cleanup_pending = True
    coord._battery_hold_owner = MagicMock(disengage=AsyncMock())
    freezer.tick(20)
    assert await coord.async_stop_battery_controls()
    assert coord._battery_retry_after == previous


async def test_failed_journal_clear_does_not_extend_already_confirmed_stop(
    coordinator_factory, mock_inverter_controller, freezer,
):
    freezer.move_to("2030-01-01T12:00:00Z")
    coord, _ = prepared(coordinator_factory, mock_inverter_controller)
    await entity(ForceChargeSwitch, coord).async_turn_on()
    coord._battery_journal.async_save.side_effect = OSError("disk unavailable")
    assert not await coord.async_stop_battery_controls()
    deadline = coord._battery_retry_after
    assert deadline == dt_util.utcnow() + timedelta(minutes=5)
    freezer.tick(25)
    coord._battery_journal.async_save.side_effect = None
    assert await coord.async_stop_battery_controls()
    assert coord._battery_retry_after == deadline


async def test_confirmed_stop_invalidates_cached_plan(
    coordinator_factory, mock_inverter_controller,
):
    coord, _ = prepared(coordinator_factory, mock_inverter_controller)
    await entity(ForceChargeSwitch, coord).async_turn_on()
    coord._battery_plan_key = ("old-plan",)
    coord._battery_grid_plan = object()
    assert await coord.async_stop_battery_controls()
    assert coord._battery_plan_key is None
    assert coord._battery_grid_plan is None


async def test_journal_deadline_survives_restart_without_ownership(
    hass, coordinator_factory, mock_inverter_controller, freezer,
):
    freezer.move_to("2030-01-01T12:00:00Z")
    coord, _ = prepared(coordinator_factory, mock_inverter_controller)
    deadline = dt_util.utcnow() + timedelta(minutes=4)
    journal = BatteryOwnershipJournal(hass, coord.config_entry.entry_id)
    await journal.async_save(None, None, charge_retry_after=deadline)
    coord._battery_journal = BatteryOwnershipJournal(hass, coord.config_entry.entry_id)
    await coord._restore_battery_ownership()
    assert coord._battery_retry_after == deadline
    assert coord._battery_storage_ready
    assert not coord._battery_recovery
    assert coord.battery_control_diagnostics()["battery_restart_available_at"] == deadline.isoformat()
    freezer.tick(241)
    assert coord.battery_control_diagnostics()["battery_restart_available_at"] is None


@pytest.mark.parametrize("raw", [True, 42, "bad", "2030-01-01T12:00:00", [], {}])
async def test_invalid_persisted_deadline_requires_repair_before_starts(hass, hass_storage, raw):
    key = "pv_excess_control.restart.battery_ownership"
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key,
                         "data": {"charge": None, "hold": None, "charge_retry_after": raw}}
    journal = BatteryOwnershipJournal(hass, "restart")
    assert await journal.async_load() == (None, None)
    assert journal.charge_retry_after_invalid


async def test_journal_normalizes_aware_deadline_and_reads_legacy_record(hass, freezer):
    freezer.move_to("2030-01-01T11:55:00Z")
    journal = BatteryOwnershipJournal(hass, "restart")
    local = datetime(2030, 1, 1, 14, tzinfo=timezone(timedelta(hours=2)))
    await journal.async_save(None, None, charge_retry_after=local)
    restored = BatteryOwnershipJournal(hass, "restart")
    assert await restored.async_load() == (None, None)
    assert restored.charge_retry_after == local.astimezone(timezone.utc)
    await journal.async_save(None, None)
    restored = BatteryOwnershipJournal(hass, "restart")
    assert await restored.async_load() == (None, None)
    assert restored.charge_retry_after is None


@pytest.mark.parametrize("minutes", [True, -1, float("nan"), 1e300])
async def test_malformed_delay_cannot_prevent_confirmed_cleanup(
    coordinator_factory, mock_inverter_controller, freezer, minutes,
):
    freezer.move_to("2030-01-01T12:00:00Z")
    coord, _ = prepared(coordinator_factory, mock_inverter_controller,
                        grid_charge_engage_min_duration_minutes=minutes)
    await entity(ForceChargeSwitch, coord).async_turn_on()
    assert await coord.async_stop_battery_controls()
    assert not coord._grid_charge_cleanup_pending
    assert coord._battery_retry_after == dt_util.utcnow() + timedelta(minutes=5)


async def test_confirmed_stop_deadline_survives_real_journal_reload_and_expires(
    hass, coordinator_factory, mock_inverter_controller, freezer,
):
    from tests.test_battery_journal import charge_config
    freezer.move_to("2030-01-01T12:00:00Z")
    coord, _ = prepared(coordinator_factory, mock_inverter_controller)
    mock_inverter_controller.config = charge_config()
    coord._battery_journal = BatteryOwnershipJournal(hass, coord.config_entry.entry_id)
    await entity(ForceChargeSwitch, coord).async_turn_on()
    assert await coord.async_stop_battery_controls("unload")
    deadline = coord._battery_retry_after
    replacement, states = prepared(coordinator_factory, mock_inverter_controller)
    replacement._battery_journal = BatteryOwnershipJournal(hass, coord.config_entry.entry_id)
    await replacement._restore_battery_ownership()
    replacement.force_charge = True
    await replacement._run_grid_charge_state_machine(None, None)
    assert not replacement._grid_charge_engaged
    assert replacement._battery_retry_after == deadline
    mock_inverter_controller.engage.assert_awaited_once()
    freezer.move_to(deadline)
    states["sensor.soc"] = State("sensor.soc", "40")
    await replacement._run_grid_charge_state_machine(None, None)
    assert replacement._grid_charge_engaged
    assert mock_inverter_controller.engage.await_count == 2
    freezer.tick(10)
    assert await replacement.async_stop_battery_controls()
    assert replacement._battery_retry_after == dt_util.utcnow() + timedelta(minutes=5)
