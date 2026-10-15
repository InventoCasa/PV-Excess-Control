"""Expensive pure searches yield while only fresh decisions reach controls."""
import asyncio
from dataclasses import replace
from datetime import timedelta
import threading

import pytest

from custom_components.pv_excess_control import battery_planner
from tests.test_battery_control_economics import economic_case  # noqa: F401
from tests.test_battery_numeric_hold import cleanup, numeric_case, request_hold  # noqa: F401
from custom_components.pv_excess_control.models import BatteryDischargeAction


@pytest.fixture
def blocked_search(economic_case, monkeypatch):
    case = economic_case([.1, .4], [0, 0], [0, 2000])
    entered, release = threading.Event(), threading.Event()
    original = battery_planner.build_battery_grid_plan
    calls = []
    main_thread = threading.get_ident()

    def search(*args, **kwargs):
        calls.append((threading.get_ident(), args, kwargs))
        if len(calls) == 1:
            entered.set()
            if not release.wait(3):
                raise RuntimeError("worker was not released")
        return original(*args, **kwargs)

    async def executor(function, *args):
        return await asyncio.to_thread(function, *args)

    case.coord.hass.async_add_executor_job = executor
    monkeypatch.setattr(battery_planner, "build_battery_grid_plan", search)
    yield case, entered, release, calls, main_thread
    release.set()
    case.coord._cancel_battery_watchdog()


async def wait_until_entered(event):
    assert await asyncio.to_thread(event.wait, 1)


async def test_search_runs_outside_event_loop_and_preserves_cached_target(blocked_search):
    case, entered, release, calls, main_thread = blocked_search
    task = asyncio.create_task(case.coord._async_refresh_battery_grid_plan(case.tariff, None))
    try:
        await wait_until_entered(entered)
        assert not task.done()
        assert calls[0][0] != main_thread
    finally:
        release.set()
        assert await task is None
    plan = case.coord._battery_grid_plan
    case.reading("sensor.soc", 11)
    assert await case.coord._async_refresh_battery_grid_plan(case.tariff, None) is None
    assert len(calls) == 1
    assert case.coord._battery_grid_plan is plan


@pytest.mark.parametrize("change", ["disable", "unload", "manual", "missing_soc", "stale_soc", "price", "storage"])
async def test_changed_request_or_invalid_input_never_reaches_hardware(blocked_search, change):
    case, entered, release, calls, _ = blocked_search
    task = asyncio.create_task(case.coord._run_grid_charge_state_machine(case.tariff, None))
    try:
        await wait_until_entered(entered)
        if change == "disable":
            case.coord.enabled = False
        elif change == "unload":
            case.coord._battery_unloading = True
        elif change == "manual":
            case.coord.force_charge = True
        elif change == "missing_soc":
            case.reading("sensor.soc", "unavailable")
        elif change == "stale_soc":
            case.clock["now"] += timedelta(seconds=301)
        elif change == "storage":
            case.coord._battery_storage_ready = False
        else:
            case.reading("sensor.price", .3)
    finally:
        release.set()
        await task
    assert case.coord._battery_grid_plan is None
    case.controller.engage.assert_not_awaited()
    case.controller.disengage.assert_not_awaited()
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["soc", "configuration", "load", "forecast", "tariffs", "deadline"])
async def test_material_change_retries_once_with_detached_fresh_inputs(blocked_search, change):
    case, entered, release, calls, _ = blocked_search
    task = asyncio.create_task(case.coord._async_refresh_battery_grid_plan(case.tariff, None))
    try:
        await wait_until_entered(entered)
        if change == "soc":
            case.reading("sensor.soc", 20)
        elif change == "configuration":
            case.coord.config_entry.data["battery_grid_charge_power_w"] = 1000
        elif change == "load":
            case.reading("sensor.load_power", 3000, unit_of_measurement="W")
        elif change == "forecast":
            case.coord._forecast_data.hourly_breakdown[1].expected_kwh = 1
        elif change == "tariffs":
            case.tariff.windows[1] = replace(case.tariff.windows[1], price=.5)
        else:
            case.coord._battery_retry_after = case.clock["now"] + timedelta(minutes=5)
    finally:
        release.set()
        assert await task is None
    assert len(calls) == 2
    assert case.coord._battery_grid_plan.valid
    if change == "soc":
        assert [call[1][1] for call in calls] == [10, 20]
    if change == "forecast":
        assert calls[0][1][3][1].expected_kwh == 0
        assert calls[1][1][3][1].expected_kwh == 1
    if change == "tariffs":
        assert calls[0][1][2][1].price == .4
        assert calls[1][1][2][1].price == .5
    if change == "deadline":
        assert all(s.start >= case.coord._battery_retry_after for s in case.coord._battery_grid_plan.slots if s.action == "charge")


