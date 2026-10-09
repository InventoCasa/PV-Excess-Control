"""Safety regressions for owned grid charging and lifecycle cleanup."""
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import State
from homeassistant.util import dt as dt_util

from custom_components.pv_excess_control.switch import ControlEnabledSwitch, ForceChargeSwitch


def prepared(coordinator_factory, mock_inverter_controller, **overrides):
    data = {
        "battery_soc": "sensor.soc", "battery_capacity": 10,
        "battery_grid_charge_power_w": 2500,
        "battery_max_charge_power_w": 5000,
        "battery_grid_target_soc": 80,
        "inverter_force_charge_enable_entity": "switch.charge",
        "inverter_force_charge_power_entity": "number.power",
        "inverter_force_charge_enable_engage_value": "on",
        "inverter_force_charge_enable_disengage_value": "off",
        **overrides,
    }
    coord = coordinator_factory(config_data=data, inverter_ctl=mock_inverter_controller)
    states = {"sensor.soc": State("sensor.soc", "40"),
              "number.power": State("number.power", "2500", {"min": 0, "max": 5000, "step": .001})}
    coord.hass.states.get.side_effect = states.get
    mock_inverter_controller.confirmation_level = "physical"
    mock_inverter_controller.verify_engaged = AsyncMock()
    mock_inverter_controller.verify_disengaged = AsyncMock()
    return coord, states


def entity(cls, coord):
    switch = cls(coord)
    switch.hass = coord.hass
    switch.async_write_ha_state = lambda: None
    return switch


@pytest.mark.parametrize("soc", [None, "unknown", "unavailable", "nan", "inf", "-1", "101"])
async def test_unknown_or_invalid_soc_cannot_start_even_manual(
    coordinator_factory, mock_inverter_controller, soc,
):
    coord, states = prepared(coordinator_factory, mock_inverter_controller)
    states["sensor.soc"] = None if soc is None else State("sensor.soc", soc)
    await entity(ForceChargeSwitch, coord).async_turn_on()
    mock_inverter_controller.engage.assert_not_awaited()


async def test_stale_soc_prevents_start(coordinator_factory, mock_inverter_controller):
    coord, states = prepared(coordinator_factory, mock_inverter_controller)
    old = dt_util.utcnow() - timedelta(minutes=10)
    states["sensor.soc"] = State("sensor.soc", "40", last_updated=old, last_reported=old)
    await entity(ForceChargeSwitch, coord).async_turn_on()
    mock_inverter_controller.engage.assert_not_awaited()


async def test_real_zero_soc_is_valid(coordinator_factory, mock_inverter_controller):
    coord, states = prepared(coordinator_factory, mock_inverter_controller)
    states["sensor.soc"] = State("sensor.soc", "0")
    await entity(ForceChargeSwitch, coord).async_turn_on()
    mock_inverter_controller.engage.assert_awaited_once_with(2500.0)
    await entity(ForceChargeSwitch, coord).async_turn_off()


async def test_master_disable_immediately_stops_owned_charge(coordinator_factory, mock_inverter_controller):
    coord, _ = prepared(coordinator_factory, mock_inverter_controller)
    await entity(ForceChargeSwitch, coord).async_turn_on()
    await entity(ControlEnabledSwitch, coord).async_turn_off()
    mock_inverter_controller.disengage.assert_awaited_once()
    assert not coord._grid_charge_engaged
    assert not coord.force_charge


async def test_master_disable_blocks_force_switch(coordinator_factory, mock_inverter_controller):
    coord, _ = prepared(coordinator_factory, mock_inverter_controller)
    coord.enabled = False
    await entity(ForceChargeSwitch, coord).async_turn_on()
    mock_inverter_controller.engage.assert_not_awaited()
    assert not coord.force_charge


