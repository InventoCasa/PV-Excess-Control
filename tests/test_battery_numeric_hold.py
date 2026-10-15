"""Economic hold and consumer discharge limits share one owned actuator."""
import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import State

from custom_components.pv_excess_control import battery_control
from custom_components.pv_excess_control.inverter_control import InverterGridChargeController
from custom_components.pv_excess_control.models import BatteryDischargeAction, InverterGridChargeConfig
from tests.test_battery_control_economics import economic_case  # noqa: F401


@pytest.fixture
def numeric_case(economic_case, monkeypatch):
    case = economic_case([.2, .4], [0, 0], [1000, 2000],
                         battery_max_discharge_entity="input_number.discharge",
                         battery_max_discharge_default=5000,
                         battery_hold_entity="input_number.discharge",
                         battery_hold_engage_value="10", battery_hold_release_value="5000",
                         battery_hold_feedback_entity="sensor.discharge_limit")
    config = InverterGridChargeConfig(
        "input_number.discharge", "10", "5000",
        enable_feedback_entity_id="sensor.discharge_limit", timeout_seconds=.01,
    )
    case.coord._battery_hold_ctl = MagicMock(config=config, confirmation_level="physical")
    case.coord.config_entry.data["battery_hold_verified"] = True
    case.coord._battery_action_after_wait = AsyncMock(side_effect=lambda *_:
        SimpleNamespace(action="hold", end=case.coord._battery_numeric_hold_deadline))
    case.reading("input_number.discharge", 5000, min=10, max=5000, step=10, unit_of_measurement="W")
    case.reading("sensor.discharge_limit", 5000, unit_of_measurement="W")
    case.writes = []
    case.fail_release = False
    case.ignore_feedback = False

    async def service(domain, name, data, **kwargs):
        assert domain == "input_number" and name == "set_value"
        watts = data["value"]
        case.writes.append(watts)
        if case.fail_release and watts == 5000:
            raise RuntimeError("release rejected")
        case.reading(data["entity_id"], watts, min=10, max=5000, step=10, unit_of_measurement="W")
        if not case.ignore_feedback:
            case.reading("sensor.discharge_limit", watts, unit_of_measurement="W")

    case.coord.hass.services.async_call = AsyncMock(side_effect=service)
    monkeypatch.setattr(battery_control, "InverterGridChargeController", lambda hass, mapping:
                        InverterGridChargeController(hass, replace(mapping, timeout_seconds=.01)))
    return case


def request_hold(case, enabled=True):
    case.coord._battery_numeric_hold_requested = enabled
    case.coord._battery_numeric_hold_deadline = case.clock["now"] + timedelta(minutes=15) if enabled else None


async def cleanup(case):
    case.fail_release = False
    case.ignore_feedback = False
    await case.coord.async_stop_battery_controls()


@pytest.mark.parametrize("ordinary", [None, 100, 2000, 0])
async def test_hold_has_priority_over_every_ordinary_limit(numeric_case, ordinary):
    case = numeric_case
    request_hold(case)
    try:
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(ordinary is not None, ordinary))
        assert case.writes == [10]
        assert case.coord._battery_hold_engaged
        assert case.coord._battery_discharge_cleanup_pending
        assert case.coord._battery_journal.async_save.await_count >= 1
    finally:
        await cleanup(case)


async def test_release_uses_current_consumer_limit_without_default_pulse(numeric_case):
    case = numeric_case
    request_hold(case)
    try:
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(False))
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(True, 100))
        request_hold(case, False)
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(True, 100))
        assert case.writes == [10, 100]
        assert not case.coord._battery_hold_engaged
        assert case.coord._battery_discharge_cleanup_pending
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(False))
        assert case.writes == [10, 100, 5000]
        assert not case.coord._battery_discharge_cleanup_pending
    finally:
        await cleanup(case)