async def test_repeated_material_changes_fail_closed_after_two_searches(economic_case):
    case = economic_case([.1, .4], [0, 0], [0, 2000])
    calls = 0
    async def executor(function, *args):
        nonlocal calls
        result = await asyncio.to_thread(function, *args)
        calls += 1
        case.reading("sensor.soc", 10 + calls)
        return result
    case.coord.hass.async_add_executor_job = executor
    assert await case.coord._async_refresh_battery_grid_plan(case.tariff, None) == "planning_input_changed"
    assert calls == 2
    assert case.coord._battery_grid_plan is None


async def test_expired_first_interval_is_rebuilt_before_dispatch(blocked_search):
    case, entered, release, calls, _ = blocked_search
    case.coord._battery_retry_after = case.clock["now"] + timedelta(seconds=1)
    task = asyncio.create_task(case.coord._async_refresh_battery_grid_plan(case.tariff, None))
    try:
        await wait_until_entered(entered)
        case.clock["now"] += timedelta(seconds=2)
        for name, state in list(case.states.items()):
            case.reading(name, state.state, **state.attributes)
    finally:
        release.set()
        assert await task is None
    assert len(calls) == 2
    assert case.coord._battery_grid_plan.slots[0].start == case.clock["now"]


async def test_cancelled_worker_cannot_later_publish_a_plan(blocked_search):
    case, entered, release, _, _ = blocked_search
    task = asyncio.create_task(case.coord._async_refresh_battery_grid_plan(case.tariff, None))
    try:
        await wait_until_entered(entered)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    await asyncio.sleep(0)
    assert case.coord._battery_grid_plan is None
    case.controller.engage.assert_not_awaited()


async def test_latest_provider_replacement_is_not_reverted_by_followup_validation(blocked_search):
    case, entered, release, calls, _ = blocked_search
    task = asyncio.create_task(case.coord._async_refresh_battery_grid_plan(case.tariff, None))
    try:
        await wait_until_entered(entered)
        case.coord._latest_tariff = replace(
            case.tariff, windows=[case.tariff.windows[0], replace(case.tariff.windows[1], price=.5)],
        )
    finally:
        release.set()
        assert await task is None
    plan = case.coord._battery_grid_plan
    assert len(calls) == 2
    assert await case.coord._async_refresh_battery_grid_plan(case.tariff, None) is None
    assert case.coord._battery_grid_plan is plan
    assert len(calls) == 2


async def test_failed_worker_releases_existing_owned_charge(economic_case):
    case = economic_case([.1, .4], [0, 0], [0, 2000])
    async def failed(*_):
        raise RuntimeError("executor unavailable")
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.coord._grid_charge_engaged
        case.coord._battery_plan_key = None
        case.coord.hass.async_add_executor_job = failed
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.coord._battery_grid_plan is None
        assert not case.coord._grid_charge_engaged
        assert case.coord._battery_control_reason == "planning_failed"
        case.controller.disengage.assert_awaited_once()
    finally:
        await case.coord.async_stop_battery_controls()


async def test_cancelled_replan_arms_cleanup_for_existing_owned_charge(economic_case):
    case = economic_case([.1, .4], [0, 0], [0, 2000])
    entered = asyncio.Event()
    async def blocked(*_):
        entered.set()
        await asyncio.Event().wait()
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        case.coord._battery_plan_key = None
        case.coord.hass.async_add_executor_job = blocked
        task = asyncio.create_task(case.coord._run_grid_charge_state_machine(case.tariff, None))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert case.coord._battery_recovery
        callback = case.coord._battery_watchdog._callback
        case.coord._battery_watchdog.cancel()
        callback()
        await asyncio.sleep(0)
        assert not case.coord._grid_charge_engaged
        case.controller.disengage.assert_awaited_once()
    finally:
        await case.coord.async_stop_battery_controls()


