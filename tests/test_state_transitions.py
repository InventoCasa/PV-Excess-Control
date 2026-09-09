"""Behavior regressions for config edits and explicit control transitions."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.test_daily_state import make_coord
from tests.test_optimizer import (
    _make_appliance,
    _make_state,
    _make_power,
    _make_tariff,
    _empty_plan,
)
from custom_components.pv_excess_control.const import DOMAIN
from custom_components.pv_excess_control.optimizer import Optimizer
from custom_components.pv_excess_control import _async_update_listener
from custom_components.pv_excess_control.switch import ApplianceOverrideSwitch


@pytest.mark.asyncio
async def test_options_edit_updates_cached_runtime_priority_without_reload():
    coord = make_coord()
    coord.config_entry.subentries["app_1"].data.update(appliance_priority=100, min_daily_runtime=90)
    coord.appliance_priorities["app_1"] = 500
    coord.appliance_min_daily_runtime["app_1"] = 30
    coord.appliance_max_daily_runtime["app_1"] = 120
    coord.current_plan = _empty_plan()
    eid = coord.config_entry.entry_id
    coord.hass.data[DOMAIN] = {
        eid: coord,
        f"{eid}_config_snapshot": dict(coord.config_entry.data),
        f"{eid}_subentry_count": 1,
        f"{eid}_subentries_snapshot": {"app_1": {"appliance_priority": 500}},
    }
    coord.hass.config_entries.async_reload = AsyncMock()
    await _async_update_listener(coord.hass, coord.config_entry)
    cfg = coord._get_appliance_configs()[0]
    assert cfg.priority == 100
    assert cfg.min_daily_runtime == timedelta(minutes=90)
    assert cfg.max_daily_runtime is None
    assert coord.current_plan is None
    coord.hass.config_entries.async_reload.assert_not_awaited()


@pytest.mark.asyncio
async def test_replacing_subentry_with_same_count_reloads():
    coord = make_coord()
    eid = coord.config_entry.entry_id
    coord.hass.data[DOMAIN] = {
        eid: coord,
        f"{eid}_config_snapshot": dict(coord.config_entry.data),
        f"{eid}_subentry_count": 1,
        f"{eid}_subentries_snapshot": {"old_id": {}},
    }
    coord.hass.config_entries.async_reload = AsyncMock()
    await _async_update_listener(coord.hass, coord.config_entry)
    coord.hass.config_entries.async_reload.assert_awaited_once_with(eid)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_ending_override_respects_disabled_appliance(enabled):
    coord = make_coord(disabled=not enabled)
    coord.appliance_overrides["app_1"] = True
    switch = ApplianceOverrideSwitch(coord, "app_1", "Pump")
    switch.hass = coord.hass
    switch.async_write_ha_state = MagicMock()
    await switch.async_turn_off()
    assert not coord.appliance_overrides["app_1"]
    calls = coord.hass.services.calls
    assert bool(calls) is (not enabled)
    if not enabled:
        assert calls[0] == ("switch", "turn_off", {"entity_id": "switch.app_1"})


@pytest.mark.asyncio
async def test_failed_override_shutdown_remains_pending_and_retries():
    coord = make_coord(disabled=True)
    coord.appliance_overrides["app_1"] = True
    coord.hass.services.async_call = AsyncMock(side_effect=[RuntimeError("offline"), None])
    switch = ApplianceOverrideSwitch(coord, "app_1", "Pump")
    switch.hass = coord.hass
    switch.async_write_ha_state = MagicMock()
    await switch.async_turn_off()
    assert "app_1" in coord._pending_stop_appliances
    await coord._retry_pending_stops()
    assert coord.hass.services.async_call.await_count == 2


@pytest.mark.asyncio
async def test_pause_preserves_physical_state_and_statistics():
    from custom_components.pv_excess_control.switch import AppliancePausedSwitch

    coord = make_coord()
    coord.appliance_paused = {}
    coord._get_appliance_states(coord._get_appliance_configs())
    state = coord.appliance_states["app_1"]
    state.runtime_today = timedelta(hours=2)
    switch = AppliancePausedSwitch(coord, "app_1", "Pump")
    switch.hass = coord.hass
    switch.async_write_ha_state = MagicMock()
    await switch.async_turn_on()
    assert coord.hass.services.calls == []
    assert coord._get_appliance_configs() == []
    assert coord._get_appliance_states([])["app_1"].runtime_today >= timedelta(hours=2)
    await switch.async_turn_off()
    assert len(coord._get_appliance_configs()) == 1


def test_unmet_daily_minimum_reports_actual_shed_blocker():
    power = _make_power(-1500)
    app = _make_appliance(nominal_power=700, min_daily_runtime=timedelta(hours=8))
    result = Optimizer().optimize(
        power_state=power,
        appliances=[app],
        appliance_states=[_make_state(is_on=True, current_power=700)],
        power_history=[power] * 3,
        plan=_empty_plan(),
        tariff=_make_tariff(),
    )
    decision = result.decisions[0]
    assert decision.action == "on"
    assert "shed imminent" not in decision.reason.lower()
    assert "daily runtime" in decision.reason.lower()


@pytest.mark.asyncio
async def test_number_uses_synchronous_ha_subentry_api(caplog):
    from custom_components.pv_excess_control.number import ApplianceMinDailyRuntimeNumber

    coord = make_coord()
    coord.hass.config_entries.async_update_subentry = MagicMock(return_value=True)
    num = ApplianceMinDailyRuntimeNumber(coord, "app_1", "Pump")
    num.hass = coord.hass
    num.async_write_ha_state = MagicMock()
    await num.async_set_native_value(45)
    coord.hass.config_entries.async_update_subentry.assert_called_once()
    assert not any("persist" in r.message.lower() for r in caplog.records)


@pytest.mark.parametrize(
    "data,expected",
    [
        ({"pending_stop_appliances": ["sub123"]}, "Shutdown pending"),
        ({"paused_appliances": {"sub123": True}}, "Paused"),
        ({"appliance_enabled": {"sub123": False}}, "Disabled"),
    ],
)
def test_inactive_status_is_explicit(data, expected):
    from tests.test_status_sensor import _make_coordinator_with_data
    from custom_components.pv_excess_control.sensor import PvApplianceStatusSensor

    sensor = PvApplianceStatusSensor(_make_coordinator_with_data(data), "sub123", "Pump")
    assert expected in sensor.native_value


async def test_disabling_device_cancels_override_so_shutdown_cannot_be_undone():
    from custom_components.pv_excess_control.switch import ApplianceEnabledSwitch

    coord = make_coord()
    coord.appliance_overrides["app_1"] = True
    switch = ApplianceEnabledSwitch(coord, "app_1", "Pump")
    switch.hass = coord.hass
    switch.async_write_ha_state = MagicMock()
    await switch.async_turn_off()
    assert coord.appliance_overrides["app_1"] is False
    assert coord._get_appliance_configs() == []
    assert "app_1" in coord._pending_stop_appliances


async def test_resume_cancels_later_stop_while_another_stop_is_awaited():
    coord = make_coord()
    coord._pending_stop_appliances = {"first", "second"}
    stopped = []

    async def stop(appliance_id):
        stopped.append(appliance_id)
        # While I/O for this appliance runs, the user resumes the other one.
        other = ({"first", "second"} - {appliance_id}).pop()
        coord.cancel_pending_stop(other)

    coord.async_stop_appliance = stop
    await coord._retry_pending_stops()
    assert len(stopped) == 1


async def test_runtime_option_edit_keeps_current_command_dedup():
    coord = make_coord()
    eid = coord.config_entry.entry_id
    before = dict(coord.config_entry.subentries["app_1"].data)
    coord.hass.data[DOMAIN] = {
        eid: coord,
        f"{eid}_config_snapshot": dict(coord.config_entry.data),
        f"{eid}_subentry_count": 1,
        f"{eid}_subentries_snapshot": {"app_1": before},
    }
    coord._last_applied_current = {"app_1": 16.0}
    coord.config_entry.subentries["app_1"].data["min_daily_runtime"] = 120
    await _async_update_listener(coord.hass, coord.config_entry)
    assert coord._last_applied_current == {"app_1": 16.0}