async def test_ordinary_zero_clamped_to_hold_value_also_gets_cleanup_ownership(numeric_case):
    case = numeric_case
    try:
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(True, 0))
        assert case.writes == [10]
        assert case.coord._battery_discharge_cleanup_pending
        assert not case.coord._battery_hold_engaged
        await case.coord.async_stop_battery_controls("disabled")
        assert case.writes == [10, 5000]
        assert not case.coord._battery_discharge_cleanup_pending
    finally:
        await cleanup(case)


async def test_cleanup_is_persisted_before_first_restrictive_write(numeric_case):
    case = numeric_case
    events = []
    original = case.coord.hass.services.async_call.side_effect

    async def save(*args, **kwargs):
        events.append("saved")
        assert kwargs["discharge_config"].enable_disengage_value == "5000"

    async def service(*args, **kwargs):
        events.append("write")
        assert events[0] == "saved"
        await original(*args, **kwargs)

    case.coord._battery_journal.async_save.side_effect = save
    case.coord.hass.services.async_call.side_effect = service
    try:
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(True, 0))
        assert events == ["saved", "write"]
    finally:
        case.coord._battery_journal.async_save.side_effect = None
        await cleanup(case)


async def test_unload_releases_owned_numeric_cap(numeric_case):
    case = numeric_case
    try:
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(True, 0))
        assert await case.coord.async_prepare_battery_unload()
        assert case.writes == [10, 5000]
        assert not case.coord._battery_discharge_cleanup_pending
    finally:
        await cleanup(case)


async def test_failed_release_retains_ownership_and_blocks_ordinary_overwrite(numeric_case):
    case = numeric_case
    request_hold(case)
    try:
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(False))
        case.fail_release = True
        assert not await case.coord.async_stop_battery_controls("disabled")
        assert case.coord._battery_discharge_cleanup_pending
        assert case.coord._battery_control_state == "fault"
        writes = list(case.writes)
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(True, 100))
        assert case.writes == writes
    finally:
        await cleanup(case)


async def test_hold_release_does_not_leave_stale_deduplication_value(numeric_case):
    case = numeric_case
    case.coord._last_discharge_limit = 5000
    request_hold(case)
    try:
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(False))
        request_hold(case, False)
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(False))
        assert case.writes == [10, 5000]
        assert float(case.states["sensor.discharge_limit"].state) == 5000
    finally:
        await cleanup(case)


async def test_no_restrictive_write_after_master_disabled_during_journal_save(numeric_case):
    case = numeric_case

    async def save(*args, **kwargs):
        case.coord.enabled = False

    case.coord._battery_journal.async_save.side_effect = save
    try:
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(True, 0))
        assert 10 not in case.writes
    finally:
        case.coord._battery_journal.async_save.side_effect = None
        await cleanup(case)


async def test_shared_writer_honors_existing_battery_lock(numeric_case):
    case = numeric_case
    await case.coord._battery_lock.acquire()
    task = asyncio.create_task(case.coord._apply_battery_discharge_limit(BatteryDischargeAction(True, 0)))
    try:
        await asyncio.sleep(0)
        assert case.writes == []
        assert not task.done()
    finally:
        case.coord._battery_lock.release()
        await task
        await cleanup(case)


async def test_restored_numeric_owner_uses_original_mapping_after_options_change(numeric_case):
    case = numeric_case
    original = InverterGridChargeConfig("input_number.discharge", "10", "5000",
                                       enable_feedback_entity_id="sensor.discharge_limit", timeout_seconds=.01)
    case.coord.config_entry.data["battery_max_discharge_entity"] = "number.replacement"
    case.coord._battery_journal.async_load.return_value = (None, None)
    case.coord._battery_journal.discharge_config = original
    case.coord._battery_journal.loaded_record_exists = True
    try:
        await case.coord._restore_battery_ownership()
        assert case.coord._battery_discharge_cleanup_pending
        assert await case.coord.async_stop_battery_controls("startup_recovery")
        assert case.writes == [5000]
    finally:
        await cleanup(case)