async def test_cleanup_responsibility_precedes_first_write(coordinator_factory, mock_inverter_controller):
    coord, _ = prepared(coordinator_factory, mock_inverter_controller)
    async def engage(_):
        assert coord.config_entry.data.get("_grid_charge_cleanup_pending") is True
        raise RuntimeError("partial hardware write")
    mock_inverter_controller.engage.side_effect = engage
    await entity(ForceChargeSwitch, coord).async_turn_on()
    mock_inverter_controller.disengage.assert_awaited_once()
    assert not coord._grid_charge_engaged


async def test_failed_stop_keeps_retryable_ownership(coordinator_factory, mock_inverter_controller):
    coord, _ = prepared(coordinator_factory, mock_inverter_controller)
    await entity(ForceChargeSwitch, coord).async_turn_on()
    mock_inverter_controller.disengage.side_effect = RuntimeError("offline")
    await entity(ControlEnabledSwitch, coord).async_turn_off()
    assert coord.config_entry.data["_grid_charge_cleanup_pending"] is True
    mock_inverter_controller.disengage.side_effect = None
    assert await coord.async_stop_battery_controls("retry")
    assert not coord.config_entry.data["_grid_charge_cleanup_pending"]


async def test_missing_plan_never_falls_back_to_price_threshold(
    coordinator_factory, mock_inverter_controller, mock_tariff_at, mock_power_state_with_soc,
):
    coord, _ = prepared(coordinator_factory, mock_inverter_controller, auto_battery_grid_charge=True)
    await coord._run_grid_charge_state_machine(mock_tariff_at(.01, .20), mock_power_state_with_soc(40))
    mock_inverter_controller.engage.assert_not_awaited()


async def test_missing_soc_stops_charge_without_minimum_run_delay(coordinator_factory, mock_inverter_controller):
    coord, states = prepared(coordinator_factory, mock_inverter_controller)
    await entity(ForceChargeSwitch, coord).async_turn_on()
    states["sensor.soc"] = State("sensor.soc", "unavailable")
    await coord._run_grid_charge_state_machine(None, None)
    mock_inverter_controller.disengage.assert_awaited_once()


async def test_failed_unload_preserves_coordinator_for_retry(coordinator_factory, mock_inverter_controller):
    from custom_components.pv_excess_control import async_unload_entry
    coord, _ = prepared(coordinator_factory, mock_inverter_controller)
    await entity(ForceChargeSwitch, coord).async_turn_on()
    coord.async_save_daily_state = AsyncMock()
    coord.hass.data = {"pv_excess_control": {coord.config_entry.entry_id: coord}}
    coord.hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    mock_inverter_controller.disengage.side_effect = RuntimeError("offline")
    assert await async_unload_entry(coord.hass, coord.config_entry) is False
    assert coord.config_entry.entry_id in coord.hass.data["pv_excess_control"]
    coord.hass.config_entries.async_unload_platforms.assert_not_awaited()
    mock_inverter_controller.disengage.side_effect = None
    assert await async_unload_entry(coord.hass, coord.config_entry)


async def test_disable_during_start_cannot_leave_charge_running(coordinator_factory, mock_inverter_controller):
    import asyncio
    coord, _ = prepared(coordinator_factory, mock_inverter_controller)
    started, finish = asyncio.Event(), asyncio.Event()
    async def engage(_):
        started.set()
        await finish.wait()
    mock_inverter_controller.engage.side_effect = engage
    start = asyncio.create_task(entity(ForceChargeSwitch, coord).async_turn_on())
    await started.wait()
    stop = asyncio.create_task(entity(ControlEnabledSwitch, coord).async_turn_off())
    await asyncio.sleep(0)
    finish.set()
    await asyncio.gather(start, stop)
    assert not coord._grid_charge_engaged
    mock_inverter_controller.disengage.assert_awaited()


async def test_cancelled_stop_rearms_independent_cleanup(coordinator_factory, mock_inverter_controller):
    import asyncio
    coord, _ = prepared(coordinator_factory, mock_inverter_controller)
    await entity(ForceChargeSwitch, coord).async_turn_on()
    entered = asyncio.Event()
    async def blocked():
        entered.set()
        await asyncio.Event().wait()
    mock_inverter_controller.disengage.side_effect = blocked
    task = asyncio.create_task(coord.async_stop_battery_controls())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    try:
        assert coord._battery_watchdog is not None
        assert coord.config_entry.data["_grid_charge_cleanup_pending"]
    finally:
        mock_inverter_controller.disengage.side_effect = None
        await coord.async_stop_battery_controls()


