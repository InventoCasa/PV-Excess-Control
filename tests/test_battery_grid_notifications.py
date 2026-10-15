"""Measured grid charging notifications are independent of inverter commands."""
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.core import State
from homeassistant.util import dt as dt_util

from custom_components.pv_excess_control.const import NotificationEvent
from custom_components.pv_excess_control.switch import ForceChargeSwitch
from tests.test_battery_control_safety import entity, prepared


@pytest.fixture
def charging_case(coordinator_factory, mock_inverter_controller):
    coord, states = prepared(
        coordinator_factory, mock_inverter_controller,
        battery_charge_power="sensor.charge", battery_discharge_power="sensor.discharge",
    )
    now = dt_util.utcnow()

    def readings(*, charge=0, discharge=0, pv=1600, load=600, export=1000,
                 age=0, unit="W", combined=None, ages=None):
        stamp = now - timedelta(seconds=age)
        values = {"sensor.charge": charge, "sensor.discharge": discharge,
                  "sensor.pv_power": pv, "sensor.load_power": load,
                  "sensor.grid_export": export}
        if combined is not None:
            values["sensor.grid"] = combined
        for name, value in values.items():
            reported = stamp - timedelta(seconds=(ages or {}).get(name, 0))
            states[name] = State(name, str(value), {"unit_of_measurement": unit},
                                 last_updated=reported, last_reported=reported)
        states["sensor.soc"] = State("sensor.soc", "40", last_updated=stamp, last_reported=stamp)

    def messages():
        return [call.args[2]["message"] for call in coord.hass.services.async_call.await_args_list
                if call.args[0] in ("notify", "persistent_notification")]

    async def cycle(seconds=0, **values):
        nonlocal now
        now += timedelta(seconds=seconds)
        readings(**values)
        with patch("custom_components.pv_excess_control.battery_control.dt_util.utcnow", return_value=now):
            await coord._run_grid_charge_state_machine(None, None)

    readings()
    return coord, states, cycle, messages


async def test_confirmed_command_without_measured_grid_flow_is_silent(charging_case):
    coord, _, cycle, messages = charging_case
    try:
        await entity(ForceChargeSwitch, coord).async_turn_on()
        assert messages() == []
        await cycle(35, charge=900, pv=1600, load=600, export=100)
        assert messages() == []
        await coord.async_stop_battery_controls()
        assert messages() == []
    finally:
        coord._cancel_battery_watchdog()


async def test_observed_start_and_short_confirmed_release_are_paired(charging_case):
    coord, _, cycle, messages = charging_case
    try:
        await entity(ForceChargeSwitch, coord).async_turn_on()
        await cycle(charge=2000, pv=500, load=1000, export=0)
        assert messages() == []
        await cycle(29, charge=2000, pv=500, load=1000, export=0)
        assert messages() == []
        await cycle(1, charge=2000, pv=500, load=1000, export=0)
        assert len(messages()) == 1
        assert "started" in messages()[0]
        await coord.async_stop_battery_controls("disabled")
        assert len(messages()) == 2
        assert "stopped" in messages()[1]
    finally:
        coord._cancel_battery_watchdog()


async def test_pv_takeover_debounces_short_dips_and_ends_once(charging_case):
    coord, _, cycle, messages = charging_case
    grid = dict(charge=2000, pv=500, load=1000, export=0)
    solar = dict(charge=2000, pv=3000, load=800, export=200)
    try:
        await entity(ForceChargeSwitch, coord).async_turn_on()
        await cycle(**grid)
        await cycle(30, **grid)
        assert len(messages()) == 1
        await cycle(30, **solar)
        await cycle(30, **grid)
        assert len(messages()) == 1
        await cycle(30, **solar)
        await cycle(30, **solar)
        assert len(messages()) == 1
        await cycle(30, **solar)
        assert len(messages()) == 2
        assert "stopped" in messages()[-1]
        assert coord._grid_charge_engaged
        await cycle(30, **solar)
        await coord.async_stop_battery_controls()
        assert len(messages()) == 2
    finally:
        coord._cancel_battery_watchdog()


async def test_missing_measurements_never_mean_charging_has_stopped(charging_case):
    coord, _, cycle, messages = charging_case
    try:
        await entity(ForceChargeSwitch, coord).async_turn_on()
        await cycle(charge=2000, export=0)
        await cycle(30, charge=2000, export=0)
        assert len(messages()) == 1
        await cycle(30, charge="unavailable", export=0)
        await cycle(60, charge="unavailable", export=0)
        assert len(messages()) == 1
        await coord.async_stop_battery_controls()
        assert len(messages()) == 2
    finally:
        coord._cancel_battery_watchdog()