@pytest.mark.parametrize("stop", ["disable", "unload", "watchdog"])
async def test_owned_stop_does_not_wait_for_blocked_planner_thread(economic_case, monkeypatch, stop):
    case = economic_case([.1, .4], [0, 0], [0, 2000])
    entered, release = threading.Event(), threading.Event()
    stopped = asyncio.Event()
    original = battery_planner.build_battery_grid_plan
    async def confirm_stop():
        stopped.set()
    def blocked(*args, **kwargs):
        entered.set()
        if not release.wait(3):
            raise RuntimeError("worker was not released")
        return original(*args, **kwargs)
    await case.coord._run_grid_charge_state_machine(case.tariff, None)
    assert case.coord._grid_charge_engaged
    case.controller.disengage.side_effect = confirm_stop
    monkeypatch.setattr(battery_planner, "build_battery_grid_plan", blocked)
    case.coord._battery_plan_key = None
    replan = asyncio.create_task(case.coord._run_grid_charge_state_machine(case.tariff, None))
    stop_task = None
    try:
        await wait_until_entered(entered)
        if stop == "disable":
            case.coord.enabled = False
            stop_task = asyncio.create_task(case.coord.async_stop_battery_controls("disabled"))
        elif stop == "unload":
            stop_task = asyncio.create_task(case.coord.async_prepare_battery_unload())
        else:
            callback = case.coord._battery_watchdog._callback
            case.coord._battery_watchdog.cancel()
            callback()
        await asyncio.wait_for(stopped.wait(), .5)
        assert not release.is_set()
        await replan
        assert not case.coord._grid_charge_engaged
        assert case.coord._battery_grid_plan is None
    finally:
        release.set()
        await replan
        if stop_task is not None:
            await stop_task
        await case.coord.async_stop_battery_controls()
    case.controller.disengage.assert_awaited_once()


@pytest.mark.parametrize("stop", ["missing_soc", "hold_boundary"])
async def test_shared_hold_safety_restores_consumer_cap_before_worker_finishes(numeric_case, monkeypatch, stop):
    case = numeric_case
    entered, release = threading.Event(), threading.Event()
    restored = asyncio.Event()
    request_hold(case)
    case.coord._battery_numeric_hold_deadline = case.clock["now"] + timedelta(seconds=20)
    ordinary = BatteryDischargeAction(True, 100)
    await case.coord._apply_battery_discharge_limit(ordinary)
    callback = case.coord._battery_watchdog._callback
    assert case.writes == [10]
    original_search = battery_planner.build_battery_grid_plan
    original_service = case.coord.hass.services.async_call.side_effect
    def blocked(*args, **kwargs):
        entered.set()
        if not release.wait(3):
            raise RuntimeError("worker was not released")
        return original_search(*args, **kwargs)
    async def service(domain, name, data, **kwargs):
        await original_service(domain, name, data, **kwargs)
        if data["value"] == 100:
            restored.set()
    case.coord.hass.services.async_call.side_effect = service
    monkeypatch.setattr(battery_planner, "build_battery_grid_plan", blocked)
    del case.coord._battery_action_after_wait
    case.coord._latest_tariff = case.tariff
    replan = asyncio.create_task(case.coord._apply_battery_discharge_limit(ordinary))
    preflight = None
    try:
        await wait_until_entered(entered)
        if stop == "missing_soc":
            case.reading("sensor.soc", "unavailable")
            preflight = asyncio.create_task(case.coord._battery_preflight())
        else:
            case.clock["now"] = case.coord._battery_numeric_hold_deadline
            case.coord._battery_watchdog.cancel()
            callback()
        await asyncio.wait_for(restored.wait(), .5)
        assert not release.is_set()
        await replan
        assert case.writes == [10, 100]
        assert not case.coord._battery_hold_engaged
    finally:
        release.set()
        await replan
        if preflight is not None:
            await preflight
        await cleanup(case)