async def test_watchdog_stops_owned_charge_if_updates_stall(coordinator_factory, mock_inverter_controller):
    coord, _ = prepared(coordinator_factory, mock_inverter_controller)
    await entity(ForceChargeSwitch, coord).async_turn_on()
    callback = coord._battery_watchdog._callback
    coord._battery_watchdog.cancel()
    callback()
    import asyncio
    await asyncio.sleep(0)
    mock_inverter_controller.disengage.assert_awaited_once()


async def test_startup_releases_persisted_charge_before_fresh_request(coordinator_factory, mock_inverter_controller):
    coord, _ = prepared(coordinator_factory, mock_inverter_controller, _grid_charge_engaged=True)
    coord.force_charge = True
    await coord._run_grid_charge_state_machine(None, None)
    mock_inverter_controller.disengage.assert_awaited_once()
    mock_inverter_controller.engage.assert_not_awaited()


def planned(coordinator_factory, mock_inverter_controller, *, soc=10, **config):
    from custom_components.pv_excess_control.models import ForecastData, HourlyForecast, TariffInfo, TariffWindow
    coord, states = prepared(coordinator_factory, mock_inverter_controller,
        auto_battery_grid_charge=True, allow_grid_charging=True, price_sensor="sensor.price", battery_load_profile_w=[2000.] * 24,
        **config)
    now = dt_util.now()
    states.update({"sensor.soc": State("sensor.soc", str(soc)),
                   "sensor.load_power": State("sensor.load_power", "2000", {"unit_of_measurement": "W"}),
                   "sensor.pv_power": State("sensor.pv_power", "0", {"unit_of_measurement": "W"}),
                   "sensor.price": State("sensor.price", ".1"), "sensor.forecast": State("sensor.forecast", "0")})
    coord._forecast_entities = ["sensor.forecast"]
    coord._forecast_tomorrow_entities = []
    coord._forecast_data = ForecastData(0, [HourlyForecast(now, now + timedelta(hours=4), 0, 0)])
    tariff = TariffInfo(.1, 0, .2, .2, [
        TariffWindow(now, now + timedelta(hours=1), .1, True),
        TariffWindow(now + timedelta(hours=1), now + timedelta(hours=4), .5, False),
    ])
    return coord, states, tariff


async def test_real_economic_plan_drives_and_verifies_charge(coordinator_factory, mock_inverter_controller):
    coord, _, tariff = planned(coordinator_factory, mock_inverter_controller)
    await coord._run_grid_charge_state_machine(tariff, None)
    assert coord._grid_charge_engaged
    assert coord._battery_grid_plan.grid_energy_kwh > 0
    assert coord._battery_grid_plan.estimated_savings > 0
    await coord._run_grid_charge_state_machine(tariff, None)
    mock_inverter_controller.engage.assert_awaited_once()
    mock_inverter_controller.verify_engaged.assert_awaited_once()
    await coord.async_stop_battery_controls()


@pytest.mark.parametrize("entity_id", ["sensor.load_power", "sensor.pv_power", "sensor.price", "sensor.forecast"])
async def test_required_input_loss_stops_auto_charge(coordinator_factory, mock_inverter_controller, entity_id):
    coord, states, tariff = planned(coordinator_factory, mock_inverter_controller)
    await coord._run_grid_charge_state_machine(tariff, None)
    assert coord._grid_charge_engaged
    states[entity_id] = State(entity_id, "unavailable")
    await coord._run_grid_charge_state_machine(tariff, None)
    assert not coord._grid_charge_engaged
    mock_inverter_controller.disengage.assert_awaited_once()


