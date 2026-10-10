"""Battery ownership survives boot and releases before control bridges stop."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CoreState, HomeAssistant

from tests.test_battery_control_safety import prepared


def real_core(coordinator_factory, mock_inverter_controller, tmp_path, **config):
    coord, states = prepared(coordinator_factory, mock_inverter_controller, **config)
    hass = HomeAssistant(str(tmp_path))
    coord.hass = hass
    coord._persist_battery_controls = MagicMock()
    for entity_id, state in states.items():
        hass.states.async_set(entity_id, state.state, state.attributes)
    return coord, hass


@pytest.mark.parametrize("state", [CoreState.not_running, CoreState.starting])
async def test_restored_charge_waits_for_started_bridge_before_any_release(
    coordinator_factory, mock_inverter_controller, tmp_path, state,
):
    coord, hass = real_core(coordinator_factory, mock_inverter_controller, tmp_path,
                            _grid_charge_engaged=True)
    hass.set_state(state)
    coord._battery_journal.async_load.return_value = (None, None)
    store = MagicMock(async_load=AsyncMock(return_value=None))
    with patch("custom_components.pv_excess_control.battery_control.Store", return_value=store):
        await coord.async_restore_battery_load()
    await coord._battery_preflight()
    await coord._run_grid_charge_state_machine(None, None)
    assert await coord.async_stop_battery_controls("watchdog_retry") is False
    mock_inverter_controller.disengage.assert_not_awaited()
    assert coord._grid_charge_cleanup_pending
    assert coord._battery_recovery
    assert hass.bus.async_listeners()[EVENT_HOMEASSISTANT_STARTED] == 1
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    mock_inverter_controller.disengage.assert_awaited_once()
    assert not coord._grid_charge_cleanup_pending
    assert not coord._battery_recovery


async def test_direct_manual_charge_cannot_start_before_home_assistant_running(
    coordinator_factory, mock_inverter_controller, tmp_path,
):
    coord, hass = real_core(coordinator_factory, mock_inverter_controller, tmp_path)
    hass.set_state(CoreState.starting)
    coord.force_charge = True
    try:
        await coord._run_grid_charge_state_machine(None, None)
        mock_inverter_controller.engage.assert_not_awaited()
        mock_inverter_controller.disengage.assert_not_awaited()
    finally:
        coord._cancel_battery_watchdog()


async def test_failed_startup_unload_still_releases_after_started_without_restarting_charge(
    coordinator_factory, mock_inverter_controller, tmp_path,
):
    coord, hass = real_core(coordinator_factory, mock_inverter_controller, tmp_path,
                            _grid_charge_engaged=True)
    hass.set_state(CoreState.starting)
    coord.force_charge = True
    assert await coord.async_prepare_battery_unload() is False
    mock_inverter_controller.disengage.assert_not_awaited()
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    mock_inverter_controller.disengage.assert_awaited_once()
    assert coord._battery_unloading
    assert not coord._grid_charge_cleanup_pending
    await coord._run_grid_charge_state_machine(None, None)
    mock_inverter_controller.engage.assert_not_awaited()


async def test_hot_reload_cleanup_on_running_core_remains_immediate(
    coordinator_factory, mock_inverter_controller, tmp_path,
):
    coord, hass = real_core(coordinator_factory, mock_inverter_controller, tmp_path,
                            _grid_charge_engaged=True)
    hass.set_state(CoreState.running)
    assert await coord.async_stop_battery_controls("reload") is True
    mock_inverter_controller.disengage.assert_awaited_once()


async def test_stopping_core_does_not_suppress_owned_release(
    coordinator_factory, mock_inverter_controller, tmp_path,
):
    coord, hass = real_core(coordinator_factory, mock_inverter_controller, tmp_path,
                            _grid_charge_engaged=True)
    hass.set_state(CoreState.stopping)
    assert await coord.async_stop_battery_controls("shutdown") is True
    mock_inverter_controller.disengage.assert_awaited_once()


async def test_shutdown_hook_registered_before_refresh_and_awaited_before_stop_event(
    coordinator_factory, mock_inverter_controller, tmp_path, monkeypatch,
):
    from custom_components import pv_excess_control as integration

    coord, hass = real_core(coordinator_factory, mock_inverter_controller, tmp_path,
                            _grid_charge_engaged=True)
    hass.set_state(CoreState.running)
    coord.async_restore_battery_load = AsyncMock()
    coord.async_restore_daily_state = AsyncMock()
    hass.config_entries = SimpleNamespace(async_forward_entry_setups=AsyncMock())
    order = []

    async def first_refresh():
        assert len(hass._shutdown_jobs) == 1
        order.append("refresh")

    async def released():
        assert "stop_event" not in order
        order.append("released")

    coord.async_config_entry_first_refresh = first_refresh
    mock_inverter_controller.disengage.side_effect = released
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, lambda event: order.append("stop_event"))
    monkeypatch.setattr(integration, "PvExcessCoordinator", lambda *args: coord)
    monkeypatch.setattr(integration, "async_reconcile_appliance_entities", lambda *args: None)
    monkeypatch.setattr(integration, "async_track_time_change", MagicMock())
    assert await integration.async_setup_entry(hass, coord.config_entry)
    await hass.async_stop(force=True)
    assert order == ["refresh", "released", "stop_event"]
    assert not coord._grid_charge_cleanup_pending


async def test_successful_unload_cancels_pending_started_callback(
    coordinator_factory, mock_inverter_controller, tmp_path,
):
    coord, hass = real_core(coordinator_factory, mock_inverter_controller, tmp_path,
                            _grid_charge_engaged=True)
    hass.set_state(CoreState.starting)
    assert await coord.async_stop_battery_controls("startup_recovery") is False
    hass.set_state(CoreState.running)
    assert await coord.async_prepare_battery_unload() is True
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    mock_inverter_controller.disengage.assert_awaited_once()
    assert coord._battery_startup_unsub is None


async def test_started_release_failure_retains_ownership_and_retry(
    coordinator_factory, mock_inverter_controller, tmp_path,
):
    coord, hass = real_core(coordinator_factory, mock_inverter_controller, tmp_path,
                            _grid_charge_engaged=True)
    hass.set_state(CoreState.starting)
    await coord.async_stop_battery_controls("startup_recovery")
    mock_inverter_controller.disengage.side_effect = RuntimeError("feedback unavailable")
    hass.set_state(CoreState.running)
    try:
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()
        assert coord._battery_recovery
        assert coord._grid_charge_cleanup_pending
        assert coord._battery_watchdog is not None
        assert coord._battery_control_reason == "release_unconfirmed"
        coord._battery_journal.async_save.assert_awaited()
    finally:
        coord._cancel_battery_watchdog()


async def test_automatic_charge_intent_is_false_before_core_running(
    coordinator_factory, mock_inverter_controller, tmp_path,
):
    coord, hass = real_core(coordinator_factory, mock_inverter_controller, tmp_path,
                            auto_battery_grid_charge=True, allow_grid_charging=True)
    hass.set_state(CoreState.starting)
    coord._current_battery_slot = lambda: SimpleNamespace(action="charge")
    assert not coord.auto_should_engage_now()


async def test_setup_cleanup_callbacks_unregister_shutdown_job(
    coordinator_factory, mock_inverter_controller, tmp_path, monkeypatch,
):
    from custom_components import pv_excess_control as integration

    coord, hass = real_core(coordinator_factory, mock_inverter_controller, tmp_path)
    hass.set_state(CoreState.running)
    coord.async_restore_battery_load = AsyncMock()
    coord.async_restore_daily_state = AsyncMock()
    coord.async_config_entry_first_refresh = AsyncMock()
    hass.config_entries = SimpleNamespace(async_forward_entry_setups=AsyncMock())
    monkeypatch.setattr(integration, "PvExcessCoordinator", lambda *args: coord)
    monkeypatch.setattr(integration, "async_reconcile_appliance_entities", lambda *args: None)
    monkeypatch.setattr(integration, "async_track_time_change", MagicMock())
    assert await integration.async_setup_entry(hass, coord.config_entry)
    assert len(hass._shutdown_jobs) == 1
    for call in coord.config_entry.async_on_unload.call_args_list:
        call.args[0]()
    assert hass._shutdown_jobs == []
