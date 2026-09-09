"""Regression coverage for entry-scoped daily counters (#13/#51)."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.test_init import _make_coordinator, _make_config_entry, MockState
from homeassistant.util import dt as dt_util


def make_coord(data=None, disabled=False):
    sub = MagicMock()
    sub.data = {"appliance_entity": "switch.app_1", "appliance_name": "Pump", "nominal_power": 1000}
    entry = _make_config_entry(subentries={"app_1": sub})
    coord = _make_coordinator(entry=entry, states={"switch.app_1": MockState("on")})
    coord.appliance_enabled = {"app_1": not disabled}

    def update_entry(entry, *, data):
        entry.data = data

    coord.hass.config_entries.async_update_entry = update_entry
    coord._daily_state_store = MagicMock()
    coord._daily_state_store.async_load = AsyncMock(return_value=data)
    coord._daily_state_store.async_save = AsyncMock()
    return coord


def payload(day=None):
    return {
        "date": day or dt_util.now().date().isoformat(),
        "appliances": {
            "app_1": {"runtime_seconds": 3600, "energy_kwh": 2.0, "activations": 3},
            "removed": {"runtime_seconds": 500, "energy_kwh": 1, "activations": 1},
        },
        "analytics": {
            "solar_consumed_kwh": 2,
            "solar_produced_kwh": 10,
            "grid_export_kwh": 3,
            "savings": 0.4,
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled", [False, True])
async def test_restore_before_refresh_preserves_disabled_counters_without_downtime(disabled):
    coord = make_coord(payload(), disabled)
    await coord.async_restore_daily_state()
    states = coord._get_appliance_states(coord._get_appliance_configs())
    assert states["app_1"].runtime_today == timedelta(hours=1)
    assert states["app_1"].energy_today == 2
    assert states["app_1"].activations_today == 3
    assert states["app_1"].is_on
    assert "removed" not in states
    assert coord.analytics.savings_today == 0.4
    assert coord.analytics.self_consumption_ratio == 20
    coord.appliance_enabled["app_1"] = True
    after = coord._get_appliance_states(coord._get_appliance_configs())["app_1"]
    assert after.runtime_today >= timedelta(hours=1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [payload("2000-01-01"), [], {"date": dt_util.now().date().isoformat(), "appliances": []}],
)
async def test_stale_or_malformed_store_does_not_restore(data):
    coord = make_coord(data)
    await coord.async_restore_daily_state()
    assert coord.appliance_states == {}
    assert coord._activations_today == {}


@pytest.mark.asyncio
async def test_invalid_counter_values_do_not_poison_optimizer():
    data = payload()
    data["appliances"]["app_1"] = {
        "runtime_seconds": float("inf"),
        "energy_kwh": "nan",
        "activations": -2,
    }
    coord = make_coord(data)
    await coord.async_restore_daily_state()
    state = coord._get_appliance_states(coord._get_appliance_configs())["app_1"]
    assert state.runtime_today == timedelta()
    assert state.energy_today == 0
    assert state.activations_today == 0


@pytest.mark.asyncio
async def test_repeated_cycles_schedule_one_bounded_save():
    coord = make_coord()
    await coord.async_restore_daily_state()
    for _ in range(10):
        coord._get_appliance_states(coord._get_appliance_configs())
    coord._daily_state_store.async_delay_save.assert_called_once()
    callback, delay = coord._daily_state_store.async_delay_save.call_args.args
    assert 0 < delay <= 60
    snapshot = callback()
    assert snapshot["appliances"]["app_1"]["runtime_seconds"] > 0
    coord._get_appliance_states(coord._get_appliance_configs())
    assert coord._daily_state_store.async_delay_save.call_count == 2


@pytest.mark.asyncio
async def test_missed_midnight_resets_before_counting():
    coord = make_coord(payload())
    await coord.async_restore_daily_state()
    coord._get_appliance_states(coord._get_appliance_configs())
    coord._daily_state_date = dt_util.now().date() - timedelta(days=1)
    states = coord._get_appliance_states(coord._get_appliance_configs())
    assert states["app_1"].runtime_today <= timedelta(seconds=30)
    assert states["app_1"].activations_today == 0
    assert coord.analytics.savings_today == 0


@pytest.mark.asyncio
async def test_explicit_save_writes_current_day_and_latest_counters():
    coord = make_coord(payload(), True)
    await coord.async_restore_daily_state()
    await coord.async_save_daily_state()
    data = coord._daily_state_store.async_save.call_args.args[0]
    assert data["appliances"]["app_1"]["runtime_seconds"] == 3600
    assert data["date"] == dt_util.now().date().isoformat()


@pytest.fixture
def real_coordinator(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.pv_excess_control.coordinator import PvExcessCoordinator

    entry = MockConfigEntry(
        domain="pv_excess_control",
        data={"controller_interval": 30},
        subentries_data=[
            {
                "subentry_id": "pump",
                "subentry_type": "appliance",
                "unique_id": None,
                "title": "Pump",
                "data": {
                    "appliance_entity": "switch.pump",
                    "appliance_name": "Pump",
                    "nominal_power": 1000,
                },
            },
        ],
    )
    entry.add_to_hass(hass)
    hass.states.async_set("switch.pump", "on")
    return PvExcessCoordinator(hass, entry)


async def test_real_store_restores_disabled_device_before_first_refresh(
    real_coordinator, hass_storage
):
    from custom_components.pv_excess_control.coordinator import PvExcessCoordinator

    coord = real_coordinator
    await coord.async_restore_daily_state()
    coord._get_appliance_states(coord._get_appliance_configs())
    coord.appliance_states["pump"].runtime_today = timedelta(hours=3)
    coord.appliance_states["pump"].energy_today = 2.5
    coord._activations_today["pump"] = 2
    await coord.async_save_daily_state()
    replacement = PvExcessCoordinator(coord.hass, coord.config_entry)
    replacement.appliance_enabled["pump"] = False
    await replacement.async_restore_daily_state()
    restored = replacement._get_appliance_states([])["pump"]
    assert restored.runtime_today == timedelta(hours=3)
    assert restored.energy_today == 2.5
    assert restored.is_on
    assert restored.activations_today == 2
    await replacement.async_save_daily_state()


async def test_real_store_writes_during_continuous_updates(real_coordinator, hass_storage, freezer):
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    coord = real_coordinator
    hass = coord.hass
    await coord.async_restore_daily_state()
    coord._get_appliance_states(coord._get_appliance_configs())
    key = f"pv_excess_control.{coord.config_entry.entry_id}.daily_state"
    for _ in range(4):
        freezer.tick(timedelta(seconds=30))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        coord._get_appliance_states(coord._get_appliance_configs())
    assert key in hass_storage
    assert hass_storage[key]["data"]["appliances"]["pump"]["runtime_seconds"] >= 30
    await coord.async_save_daily_state()


async def test_load_io_error_is_reported_without_aborting_setup(caplog):
    coord = make_coord()
    coord._daily_state_store.async_load = AsyncMock(side_effect=OSError("disk unavailable"))
    await coord.async_restore_daily_state()
    assert "daily" in caplog.text.lower()
    assert coord.appliance_states == {}


async def test_save_failure_does_not_block_unload_and_cap_release(caplog):
    from custom_components.pv_excess_control import async_unload_entry
    from custom_components.pv_excess_control.const import (
        DOMAIN,
        CONF_DYNAMIC_BATTERY_CHARGE_ENABLED,
        CONF_BATTERY_MAX_CHARGE_POWER_W,
    )

    coord = make_coord()
    coord.config_entry.data.update(
        {CONF_DYNAMIC_BATTERY_CHARGE_ENABLED: True, CONF_BATTERY_MAX_CHARGE_POWER_W: 5000}
    )
    coord._daily_state_store.async_save = AsyncMock(side_effect=OSError("disk full"))
    coord._write_battery_max_charge = AsyncMock()
    coord.hass.data[DOMAIN] = {coord.config_entry.entry_id: coord}
    assert await async_unload_entry(coord.hass, coord.config_entry)
    coord._write_battery_max_charge.assert_awaited_once_with(5000)
    assert "daily" in caplog.text.lower()


async def test_midnight_notification_cannot_reset_new_day_twice():
    coord = make_coord(payload())
    await coord.async_restore_daily_state()
    coord._get_appliance_states(coord._get_appliance_configs())
    coord._daily_state_date = dt_util.now().date() - timedelta(days=1)
    # A periodic update beats the midnight callback.
    coord._get_appliance_states(coord._get_appliance_configs())
    coord.appliance_states["app_1"].runtime_today = timedelta(seconds=45)
    coord.async_request_refresh = AsyncMock()
    coord.notifications.notify_daily_summary = AsyncMock()
    await coord.async_handle_midnight()
    assert coord.appliance_states["app_1"].runtime_today == timedelta(seconds=45)
    coord.notifications.notify_daily_summary.assert_awaited_once_with(20.0, 0.4, 2.0)
    await coord.async_handle_midnight()
    assert coord.notifications.notify_daily_summary.await_count == 1