async def test_price_rise_overrides_minimum_run_time(coordinator_factory, mock_inverter_controller):
    coord, states, tariff = planned(coordinator_factory, mock_inverter_controller)
    await coord._run_grid_charge_state_machine(tariff, None)
    tariff.current_price = .5
    states["sensor.price"] = State("sensor.price", ".5")
    await coord._run_grid_charge_state_machine(tariff, None)
    assert not coord._grid_charge_engaged


async def test_fresh_target_stops_charge_without_hysteresis_delay(coordinator_factory, mock_inverter_controller):
    coord, states, tariff = planned(coordinator_factory, mock_inverter_controller)
    await coord._run_grid_charge_state_machine(tariff, None)
    target = coord._current_battery_slot().soc_end
    states["sensor.soc"] = State("sensor.soc", str(target))
    await coord._run_grid_charge_state_machine(tariff, None)
    assert not coord._grid_charge_engaged
    states["sensor.soc"] = State("sensor.soc", str(target - .3))
    await coord._run_grid_charge_state_machine(tariff, None)
    assert not coord._grid_charge_engaged


async def test_stale_telemetry_stops_even_if_value_unchanged(coordinator_factory, mock_inverter_controller):
    coord, states, tariff = planned(coordinator_factory, mock_inverter_controller)
    await coord._run_grid_charge_state_machine(tariff, None)
    old = dt_util.utcnow() - timedelta(minutes=10)
    states["sensor.load_power"] = State("sensor.load_power", "2000", last_reported=old, last_updated=old)
    await coord._run_grid_charge_state_machine(tariff, None)
    assert not coord._grid_charge_engaged


async def test_automatic_helpers_require_physical_confirmation(coordinator_factory, mock_inverter_controller):
    coord, _, tariff = planned(coordinator_factory, mock_inverter_controller)
    mock_inverter_controller.confirmation_level = "entity"
    await coord._run_grid_charge_state_machine(tariff, None)
    mock_inverter_controller.engage.assert_not_awaited()
    assert coord._battery_control_reason == "physical_feedback_required"



async def test_fault_state_survives_failed_automatic_stop(coordinator_factory, mock_inverter_controller):
    coord, _, tariff = planned(coordinator_factory, mock_inverter_controller)
    await coord._run_grid_charge_state_machine(tariff, None)
    coord.config_entry.data["auto_battery_grid_charge"] = False
    mock_inverter_controller.disengage.side_effect = RuntimeError("readback offline")
    await coord._run_grid_charge_state_machine(tariff, None)
    try:
        assert coord._battery_control_state == "fault"
        assert coord._grid_charge_cleanup_pending
    finally:
        mock_inverter_controller.disengage.side_effect = None
        await coord.async_stop_battery_controls()


async def test_malformed_tariff_stops_owned_charge(coordinator_factory, mock_inverter_controller):
    from dataclasses import replace
    coord, _, tariff = planned(coordinator_factory, mock_inverter_controller)
    await coord._run_grid_charge_state_machine(tariff, None)
    tariff.windows[0] = replace(tariff.windows[0], start=tariff.windows[0].start.replace(tzinfo=None))
    await coord._run_grid_charge_state_machine(tariff, None)
    assert not coord._grid_charge_engaged
    assert coord._battery_grid_plan is None


async def test_stale_forecast_clears_cached_plan(coordinator_factory, mock_inverter_controller):
    coord, states, tariff = planned(coordinator_factory, mock_inverter_controller)
    await coord._run_grid_charge_state_machine(tariff, None)
    old = dt_util.utcnow() - timedelta(days=1)
    states["sensor.forecast"] = State("sensor.forecast", "0", last_updated=old, last_reported=old)
    await coord._run_grid_charge_state_machine(tariff, None)
    assert coord._battery_grid_plan is None
    assert not coord.auto_should_engage_now()


@pytest.mark.parametrize(("watts", "expected"), [(2353, 2300), (50, None), (100, 100)])
async def test_power_respects_actuator_steps_without_rounding_up(
    coordinator_factory, mock_inverter_controller, watts, expected,
):
    coord, states = prepared(coordinator_factory, mock_inverter_controller,
        inverter_force_charge_power_entity="number.power")
    states["number.power"] = State("number.power", "2500", {"min": 100, "max": 5000, "step": 100, "unit_of_measurement": "W"})
    assert coord._feasible_battery_power(watts) == expected


