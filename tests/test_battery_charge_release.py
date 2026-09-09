"""Failed and cold-start dynamic battery cap releases remain retryable."""

from datetime import datetime

import pytest

from custom_components.pv_excess_control.coordinator import PvExcessCoordinator
from tests.test_coordinator_battery_charge import _coordinator_with_dyn_charge_enabled


def coordinator():
    coord = _coordinator_with_dyn_charge_enabled()
    coord._forecast_status = "unavailable"
    coord._dyn_charge_release_pending = False
    coord._dyn_charge_pause_released = False
    coord.current_plan = None
    coord._write_battery_max_charge = (
        PvExcessCoordinator._write_battery_max_charge.__get__(coord)
    )
    coord.hass.states.get.return_value.state = "100"
    return coord


async def dispatch(coord):
    await PvExcessCoordinator._dispatch_dynamic_battery_charge.__get__(coord)(None)


async def test_cold_forecast_failure_releases_retained_cap_once():
    coord = coordinator()
    await dispatch(coord)
    await dispatch(coord)
    coord.hass.services.async_call.assert_awaited_once()
    assert coord.hass.services.async_call.call_args.args[2]["value"] == 5000
    assert coord._dyn_charge_pause_released
    assert not coord._dyn_charge_release_pending


@pytest.mark.parametrize("cold", [True, False])
async def test_failed_release_retries_real_writer_until_success(cold):
    coord = coordinator()
    coord._dyn_charge_loop_active = not cold
    coord.hass.services.async_call.side_effect = [
        RuntimeError("temporary service failure"),
        None,
    ]
    await dispatch(coord)
    assert coord._dyn_charge_release_pending
    assert not coord._dyn_charge_pause_released
    await dispatch(coord)
    await dispatch(coord)
    assert coord.hass.services.async_call.await_count == 2
    assert not coord._dyn_charge_release_pending
    assert coord._dyn_charge_pause_released


async def test_unavailable_actuator_release_retries_after_entity_recovers():
    coord = coordinator()
    state = coord.hass.states.get.return_value
    state.state = "unavailable"
    await dispatch(coord)
    assert coord._dyn_charge_release_pending
    coord.hass.services.async_call.assert_not_awaited()
    state.state = "100"
    await dispatch(coord)
    assert not coord._dyn_charge_release_pending
    coord.hass.services.async_call.assert_awaited_once()


@pytest.mark.parametrize(
    "blocked",
    ["disabled", "self_disabled", "missing_entity", "invalid_max", "startup_grace"],
)
async def test_cold_release_requires_valid_enabled_configuration_after_grace(blocked):
    coord = coordinator()
    if blocked == "disabled":
        coord.config_entry.data["dynamic_battery_charge_enabled"] = False
    elif blocked == "self_disabled":
        coord._dyn_charge_self_disabled_reason = "invalid_export_limit"
    elif blocked == "missing_entity":
        coord.config_entry.data.pop("inverter_battery_max_charge_power_entity")
    elif blocked == "invalid_max":
        coord.config_entry.data["battery_max_charge_power_w"] = 0
    else:
        coord._startup_time = datetime.now()
    await dispatch(coord)
    coord.hass.services.async_call.assert_not_awaited()
    assert not coord._dyn_charge_release_pending


async def test_recovery_cancels_old_release_and_next_pause_releases_again():
    coord = coordinator()
    await dispatch(coord)
    coord._forecast_status = "available"
    await dispatch(coord)
    assert coord._dyn_charge_loop_active
    assert not coord._dyn_charge_pause_released
    coord._forecast_status = "unavailable"
    await dispatch(coord)
    await dispatch(coord)
    assert [
        call.args[2]["value"] for call in coord.hass.services.async_call.await_args_list
    ] == [5000, 100, 5000]