async def test_identical_sample_cannot_confirm_sustained_start(charging_case):
    coord, states, cycle, messages = charging_case
    try:
        await entity(ForceChargeSwitch, coord).async_turn_on()
        await cycle(charge=2000, export=0)
        stamp = states["sensor.charge"].last_reported
        with patch("custom_components.pv_excess_control.battery_control.dt_util.utcnow",
                   return_value=stamp + timedelta(seconds=40)):
            await coord._run_grid_charge_state_machine(None, None)
        assert messages() == []
    finally:
        coord._cancel_battery_watchdog()


async def test_failed_notification_cannot_create_orphan_stop(charging_case):
    coord, _, cycle, messages = charging_case
    coord.hass.services.async_call.side_effect = RuntimeError("notification unavailable")
    try:
        await entity(ForceChargeSwitch, coord).async_turn_on()
        await cycle(charge=2000, export=0)
        await cycle(30, charge=2000, export=0)
        assert coord._grid_charge_engaged
        assert len(messages()) == 1  # attempted start only
        coord.hass.services.async_call.side_effect = None
        await coord.async_stop_battery_controls()
        assert len(messages()) == 1
    finally:
        coord._cancel_battery_watchdog()


@pytest.mark.parametrize("suppression", ["disabled", "rate_limited"])
async def test_suppressed_start_does_not_produce_a_stop(charging_case, suppression):
    coord, _, cycle, messages = charging_case
    if suppression == "disabled":
        coord.notifications.settings[NotificationEvent.FORCE_CHARGE] = False
    else:
        await coord.notifications.notify_force_charge(True)
        coord.hass.services.async_call.reset_mock()
    try:
        await entity(ForceChargeSwitch, coord).async_turn_on()
        await cycle(charge=2000, export=0)
        await cycle(30, charge=2000, export=0)
        await coord.async_stop_battery_controls()
        assert messages() == []
    finally:
        coord._cancel_battery_watchdog()


async def test_native_charge_cannot_announce_with_unrelated_house_import(charging_case):
    coord, _, cycle, messages = charging_case
    await cycle(charge=2000, pv=2000, load=2000, export=0)
    await cycle(30, charge=2000, pv=2000, load=2000, export=0)
    assert not coord._grid_charge_engaged
    assert messages() == []


@pytest.mark.parametrize("key,value", [
    ("battery_charge_power", "unavailable"), ("battery_charge_power", "nan"),
    ("battery_charge_power", "inf"), ("battery_charge_power", "-1"),
    ("pv_power", "unavailable"), ("pv_power", "-1"),
    ("load_power", "unknown"), ("load_power", "-1"),
])
async def test_invalid_required_reading_is_unknown(charging_case, key, value):
    coord, states, cycle, _ = charging_case
    await cycle(charge=2000, pv=500, load=1000, export=0)
    entity_id = coord.config_entry.data[key]
    states[entity_id] = State(entity_id, value)
    assert coord._measured_battery_grid_flow() is None


@pytest.mark.parametrize("key", ["battery_charge_power", "pv_power", "load_power"])
async def test_stale_required_reading_is_unknown(charging_case, key):
    coord, states, cycle, _ = charging_case
    await cycle(charge=2000, pv=500, load=1000, export=0)
    entity_id = coord.config_entry.data[key]
    old = dt_util.utcnow() - timedelta(seconds=301)
    states[entity_id] = State(entity_id, "1000", last_updated=old, last_reported=old)
    assert coord._measured_battery_grid_flow() is None


@pytest.mark.parametrize("export", ["0", "unavailable", None])
async def test_independent_balance_does_not_require_export_only_zero(charging_case, export):
    coord, states, cycle, _ = charging_case
    await cycle(charge=2000, pv=500, load=1000, export=0)
    old = dt_util.utcnow() - timedelta(hours=1)
    states["sensor.grid_export"] = None if export is None else State(
        "sensor.grid_export", export, last_updated=old, last_reported=old,
    )
    assert coord._measured_battery_grid_flow()[0] == 2000


async def test_fresh_export_vetoes_conflicting_positive_balance(charging_case):
    coord, _, cycle, _ = charging_case
    await cycle(charge=2000, pv=500, load=1000, export=200)
    assert coord._measured_battery_grid_flow()[0] == 0


@pytest.mark.parametrize("grid,expected", [("-700", 700), ("250", 0), ("unavailable", None)])
async def test_combined_grid_is_authoritative(charging_case, grid, expected):
    coord, states, cycle, _ = charging_case
    coord.config_entry.data["import_export_power"] = "sensor.grid"
    await cycle(charge=2000, pv=500, load=1000, export=0, combined=grid)
    # A combined meter does not require inferred PV/load balance.
    states["sensor.pv_power"] = None
    result = coord._measured_battery_grid_flow()
    assert (result[0] if result else None) == expected