async def test_new_higher_slot_target_clears_old_hysteresis_latch(coordinator_factory, mock_inverter_controller):
    coord, _, tariff = planned(coordinator_factory, mock_inverter_controller, soc=10)
    coord._battery_target_latched = 10
    coord._battery_target_slot = dt_util.utcnow() - timedelta(seconds=1)
    await coord._run_grid_charge_state_machine(tariff, None)
    mock_inverter_controller.engage.assert_awaited_once()
    await coord.async_stop_battery_controls()


async def test_durable_ownership_save_finishes_before_hardware_start(coordinator_factory, mock_inverter_controller):
    coord, _ = prepared(coordinator_factory, mock_inverter_controller)
    completed = False
    async def save(*_):
        nonlocal completed
        completed = True
    async def engage(_):
        assert completed
    coord._battery_journal.async_save.side_effect = save
    mock_inverter_controller.engage.side_effect = engage
    await entity(ForceChargeSwitch, coord).async_turn_on()
    await coord.async_stop_battery_controls()


async def test_failed_durable_save_never_starts_hardware(coordinator_factory, mock_inverter_controller):
    coord, _ = prepared(coordinator_factory, mock_inverter_controller)
    coord._battery_journal.async_save.side_effect = OSError("disk unavailable")
    await entity(ForceChargeSwitch, coord).async_turn_on()
    mock_inverter_controller.engage.assert_not_awaited()
    assert coord._grid_charge_cleanup_pending
    coord._battery_journal.async_save.side_effect = None
    await coord.async_stop_battery_controls()


async def test_verified_hold_is_released_on_disable(coordinator_factory, mock_inverter_controller, freezer):
    from unittest.mock import MagicMock
    from custom_components.pv_excess_control.models import InverterGridChargeConfig
    # A partial hour can change the bounded planner's first action. Keep this
    # lifecycle test's hold schedule stable.
    freezer.move_to("2026-10-09T12:00:00Z")
    coord, _, tariff = planned(coordinator_factory, mock_inverter_controller, soc=50)
    tariff.battery_charge_price_threshold = 0  # Only preserve existing energy.
    hold = MagicMock()
    hold.config = InverterGridChargeConfig("switch.hold", "on", "off")
    hold.confirmation_level = "physical"
    hold.engage = AsyncMock()
    hold.disengage = AsyncMock()
    hold.verify_engaged = AsyncMock()
    coord._battery_hold_ctl = hold
    await coord._run_grid_charge_state_machine(tariff, None)
    assert coord._battery_control_state == "holding"
    hold.engage.assert_awaited_once_with(0)
    mock_inverter_controller.engage.assert_not_awaited()
    await entity(ControlEnabledSwitch, coord).async_turn_off()
    hold.disengage.assert_awaited_once()
    assert not coord._battery_hold_cleanup_pending


async def test_profile_learning_keeps_zero_and_does_not_reset_at_midnight(coordinator_factory, mock_inverter_controller, freezer):
    # Learning accepts observation pairs only within the same local hour.
    freezer.move_to("2026-10-09T12:30:00Z")
    coord, states = prepared(coordinator_factory, mock_inverter_controller)
    states["sensor.load_power"] = State("sensor.load_power", "0")
    now = dt_util.now()
    coord._battery_load_last = (now - timedelta(seconds=30), 0.)
    coord._observe_battery_load()
    assert coord._battery_load_hours[now.hour][0] == 0
    assert coord._battery_load_hours[now.hour][1] > 0
    before = [r[:] for r in coord._battery_load_hours]
    coord.reset_daily()
    assert coord._battery_load_hours == before


async def test_explicit_profile_rejects_unknown_or_invalid_hours(coordinator_factory, mock_inverter_controller):
    coord, _ = prepared(coordinator_factory, mock_inverter_controller, battery_load_profile_w=[500.] * 23 + [None])
    assert coord._battery_load_profile() is None