async def test_discharge_ownership_flag_is_runtime_only(numeric_case):
    from custom_components.pv_excess_control import _async_update_listener
    from custom_components.pv_excess_control.const import DOMAIN

    case = numeric_case
    entry = case.coord.config_entry
    case.coord.hass.data[DOMAIN] = {
        f"{entry.entry_id}_config_snapshot": dict(entry.data),
        f"{entry.entry_id}_subentry_count": 0,
        f"{entry.entry_id}_subentries_snapshot": {},
    }
    case.coord.hass.config_entries.async_reload = AsyncMock()
    entry.data = {**entry.data, "_battery_discharge_cleanup_pending": True}
    await _async_update_listener(case.coord.hass, entry)
    case.coord.hass.config_entries.async_reload.assert_not_awaited()


async def test_deferred_normal_release_keeps_cleanup_watchdog(numeric_case):
    case = numeric_case
    request_hold(case)
    try:
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(True, 100))
        async with case.coord._battery_lock:
            await case.coord._release_battery_locked("self_consumption", release_discharge=False)
        assert case.writes == [10]
        assert case.coord._battery_watchdog is not None
        assert not case.coord._battery_watchdog.cancelled()
    finally:
        await cleanup(case)


async def test_hold_boundary_watchdog_hands_back_to_consumer_without_default_pulse(numeric_case):
    case = numeric_case
    request_hold(case)
    case.coord._battery_numeric_hold_deadline = case.clock["now"] + timedelta(seconds=20)
    try:
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(True, 100))
        callback = case.coord._battery_watchdog._callback
        case.coord._battery_watchdog.cancel()
        case.clock["now"] += timedelta(seconds=20)
        callback()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert case.writes == [10, 100]
        assert not case.coord._battery_hold_engaged
    finally:
        await cleanup(case)


async def test_unrelated_actuator_keeps_ordinary_limit_behavior(numeric_case):
    case = numeric_case
    case.coord.config_entry.data["battery_max_discharge_entity"] = "input_number.other"
    request_hold(case)
    await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(True, 100))
    assert case.writes == [100]
    assert not case.coord._battery_discharge_cleanup_pending


async def test_numeric_shared_load_model_accepts_nonzero_kw_consumer_limit(numeric_case):
    from tests.test_battery_load_context import appliance

    case = numeric_case
    appliance(case, ev=False, watts=1000, is_big_consumer=True,
              battery_discharge_override=100, cheap_price_threshold=0)
    case.reading("input_number.discharge", 5, min=.01, max=5, step=.01, unit_of_measurement="kW")
    old = case.coord._battery_hold_ctl.config
    case.coord._battery_hold_ctl.config = replace(old, enable_engage_value=".01", enable_disengage_value="5")
    case.reading("sensor.load_power", 1000, unit_of_measurement="W")
    rows, _, error = case.coord._battery_load_context(case.clock["now"], case.tariff, [1000.] * 24)
    assert error is None
    assert any(row.max_discharge_power_w == 100 for row in rows)


async def test_failed_normal_release_journal_clear_retains_retry_ownership(numeric_case):
    case = numeric_case
    try:
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(True, 100))
        case.coord._battery_journal.async_save.side_effect = OSError("disk unavailable")
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(False))
        assert case.coord._battery_discharge_cleanup_pending
        assert case.coord._battery_discharge_owner is not None
        assert case.coord._battery_discharge_owner.config.enable_disengage_value == "5000"
        assert case.coord._battery_control_state == "fault"
    finally:
        case.coord._battery_journal.async_save.side_effect = None
        await cleanup(case)


async def test_hold_boundary_never_extends_soc_freshness_watchdog(numeric_case):
    case = numeric_case
    request_hold(case)
    case.coord._battery_numeric_hold_deadline = case.clock["now"] + timedelta(seconds=20)
    old = case.clock["now"] - timedelta(seconds=290)
    case.states["sensor.soc"] = State("sensor.soc", "40", last_updated=old, last_reported=old)
    try:
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(True, 100))
        delay = case.coord._battery_watchdog.when() - case.coord.hass.loop.time()
        assert 9 <= delay <= 10.1
    finally:
        await cleanup(case)