async def test_stale_combined_grid_does_not_fall_back(charging_case):
    coord, states, cycle, _ = charging_case
    coord.config_entry.data["import_export_power"] = "sensor.grid"
    await cycle(charge=2000, pv=500, load=1000, export=0)
    old = dt_util.utcnow() - timedelta(seconds=301)
    states["sensor.grid"] = State("sensor.grid", "-1500", last_updated=old, last_reported=old)
    assert coord._measured_battery_grid_flow() is None


@pytest.mark.parametrize("signed,expected", [("1500", 1500), ("-1500", 0), ("unavailable", None)])
async def test_combined_battery_polarity(charging_case, signed, expected):
    coord, states, cycle, _ = charging_case
    coord.config_entry.data["battery_power"] = "sensor.battery"
    await cycle(pv=0, load=1000, export=0)
    states["sensor.battery"] = State("sensor.battery", signed)
    result = coord._measured_battery_grid_flow()
    assert (result[0] if result else None) == expected


async def test_conflicting_fresh_split_charge_and_discharge_is_unknown(charging_case):
    coord, _, cycle, _ = charging_case
    await cycle(charge=1000, discharge=1000, pv=0, load=500, export=0)
    assert coord._measured_battery_grid_flow() is None


async def test_kw_measurements_are_normalized(charging_case):
    coord, _, cycle, _ = charging_case
    await cycle(charge=2, pv=.5, load=1, export=0, unit="kW")
    assert coord._measured_battery_grid_flow()[0] == 2000


async def test_diagnostic_is_observed_contribution_not_requested_power(charging_case):
    coord, _, cycle, _ = charging_case
    try:
        await entity(ForceChargeSwitch, coord).async_turn_on()
        await cycle(charge=1200, pv=1500, load=600, export=0)
        assert coord.battery_control_diagnostics()["battery_grid_charge_observed_power_w"] == 300
        await cycle(charge="unknown", export=0)
        assert coord.battery_control_diagnostics()["battery_grid_charge_observed_power_w"] is None
    finally:
        coord._cancel_battery_watchdog()


async def test_failed_stop_delivery_does_not_block_future_episodes(charging_case):
    coord, _, cycle, messages = charging_case
    try:
        await entity(ForceChargeSwitch, coord).async_turn_on()
        await cycle(charge=2000, export=0)
        await cycle(30, charge=2000, export=0)
        coord.hass.services.async_call.side_effect = RuntimeError("notification unavailable")
        await coord.async_stop_battery_controls()
        coord.hass.services.async_call.side_effect = None
        coord.notifications._rate_limit_seconds = 0
        await cycle(30, charge=2000, export=0)
        assert not coord._grid_charge_engaged
        # A failed notification does not bypass the confirmed stop's delay.
        await cycle(300, charge=2000, export=0)
        await cycle(30, charge=2000, export=0)
        assert len(messages()) == 3
        assert "started" in messages()[-1]
    finally:
        coord._cancel_battery_watchdog()


async def test_old_sample_refresh_does_not_skip_confirmation_duration(charging_case):
    coord, _, cycle, messages = charging_case
    try:
        await entity(ForceChargeSwitch, coord).async_turn_on()
        await cycle(charge=2000, export=0, age=50)
        await cycle(1, charge=2000, export=0)
        assert messages() == []
        await cycle(29, charge=2000, export=0)
        assert len(messages()) == 1
    finally:
        coord._cancel_battery_watchdog()


async def test_notification_timeout_cannot_block_control_or_create_stop(charging_case):
    import asyncio

    coord, _, cycle, messages = charging_case
    delivery_started = asyncio.Event()

    async def unresponsive_delivery(*_args, **_kwargs):
        delivery_started.set()
        await asyncio.Event().wait()

    coord.hass.services.async_call.side_effect = unresponsive_delivery
    try:
        await entity(ForceChargeSwitch, coord).async_turn_on()
        await cycle(charge=2000, export=0)
        with patch("custom_components.pv_excess_control.battery_control._GRID_NOTICE_DELIVERY_TIMEOUT_SECONDS", .01):
            await cycle(30, charge=2000, export=0)
        assert delivery_started.is_set()
        assert coord._grid_charge_engaged
        assert coord._battery_control_state == "charging"
        coord.hass.services.async_call.side_effect = None
        await coord.async_stop_battery_controls()
        assert len(messages()) == 1
    finally:
        coord._cancel_battery_watchdog()


@pytest.mark.parametrize("power", [0, 40, 100])
async def test_small_grid_residual_does_not_start_notice(charging_case, power):
    coord, _, cycle, messages = charging_case
    try:
        await entity(ForceChargeSwitch, coord).async_turn_on()
        await cycle(charge=power, pv=0, load=500, export=0)
        await cycle(30, charge=power, pv=0, load=500, export=0)
        assert messages() == []
    finally:
        coord._cancel_battery_watchdog()