async def test_journal_restores_original_controls_after_configuration_changes(coordinator_factory, mock_inverter_controller):
    from custom_components.pv_excess_control.models import InverterGridChargeConfig
    coord, states = prepared(coordinator_factory, mock_inverter_controller)
    original_charge = InverterGridChargeConfig("switch.original_charge", "on", "off")
    original_hold = InverterGridChargeConfig("switch.original_hold", "on", "off")
    states["switch.original_charge"] = State("switch.original_charge", "on")
    states["switch.original_hold"] = State("switch.original_hold", "on")
    async def update_state(domain, service, data, **kwargs):
        states[data["entity_id"]] = State(data["entity_id"], "off")
    coord.hass.services.async_call.side_effect = update_state
    coord._battery_journal.async_load.return_value = (original_charge, original_hold)
    coord._battery_journal.loaded_record_exists = True
    await coord._restore_battery_ownership()
    await coord._run_grid_charge_state_machine(None, None)
    assert [c.args[2]["entity_id"] for c in coord.hass.services.async_call.await_args_list] == [
        "switch.original_charge", "switch.original_hold",
    ]
    mock_inverter_controller.disengage.assert_not_awaited()
    coord._battery_journal.async_save.assert_awaited_with(None, None)


async def test_cleared_journal_overrides_late_config_flags(coordinator_factory, mock_inverter_controller):
    coord, _ = prepared(coordinator_factory, mock_inverter_controller, _grid_charge_engaged=True)
    coord._battery_journal.async_load.return_value = (None, None)
    coord._battery_journal.loaded_record_exists = True
    await coord._restore_battery_ownership()
    await coord._run_grid_charge_state_machine(None, None)
    mock_inverter_controller.disengage.assert_not_awaited()
    assert not coord._grid_charge_cleanup_pending


async def test_cancelled_journal_clear_retains_retryable_ownership(coordinator_factory, mock_inverter_controller):
    import asyncio
    coord, _ = prepared(coordinator_factory, mock_inverter_controller)
    await entity(ForceChargeSwitch, coord).async_turn_on()
    entered = asyncio.Event()
    async def blocked(*_):
        entered.set()
        await asyncio.Event().wait()
    coord._battery_journal.async_save.side_effect = blocked
    stop = asyncio.create_task(coord.async_stop_battery_controls())
    await entered.wait()
    stop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stop
    try:
        assert coord._grid_charge_cleanup_pending
        assert coord._battery_watchdog is not None
    finally:
        coord._battery_journal.async_save.side_effect = None
        await coord.async_stop_battery_controls()


async def test_negative_household_sample_does_not_poison_next_hour_average(coordinator_factory, mock_inverter_controller):
    coord, states = prepared(coordinator_factory, mock_inverter_controller)
    states["sensor.load_power"] = State("sensor.load_power", "-5000")
    coord._observe_battery_load()
    now = dt_util.now()
    assert coord._battery_load_last[1] is None
    coord._battery_load_last = (now - timedelta(seconds=30), None)
    states["sensor.load_power"] = State("sensor.load_power", "500")
    coord._observe_battery_load()
    assert coord._battery_load_hours[now.hour][1] == 0


async def test_automatic_requires_adjustable_power_control(coordinator_factory, mock_inverter_controller):
    coord, _, tariff = planned(coordinator_factory, mock_inverter_controller)
    coord.config_entry.data.pop("inverter_force_charge_power_entity", None)
    await coord._run_grid_charge_state_machine(tariff, None)
    mock_inverter_controller.engage.assert_not_awaited()
    assert coord._battery_control_reason == "adjustable_power_control_required"