@pytest.mark.parametrize("change", ["soc", "pv", "load", "force", "deadline", "plan"])
async def test_hold_is_revalidated_after_ownership_save(numeric_case, change):
    case = numeric_case
    request_hold(case)

    async def save(*args, **kwargs):
        if change in {"soc", "pv", "load"}:
            case.reading({"soc": "sensor.soc", "pv": "sensor.pv_power", "load": "sensor.load_power"}[change], "unavailable")
        elif change == "force":
            case.coord.force_charge = True
        elif change == "deadline":
            case.coord._battery_numeric_hold_deadline = case.clock["now"]
        else:
            case.coord._battery_action_after_wait.side_effect = lambda *_: None

    case.coord._battery_journal.async_save.side_effect = save
    try:
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(True, 100))
        assert case.writes == [100]
        assert not case.coord._battery_hold_engaged
    finally:
        case.coord._battery_journal.async_save.side_effect = None
        await cleanup(case)


async def test_hold_revalidates_after_physical_engage_without_default_pulse(numeric_case):
    case = numeric_case
    request_hold(case)
    original = case.coord.hass.services.async_call.side_effect

    async def service(*args, **kwargs):
        await original(*args, **kwargs)
        if case.writes[-1] == 10:
            case.coord.force_charge = True

    case.coord.hass.services.async_call.side_effect = service
    try:
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(True, 100))
        assert case.writes == [10, 100]
        assert not case.coord._battery_hold_engaged
    finally:
        await cleanup(case)


async def test_numeric_hold_releases_prior_solar_cap_before_engage(numeric_case):
    case = numeric_case
    request_hold(case)
    events = []
    original = case.coord.hass.services.async_call.side_effect

    async def release_cap():
        events.append("solar_cap_released")

    async def service(*args, **kwargs):
        events.append("discharge_write")
        await original(*args, **kwargs)

    case.coord._release_solar_charge_cap = AsyncMock(side_effect=release_cap)
    case.coord.hass.services.async_call.side_effect = service
    try:
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(False))
        assert events[:2] == ["solar_cap_released", "discharge_write"]
    finally:
        await cleanup(case)


async def test_missing_soc_does_not_toggle_an_independent_consumer_cap(numeric_case):
    case = numeric_case
    try:
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(True, 0))
        case.reading("sensor.soc", "unavailable")
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(True, 0))
        assert case.writes == [10]
    finally:
        await cleanup(case)


async def test_missing_soc_ends_economic_hold_directly_at_existing_consumer_cap(numeric_case):
    case = numeric_case
    request_hold(case)
    try:
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(True, 100))
        case.reading("sensor.soc", "unavailable")
        await case.coord._battery_preflight()
        assert case.writes == [10, 100]
        assert not case.coord._battery_hold_engaged
    finally:
        await cleanup(case)


async def test_removed_hold_options_retain_owned_ordinary_caps_on_original_channel(numeric_case):
    case = numeric_case
    mapping = case.coord._battery_hold_ctl.config
    case.coord.config_entry.data = {key: value for key, value in case.coord.config_entry.data.items()
                                   if not key.startswith("battery_hold_")}
    case.coord._battery_hold_ctl = None
    case.coord._battery_discharge_template = None
    case.coord._battery_journal.async_load.return_value = (None, None)
    case.coord._battery_journal.discharge_config = None
    case.coord._battery_journal.discharge_mapping = mapping
    try:
        await case.coord._restore_battery_ownership()
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(True, 0))
        assert case.writes == [10]
        assert case.coord._battery_discharge_cleanup_pending
        assert not case.coord.battery_control_diagnostics()["battery_hold_supported"]
        await case.coord.async_stop_battery_controls("unload")
        assert case.writes == [10, 5000]
    finally:
        await cleanup(case)