async def test_writer_reports_failure_success_and_dedupe_success():
    coord = coordinator()
    coord.hass.services.async_call.side_effect = [RuntimeError("unreachable"), None]
    assert await coord._write_battery_max_charge(5000) is False
    assert await coord._write_battery_max_charge(5000) is True
    assert await coord._write_battery_max_charge(5000) is True
    assert coord.hass.services.async_call.await_count == 2


async def test_pending_release_is_visible_until_actuator_recovers():
    from custom_components.pv_excess_control.sensor import PvExcessStatusSensor

    coord = coordinator()
    sensor = PvExcessStatusSensor(coord)
    state = coord.hass.states.get.return_value
    state.state = "unavailable"
    await dispatch(coord)
    assert (
        sensor.extra_state_attributes["dynamic_battery_charge_release_pending"] is True
    )
    assert (
        sensor.extra_state_attributes["dynamic_battery_charge_status"]
        == "paused: forecast_unavailable"
    )
    state.state = "100"
    await dispatch(coord)
    assert (
        sensor.extra_state_attributes["dynamic_battery_charge_release_pending"] is False
    )


async def test_cold_release_waits_for_grace_then_releases():
    coord = coordinator()
    coord._startup_time = datetime.now()
    await dispatch(coord)
    coord.hass.services.async_call.assert_not_awaited()
    coord._startup_time = datetime(2020, 1, 1)
    await dispatch(coord)
    coord.hass.services.async_call.assert_awaited_once()


async def test_recovery_cancels_failed_release_before_applying_valid_curve():
    coord = coordinator()
    coord.hass.services.async_call.side_effect = [
        RuntimeError("temporary failure"),
        None,
    ]
    await dispatch(coord)
    assert coord._dyn_charge_release_pending
    coord._forecast_status = "available"
    await dispatch(coord)
    assert not coord._dyn_charge_release_pending
    assert coord._dyn_charge_loop_active
    assert [
        call.args[2]["value"] for call in coord.hass.services.async_call.await_args_list
    ] == [5000, 100]


async def test_startup_validator_allows_late_actuator_then_releases_cold_cap():
    from types import SimpleNamespace

    coord = coordinator()
    coord._get_battery_config = lambda: SimpleNamespace(capacity_kwh=10)
    actuator = coord.hass.states.get.return_value
    coord.hass.states.get.return_value = None
    PvExcessCoordinator._validate_dynamic_battery_charge_config(coord)
    assert coord._dyn_charge_self_disabled_reason is None
    await dispatch(coord)
    assert coord._dyn_charge_release_pending
    coord.hass.services.async_call.assert_not_awaited()
    coord.hass.states.get.return_value = actuator
    await dispatch(coord)
    assert not coord._dyn_charge_release_pending
    coord.hass.services.async_call.assert_awaited_once()
    assert coord.hass.services.async_call.call_args.args[2]["value"] == 5000


@pytest.mark.parametrize(
    ("key", "value", "reason"),
    [
        ("inverter_battery_max_charge_power_entity", None, "entity_not_configured"),
        (
            "inverter_battery_max_charge_power_entity",
            "sensor.bad",
            "entity_wrong_domain",
        ),
        ("battery_max_charge_power_w", 0, "invalid_max_power"),
        ("export_limit", 0, "invalid_export_limit"),
        ("battery_capacity", 0, "invalid_battery_capacity"),
    ],
)
async def test_missing_actuator_does_not_skip_static_validation(key, value, reason):
    from types import SimpleNamespace

    coord = coordinator()
    coord.config_entry.data[key] = value
    coord._get_battery_config = lambda: SimpleNamespace(
        capacity_kwh=0 if key == "battery_capacity" else 10
    )
    coord.hass.states.get.return_value = None
    PvExcessCoordinator._validate_dynamic_battery_charge_config(coord)
    assert coord._dyn_charge_self_disabled_reason == reason
    await dispatch(coord)
    coord.hass.services.async_call.assert_not_awaited()
    assert not coord._dyn_charge_release_pending