async def test_cold_dynamic_cap_is_released_before_forced_charge(coordinator_factory, mock_inverter_controller):
    coord, _ = prepared(coordinator_factory, mock_inverter_controller,
        dynamic_battery_charge_enabled=True, inverter_battery_max_charge_power_entity="number.pv_cap",
        battery_max_charge_power_w=5000)
    coord._dyn_charge_loop_active = False
    events = []
    async def lift(power):
        events.append(("cap", power))
        return True
    async def start(power):
        events.append(("charge", power))
    coord._write_battery_max_charge = AsyncMock(side_effect=lift)
    mock_inverter_controller.engage.side_effect = start
    await entity(ForceChargeSwitch, coord).async_turn_on()
    try:
        assert events == [("cap", 5000), ("charge", 2500)]
    finally:
        await coord.async_stop_battery_controls()


async def test_unconfirmed_cap_release_blocks_grid_start(coordinator_factory, mock_inverter_controller):
    coord, _ = prepared(coordinator_factory, mock_inverter_controller,
        dynamic_battery_charge_enabled=True, inverter_battery_max_charge_power_entity="number.pv_cap",
        battery_max_charge_power_w=5000)
    coord._write_battery_max_charge = AsyncMock(return_value=False)
    await entity(ForceChargeSwitch, coord).async_turn_on()
    mock_inverter_controller.engage.assert_not_awaited()


def test_solar_cap_yields_during_hold():
    from types import SimpleNamespace
    from custom_components.pv_excess_control.coordinator import _dyn_charge_should_run
    coord = SimpleNamespace(config_entry=SimpleNamespace(data={"dynamic_battery_charge_enabled": True}),
                            _battery_hold_engaged=True, enabled=True)
    assert not _dyn_charge_should_run(coord)


async def test_manual_helper_requires_hardware_feedback(coordinator_factory, mock_inverter_controller):
    coord, _ = prepared(coordinator_factory, mock_inverter_controller)
    mock_inverter_controller.confirmation_level = "entity"
    await entity(ForceChargeSwitch, coord).async_turn_on()
    mock_inverter_controller.engage.assert_not_awaited()
    assert coord._battery_control_reason == "physical_feedback_required"


async def test_disable_during_solar_cap_release_prevents_hardware_start(coordinator_factory, mock_inverter_controller):
    import asyncio
    coord, _ = prepared(coordinator_factory, mock_inverter_controller,
        dynamic_battery_charge_enabled=True, inverter_battery_max_charge_power_entity="number.pv_cap")
    entered, finish = asyncio.Event(), asyncio.Event()
    async def lift(_):
        entered.set()
        await finish.wait()
        return True
    coord._write_battery_max_charge = AsyncMock(side_effect=lift)
    start = asyncio.create_task(entity(ForceChargeSwitch, coord).async_turn_on())
    await entered.wait()
    stop = asyncio.create_task(entity(ControlEnabledSwitch, coord).async_turn_off())
    await asyncio.sleep(0)
    finish.set()
    await asyncio.gather(start, stop)
    mock_inverter_controller.engage.assert_not_awaited()


async def test_input_loss_during_cap_release_prevents_automatic_start(coordinator_factory, mock_inverter_controller):
    coord, states, tariff = planned(coordinator_factory, mock_inverter_controller,
        dynamic_battery_charge_enabled=True, inverter_battery_max_charge_power_entity="number.pv_cap")
    async def lift(_):
        states["sensor.load_power"] = State("sensor.load_power", "unavailable")
        return True
    coord._write_battery_max_charge = AsyncMock(side_effect=lift)
    await coord._run_grid_charge_state_machine(tariff, None)
    mock_inverter_controller.engage.assert_not_awaited()
    assert coord._battery_grid_plan is None


async def test_unload_stops_before_flushing_daily_history(coordinator_factory, mock_inverter_controller):
    from custom_components.pv_excess_control import async_unload_entry
    coord, _ = prepared(coordinator_factory, mock_inverter_controller)
    await entity(ForceChargeSwitch, coord).async_turn_on()
    async def save():
        assert not coord._grid_charge_engaged
    coord.async_save_daily_state = AsyncMock(side_effect=save)
    coord.hass.data = {"pv_excess_control": {coord.config_entry.entry_id: coord}}
    coord.hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    try:
        assert await async_unload_entry(coord.hass, coord.config_entry)
    finally:
        await coord.async_stop_battery_controls()