async def test_real_economic_plan_defers_numeric_hold_until_shared_dispatch(numeric_case):
    case = numeric_case
    del case.coord._battery_action_after_wait
    case.reading("sensor.soc", 30)
    case.tariff.battery_charge_price_threshold = 0
    case.coord._latest_tariff = case.tariff
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.coord._current_battery_slot().action == "hold"
        assert case.coord._battery_numeric_hold_requested
        assert case.writes == []
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(False))
        assert case.writes == [10]
        assert case.coord._battery_control_state == "holding"
    finally:
        await cleanup(case)


async def test_preserved_capability_does_not_authorize_hold_on_changed_mapping(numeric_case):
    case = numeric_case
    case.coord._battery_discharge_template = case.coord._battery_hold_ctl
    case.coord.config_entry.data["battery_hold_entity"] = "input_number.other_hold"
    request_hold(case)
    try:
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(True, 100))
        assert case.writes == [100]
        assert not case.coord._battery_hold_engaged
    finally:
        await cleanup(case)


async def test_native_number_uses_own_kw_feedback_for_shared_hold_and_release(numeric_case):
    case = numeric_case
    entity = "number.native_discharge"
    case.coord.config_entry.data.update(battery_max_discharge_entity=entity, battery_hold_entity=entity,
                                        battery_max_discharge_default=None)
    attributes = {"min": .01, "max": 5, "step": .01, "unit_of_measurement": "kW"}
    case.reading(entity, 5, **attributes)
    mapping = InverterGridChargeConfig(entity, ".01", "5", timeout_seconds=.01)
    case.coord._battery_hold_ctl = InverterGridChargeController(case.coord.hass, mapping)

    async def service(domain, name, data, **kwargs):
        assert domain == "number" and name == "set_value"
        case.writes.append(data["value"])
        case.reading(entity, data["value"], **attributes)

    case.coord.hass.services.async_call.side_effect = service
    request_hold(case)
    try:
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(False))
        assert case.writes == [.01]
        assert case.coord._battery_hold_engaged
        request_hold(case, False)
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(True, 100))
        assert case.writes == [.01, .1]
        await case.coord.async_stop_battery_controls("unload")
        assert case.writes == [.01, .1, 5]
    finally:
        await cleanup(case)


@pytest.mark.parametrize("missing", [True, False])
async def test_optional_default_uses_validated_release_value(numeric_case, missing):
    case = numeric_case
    if missing:
        case.coord.config_entry.data.pop("battery_max_discharge_default")
    else:
        case.coord.config_entry.data["battery_max_discharge_default"] = None
    request_hold(case)
    try:
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(False))
        assert case.writes == [10]
        request_hold(case, False)
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(False))
        assert case.writes == [10, 5000]
    finally:
        await cleanup(case)


async def test_explicit_zero_default_is_not_replaced_with_release_value(numeric_case):
    case = numeric_case
    case.coord.config_entry.data["battery_max_discharge_default"] = 0
    try:
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(False))
        assert case.writes == [10]
    finally:
        await cleanup(case)


async def test_active_consumer_limit_overrides_missing_default(numeric_case):
    case = numeric_case
    case.coord.config_entry.data["battery_max_discharge_default"] = None
    try:
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(True, 100))
        assert case.writes == [100]
    finally:
        await cleanup(case)


async def test_recovery_clears_old_owner_before_using_changed_release_on_same_entity(numeric_case):
    case = numeric_case
    original = case.coord._battery_hold_ctl.config
    current = replace(original, enable_disengage_value="4000")
    case.coord._battery_hold_ctl = InverterGridChargeController(case.coord.hass, current)
    case.coord.config_entry.data.update(battery_max_discharge_default=4000,
                                        battery_hold_release_value="4000")
    case.coord._battery_journal.async_load.return_value = (None, None)
    case.coord._battery_journal.discharge_config = original
    case.coord._battery_journal.loaded_record_exists = True
    try:
        await case.coord._restore_battery_ownership()
        assert await case.coord.async_stop_battery_controls("startup_recovery")
        assert case.writes == [5000]
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(False))
        assert case.writes == [5000, 4000]
        assert case.coord._last_discharge_limit == 4000
        assert case.coord._battery_discharge_owner is None
    finally:
        await cleanup(case)