async def test_minimum_noise_band_retains_announced_episode(charging_case):
    coord, _, cycle, messages = charging_case
    try:
        await entity(ForceChargeSwitch, coord).async_turn_on()
        await cycle(charge=1000, pv=0, export=0)
        await cycle(30, charge=1000, pv=0, export=0)
        await cycle(30, charge=75, pv=0, export=0)
        await cycle(60, charge=75, pv=0, export=0)
        assert len(messages()) == 1
        await coord.async_stop_battery_controls()
        assert len(messages()) == 2
    finally:
        coord._cancel_battery_watchdog()


async def test_observation_gap_requires_new_confirmation(charging_case):
    coord, _, cycle, messages = charging_case
    try:
        await entity(ForceChargeSwitch, coord).async_turn_on()
        await cycle(charge=1000, pv=0, export=0)
        await cycle(120, charge=1000, pv=0, export=0)
        assert messages() == []
        await cycle(30, charge=1000, pv=0, export=0)
        assert len(messages()) == 1
    finally:
        coord._cancel_battery_watchdog()


async def test_stale_zero_discharge_template_does_not_hide_fresh_charge(charging_case):
    coord, _, cycle, messages = charging_case
    try:
        await entity(ForceChargeSwitch, coord).async_turn_on()
        await cycle(charge=2000, export=0, ages={"sensor.discharge": 3600})
        await cycle(30, charge=2000, export=0, ages={"sensor.discharge": 3600})
        assert len(messages()) == 1
        assert "started" in messages()[0]
    finally:
        coord._cancel_battery_watchdog()


async def test_refreshed_balance_cannot_reuse_identical_charge_sample(charging_case):
    coord, _, cycle, messages = charging_case
    try:
        await entity(ForceChargeSwitch, coord).async_turn_on()
        await cycle(charge=2000, export=0, ages={"sensor.pv_power": 50, "sensor.load_power": 50})
        await cycle(30, charge=2000, export=0, ages={"sensor.charge": 30})
        assert messages() == []
        await cycle(30, charge=2000, export=0)
        assert len(messages()) == 1
    finally:
        coord._cancel_battery_watchdog()


async def test_unchanged_valid_zero_pv_does_not_block_grid_start(charging_case):
    coord, _, cycle, messages = charging_case
    try:
        await entity(ForceChargeSwitch, coord).async_turn_on()
        await cycle(charge=2000, pv=0, export=0)
        await cycle(30, charge=2000, pv=0, export=0, ages={"sensor.pv_power": 30})
        assert len(messages()) == 1
    finally:
        coord._cancel_battery_watchdog()


@pytest.mark.parametrize("charge", ["unknown", 1000])
async def test_fresh_export_can_confirm_stop_without_fresh_battery(charging_case, charge):
    coord, _, cycle, messages = charging_case
    try:
        await entity(ForceChargeSwitch, coord).async_turn_on()
        await cycle(charge=2000, export=0)
        await cycle(30, charge=2000, export=0)
        await cycle(30, charge=charge, export=500, ages={"sensor.charge": 3600})
        await cycle(60, charge=charge, export=500, ages={"sensor.charge": 3600})
        assert len(messages()) == 2
        assert "stopped" in messages()[-1]
    finally:
        coord._cancel_battery_watchdog()


async def test_zero_charge_with_advancing_balance_confirms_stop(charging_case):
    coord, _, cycle, messages = charging_case
    try:
        await entity(ForceChargeSwitch, coord).async_turn_on()
        await cycle(charge=2000, export=0)
        await cycle(30, charge=2000, export=0)
        await cycle(30, charge=0, export=0)
        await cycle(30.1, charge=0, export=0, ages={"sensor.charge": 30.1})
        await cycle(30.1, charge=0, export=0, ages={"sensor.charge": 60.2})
        assert len(messages()) == 2
    finally:
        coord._cancel_battery_watchdog()


@pytest.mark.parametrize("discharge", ["unknown", "unavailable", 0, 1000])
async def test_stale_complement_cannot_override_fresh_directional_charge(charging_case, discharge):
    coord, _, cycle, _ = charging_case
    await cycle(charge=2000, discharge=discharge, pv=0, load=500, export=0,
                ages={"sensor.discharge": 3600})
    assert coord._measured_battery_grid_flow()[0] == 2000


async def test_positive_charge_requires_recent_primary_sample(charging_case):
    coord, _, cycle, _ = charging_case
    await cycle(charge=2000, export=0, ages={"sensor.charge": 61})
    assert coord._measured_battery_grid_flow() is None
