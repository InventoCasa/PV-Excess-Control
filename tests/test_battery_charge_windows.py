"""An authorized charge episode survives safe subslot handoffs, never expiry."""
import asyncio
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.pv_excess_control.battery_planner import BatteryGridPlan, BatteryGridSlot
from custom_components.pv_excess_control.models import BatteryDischargeAction, InverterGridChargeConfig
from tests.test_battery_control_economics import economic_case  # noqa: F401


def install_charge_plan(case, *, actions=("charge", "charge", "self_consumption"), seconds=20, powers=None):
    start = case.clock["now"]
    powers = powers or [1800] * len(actions)
    slots = tuple(BatteryGridSlot(
        start + timedelta(seconds=seconds * i), start + timedelta(seconds=seconds * (i + 1)),
        .1, 0., 0., powers[i] * seconds / 3_600_000 if action == "charge" else 0.,
        0., 0., 11. + i, action,
    ) for i, action in enumerate(actions))
    case.coord._battery_grid_plan = BatteryGridPlan(slots, .02, .01, "economic_plan", 12.)
    case.coord._async_refresh_battery_grid_plan = AsyncMock(return_value=None)
    case.coord._latest_tariff = case.tariff
    return slots


async def fire_watchdog(case):
    callback = case.coord._battery_watchdog._callback
    case.coord._battery_watchdog.cancel()
    callback()
    for _ in range(10):
        await asyncio.sleep(0)


async def test_adjacent_same_power_charge_continues_without_stop_or_restart_delay(economic_case):
    case = economic_case([.1, .4], [0, 0], [0, 2000], battery_grid_charge_minimum_minutes=0)
    slots = install_charge_plan(case)
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        case.clock["now"] = slots[0].end
        await fire_watchdog(case)
        assert case.coord._grid_charge_engaged
        case.controller.disengage.assert_not_awaited()
        assert case.coord._battery_retry_after is None
        assert case.coord._battery_charge_deadline == slots[1].end
        case.clock["now"] = slots[1].end
        await fire_watchdog(case)
        assert not case.coord._grid_charge_engaged
        case.controller.disengage.assert_awaited_once()
    finally:
        await case.coord.async_stop_battery_controls()


async def test_shared_cap_rearm_cannot_extend_expired_charge_into_native_slot(economic_case):
    case = economic_case([.1, .4], [0, 0], [0, 2000], battery_grid_charge_minimum_minutes=0)
    slots = install_charge_plan(case, actions=("charge", "self_consumption"))
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        callback = case.coord._battery_watchdog._callback
        case.coord._battery_watchdog.cancel()
        async with case.coord._battery_lock:
            case.clock["now"] = slots[0].end + timedelta(seconds=.1)
            callback()
            case.coord._arm_shared_discharge_watchdog()
            await asyncio.sleep(0)
        for _ in range(10):
            await asyncio.sleep(0)
        assert not case.coord._grid_charge_engaged
        case.controller.disengage.assert_awaited_once()
    finally:
        await case.coord.async_stop_battery_controls()


async def test_shared_rearm_cannot_authorize_new_later_plan_deadline(economic_case):
    case = economic_case([.1, .4], [0, 0], [0, 2000], battery_grid_charge_minimum_minutes=0)
    slots = install_charge_plan(case, actions=("charge", "self_consumption"))
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        old_end = slots[0].end
        case.coord._battery_grid_plan = replace(case.coord._battery_grid_plan,
            slots=(replace(slots[0], end=old_end + timedelta(minutes=10)),))
        case.coord._arm_shared_discharge_watchdog()
        assert case.coord._battery_charge_deadline == old_end
        assert case.coord._battery_watchdog.when() - case.coord.hass.loop.time() <= 20.1
    finally:
        await case.coord.async_stop_battery_controls()


async def test_continuation_target_is_episode_final_soc_not_first_subslot(economic_case):
    case = economic_case([.1, .4], [0, 0], [0, 2000], battery_grid_charge_minimum_minutes=0)
    slots = install_charge_plan(case)
    case.reading("sensor.soc", 11)
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.coord._grid_charge_engaged
        case.controller.engage.assert_awaited_once()
        assert case.coord._battery_charge_deadline == slots[1].end
    finally:
        await case.coord.async_stop_battery_controls()


@pytest.mark.parametrize("seconds", [30, 299])
async def test_new_short_charge_is_rejected_before_any_hardware_write(economic_case, seconds):
    case = economic_case([.1, .4], [0, 0], [0, 2000], battery_grid_charge_minimum_minutes=5)
    install_charge_plan(case, actions=("charge", "self_consumption"), seconds=seconds)
    case.controller.prepare = AsyncMock()
    case.coord._battery_plan_key = ("previously_executable",)
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.coord._battery_grid_plan is None
        assert case.coord._battery_plan_key is None
        case.controller.prepare.assert_not_awaited()
        case.controller.engage.assert_not_awaited()
        case.controller.disengage.assert_not_awaited()
        assert not case.coord._grid_charge_cleanup_pending
    finally:
        await case.coord.async_stop_battery_controls()


async def test_already_running_episode_does_not_require_five_more_minutes(economic_case):
    case = economic_case([.1, .4], [0, 0], [0, 2000], battery_grid_charge_minimum_minutes=0)
    slots = install_charge_plan(case)
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        case.coord.config_entry.data["battery_grid_charge_minimum_minutes"] = 5
        case.clock["now"] = slots[0].end
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.coord._grid_charge_engaged
        case.controller.disengage.assert_not_awaited()
    finally:
        await case.coord.async_stop_battery_controls()


def preparation_case(factory, *, seconds=400, available_seconds=.2):
    case = factory([.1, .4], [0, 0], [0, 2000], battery_grid_charge_minimum_minutes=5)
    case.controller.config = InverterGridChargeConfig("select.command", "Charge", "Stop",
        mode_entity_id="select.mode", mode_engage_value="Forced", mode_disengage_value="Self",
        power_entity_id="number.charge_power", timeout_seconds=.1)
    slots = install_charge_plan(case, actions=("charge", "self_consumption"), seconds=seconds)
    available = case.clock["now"] + timedelta(seconds=available_seconds)
    first = replace(slots[0], grid_charge_available_at=available,
                    grid_charge_kwh=1800 * (seconds - available_seconds) / 3_600_000)
    case.coord._battery_grid_plan = replace(case.coord._battery_grid_plan, slots=(first, slots[1]))
    case.wait_entered = asyncio.Event()
    case.ready = asyncio.Event()
    async def wait(when):
        case.wait_entered.set()
        await case.ready.wait()
        case.clock["now"] = when
    case.coord._wait_battery_charge_ready = wait
    return case, available


async def test_prepare_waits_for_planned_enable_and_revalidates_ready_state(economic_case):
    case, available = preparation_case(economic_case)
    prepared_seen = []
    async def refresh(*_):
        prepared_seen.append(case.coord._battery_charge_prepared)
        return None
    case.coord._async_refresh_battery_grid_plan.side_effect = refresh
    task = asyncio.create_task(case.coord._run_grid_charge_state_machine(case.tariff, None))
    try:
        await asyncio.wait_for(case.wait_entered.wait(), .5)
        assert case.coord._battery_control_state == "preparing"
        case.controller.prepare.assert_awaited_once_with(1800)
        case.controller.enable_prepared.assert_not_awaited()
        case.controller.engage.assert_not_awaited()
        assert case.coord._grid_charge_cleanup_pending
        case.ready.set()
        await task
        assert case.clock["now"] >= available
        assert True in prepared_seen
        case.controller.enable_prepared.assert_awaited_once()
        assert case.controller.enable_prepared.await_args.args == (1800,)
        assert case.coord._grid_charge_engaged
        assert not case.coord._battery_charge_prepared
    finally:
        case.ready.set()
        await task
        await case.coord.async_stop_battery_controls()


@pytest.mark.parametrize("stop", ["disable", "unload", "watchdog"])
async def test_preparation_wait_is_interruptible_without_enabling_grid_charge(economic_case, stop):
    case, _ = preparation_case(economic_case)
    task = asyncio.create_task(case.coord._run_grid_charge_state_machine(case.tariff, None))
    stopper = None
    try:
        await asyncio.wait_for(case.wait_entered.wait(), .5)
        if stop == "disable":
            case.coord.enabled = False
            stopper = asyncio.create_task(case.coord.async_stop_battery_controls("disabled"))
        elif stop == "unload":
            stopper = asyncio.create_task(case.coord.async_prepare_battery_unload())
        else:
            await fire_watchdog(case)
        await asyncio.wait_for(task, .5)
        if stopper:
            await stopper
        assert not case.ready.is_set()
        case.controller.enable_prepared.assert_not_awaited()
        case.controller.disengage.assert_awaited_once()
        assert not case.coord._grid_charge_cleanup_pending
    finally:
        case.ready.set()
        await asyncio.gather(task, return_exceptions=True)
        await case.coord.async_stop_battery_controls()


async def test_expired_five_minute_ready_window_never_enables(economic_case):
    case, _ = preparation_case(economic_case)
    async def wait(_):
        case.clock["now"] += timedelta(seconds=101)
    case.coord._wait_battery_charge_ready = wait
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        case.controller.prepare.assert_awaited_once()
        case.controller.enable_prepared.assert_not_awaited()
        case.controller.disengage.assert_awaited_once()
    finally:
        await case.coord.async_stop_battery_controls()


async def test_enable_confirmation_reserve_is_included_before_preparation(economic_case):
    case, _ = preparation_case(economic_case, seconds=330, available_seconds=30)
    case.controller.config = replace(case.controller.config, timeout_seconds=15)
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        case.controller.prepare.assert_not_awaited()
        case.controller.enable_prepared.assert_not_awaited()
        case.controller.disengage.assert_not_awaited()
    finally:
        await case.coord.async_stop_battery_controls()


async def test_obsolete_numeric_hold_callback_does_not_abort_new_preparation(economic_case):
    case, _ = preparation_case(economic_case)
    mapping = InverterGridChargeConfig("input_number.discharge", "10", "5000",
                                      enable_feedback_entity_id="sensor.discharge_limit")
    hold = MagicMock(config=mapping, confirmation_level="physical", disengage=AsyncMock())
    case.coord._battery_hold_ctl = hold
    case.coord._battery_discharge_owner = hold
    case.coord._battery_discharge_cleanup_pending = True
    case.coord._battery_hold_engaged = True
    case.coord._battery_numeric_hold_deadline = case.clock["now"] + timedelta(seconds=20)
    case.coord._battery_discharge_last_action = BatteryDischargeAction(True, 100)
    case.coord.config_entry.data.update(battery_max_discharge_entity="input_number.discharge",
        battery_hold_entity="input_number.discharge", battery_hold_verified=True)
    case.coord._arm_shared_discharge_watchdog()
    old_callback = case.coord._battery_watchdog._callback
    task = asyncio.create_task(case.coord._run_grid_charge_state_machine(case.tariff, None))
    try:
        await asyncio.wait_for(case.wait_entered.wait(), .5)
        old_callback()
        for _ in range(5):
            await asyncio.sleep(0)
        assert not case.coord._battery_charge_prepare_abort.is_set()
        case.controller.disengage.assert_not_awaited()
        case.ready.set()
        await task
        assert case.coord._grid_charge_engaged
        case.controller.enable_prepared.assert_awaited_once()
    finally:
        case.ready.set()
        await asyncio.gather(task, return_exceptions=True)
        await case.coord.async_stop_battery_controls()


def test_plan_diagnostics_distinguish_setup_from_actual_charge(economic_case):
    case, available = preparation_case(economic_case)
    plan = case.coord._battery_grid_plan
    first = replace(plan.slots[0], startup_pv_loss_kwh=.01)
    case.coord._battery_grid_plan = replace(plan, slots=(first, plan.slots[1]))
    data = case.coord.battery_control_diagnostics()
    assert data["battery_grid_charge_minimum_minutes"] == 5
    assert data["battery_plan"][0]["grid_charge_available_at"] == available.isoformat()
    assert data["battery_plan"][0]["startup_pv_loss_kwh"] == .01
    assert "grid_charge_available_at" not in data["battery_plan"][1]
    assert "startup_pv_loss_kwh" not in data["battery_plan"][1]


@pytest.mark.parametrize("blocked", ["planner", "power"])
async def test_boundary_handoff_keeps_hard_deadline_while_work_is_blocked(economic_case, blocked):
    case = economic_case([.1, .4], [0, 0], [0, 2000], battery_grid_charge_minimum_minutes=0)
    slots = install_charge_plan(case, powers=[1800, 900, 0])
    entered = asyncio.Event()
    finish = asyncio.Event()
    stopped = asyncio.Event()
    async def delayed(*args):
        entered.set()
        await finish.wait()
        return args[0]() if blocked == "planner" else None
    async def released():
        stopped.set()
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        case.controller.disengage.side_effect = released
        if blocked == "planner":
            del case.coord._async_refresh_battery_grid_plan
            case.coord._battery_plan_key = None
            case.coord.hass.async_add_executor_job = delayed
        else:
            case.controller.set_power.side_effect = delayed
        case.clock["now"] = slots[0].end
        await fire_watchdog(case)
        await asyncio.wait_for(entered.wait(), .5)
        assert case.coord._battery_watchdog is not None
        assert case.coord._battery_watchdog.when() - case.coord.hass.loop.time() <= 20.1
        case.clock["now"] = slots[1].end
        await fire_watchdog(case)
        await asyncio.wait_for(stopped.wait(), .5)
        assert not finish.is_set()
        assert not case.coord._grid_charge_engaged
    finally:
        finish.set()
        await case.coord.async_stop_battery_controls()


@pytest.mark.parametrize("change", ["power", "price", "soc", "missing_load"])
async def test_active_confirmation_revalidates_changed_limits_and_inputs(economic_case, change):
    case = economic_case([.1, .4], [3, 0], [0, 4000], battery_grid_charge_minimum_minutes=0,
                         battery_grid_charge_power_w=5000, battery_max_charge_power_w=6570,
                         battery_max_discharge_default=5000)
    case.reading("sensor.pv_power", 0, unit_of_measurement="W")
    changed = False
    async def verify(_):
        nonlocal changed
        if changed:
            return
        changed = True
        if change == "power":
            case.coord.config_entry.data = {**case.coord.config_entry.data, "battery_grid_charge_power_w": 1000}
        elif change == "price":
            case.coord.config_entry.data = {**case.coord.config_entry.data, "battery_charge_price_threshold": .05}
        elif change == "soc":
            case.reading("sensor.soc", 100)
        else:
            case.reading("sensor.load_power", "unavailable")
        await asyncio.sleep(0)
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.coord._grid_charge_engaged
        case.controller.verify_engaged.side_effect = verify
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        if change == "power":
            assert case.coord._grid_charge_engaged
            assert case.coord._battery_charge_power <= 1000
            case.controller.set_power.assert_awaited()
            case.controller.disengage.assert_not_awaited()
        else:
            assert not case.coord._grid_charge_engaged
            case.controller.disengage.assert_awaited_once()
    finally:
        await case.coord.async_stop_battery_controls()


async def test_confirmed_minimum_deadline_is_not_renewed_by_active_updates(economic_case):
    case, available = preparation_case(economic_case)
    async def enabled(_, *, before_enable=None):
        await before_enable()
        case.clock["now"] += timedelta(seconds=.1)
    case.controller.enable_prepared.side_effect = enabled
    case.ready.set()
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        deadline = available + timedelta(seconds=.1, minutes=5)
        assert case.coord._battery_charge_minimum_until == deadline
        for _ in range(3):
            case.clock["now"] += timedelta(seconds=30)
            await case.coord._run_grid_charge_state_machine(case.tariff, None)
            assert case.coord._battery_charge_minimum_until == deadline
            assert case.coord._grid_charge_engaged
        case.controller.enable_prepared.assert_awaited_once()
        await case.coord.async_stop_battery_controls("disabled")
        assert case.coord._battery_charge_minimum_until is None
    finally:
        await case.coord.async_stop_battery_controls()


@pytest.mark.parametrize("confirmed_soc", [10, 13])
async def test_confirmed_stop_keeps_original_target_and_reason_through_idle_updates(economic_case, confirmed_soc):
    case = economic_case([.1, .4], [0, 0], [0, 2000], battery_grid_charge_minimum_minutes=0)
    slots = install_charge_plan(case)
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.coord._grid_charge_engaged
        original_target = slots[1].soc_end
        case.coord._battery_grid_plan = replace(case.coord._battery_grid_plan,
            slots=(replace(slots[0], action="self_consumption", grid_charge_kwh=0, soc_end=0),))
        async def confirm():
            case.clock["now"] += timedelta(seconds=1)
            case.reading("sensor.soc", confirmed_soc)
        case.controller.disengage.side_effect = confirm
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        stopped = case.coord.battery_control_diagnostics()["battery_last_charge_stop"]
        assert stopped["reason"] == "plan_changed"
        assert stopped["target_soc"] == original_target
        assert stopped["soc_at_request"] == 10
        assert stopped["soc"] == confirmed_soc
        assert stopped["authorized_until"] == slots[1].end.isoformat()
        case.clock["now"] += timedelta(seconds=30)
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.coord.battery_control_diagnostics()["battery_last_charge_stop"] == stopped
    finally:
        await case.coord.async_stop_battery_controls()


async def test_real_planner_rolling_updates_keep_a_useful_charge_with_quantized_soc(economic_case):
    import math
    case = economic_case([.1, .4], [0, 0], [0, 2000], battery_grid_charge_minimum_minutes=5,
                         battery_max_discharge_default=5000)
    case.controller.config = InverterGridChargeConfig("select.command", "Charge", "Stop",
        mode_entity_id="select.mode", mode_engage_value="Forced", mode_disengage_value="Self",
        power_entity_id="number.charge_power", timeout_seconds=15)
    async def ready(when):
        case.clock["now"] = when
    async def enabled(_, *, before_enable=None):
        await before_enable()
        case.clock["now"] += timedelta(seconds=15)
    case.coord._wait_battery_charge_ready = ready
    case.controller.enable_prepared.side_effect = enabled
    true_soc = 10.
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.coord._grid_charge_engaged
        deadline = case.coord._battery_charge_minimum_until
        assert deadline == case.clock["now"] + timedelta(minutes=5)
        for _ in range(11):
            true_soc += case.coord._battery_charge_power * 30 / 3_600_000 * math.sqrt(.85) / 10 * 100
            case.clock["now"] += timedelta(seconds=30)
            for name, state in list(case.states.items()):
                case.reading(name, state.state, **state.attributes)
            case.reading("sensor.soc", math.floor(true_soc))
            # Force genuine pure-planner reevaluation, not a cached-plan pass.
            case.coord._battery_plan_key = None
            await case.coord._run_grid_charge_state_machine(case.tariff, None)
            assert case.coord._grid_charge_engaged
            assert case.coord._battery_charge_minimum_until == deadline
            assert case.coord._battery_charge_deadline > case.clock["now"]
        case.controller.enable_prepared.assert_awaited_once()
        case.controller.disengage.assert_not_awaited()
    finally:
        await case.coord.async_stop_battery_controls()


async def test_fresh_higher_target_supersedes_previous_authorization(economic_case):
    case = economic_case([.1, .4], [0, 0], [0, 2000], battery_grid_charge_minimum_minutes=0)
    slots = install_charge_plan(case)
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.coord._battery_charge_target_soc == 12
        case.reading("sensor.soc", 13)
        case.coord._battery_grid_plan = replace(case.coord._battery_grid_plan,
            slots=tuple(replace(row, soc_end=20) if row.action == "charge" else row for row in slots))
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.coord._grid_charge_engaged
        case.controller.disengage.assert_not_awaited()
        assert case.coord._battery_charge_target_soc == 20
    finally:
        await case.coord.async_stop_battery_controls()


async def test_soft_boundary_preserves_overlapping_active_replan_until_hard_expiry(economic_case):
    case = economic_case([.1, .4], [0, 0], [0, 2000], battery_grid_charge_minimum_minutes=0)
    slots = install_charge_plan(case)
    entered, finish, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
    periodic = None
    async def worker(job):
        entered.set()
        await finish.wait()
        return job()
    async def released():
        stopped.set()
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        case.controller.disengage.side_effect = released
        del case.coord._async_refresh_battery_grid_plan
        case.coord._battery_plan_key = None
        case.coord.hass.async_add_executor_job = worker
        case.clock["now"] = slots[0].end - timedelta(seconds=1)
        periodic = asyncio.create_task(case.coord._run_grid_charge_state_machine(case.tariff, None))
        await asyncio.wait_for(entered.wait(), .5)
        case.clock["now"] = slots[0].end
        await fire_watchdog(case)
        case.controller.disengage.assert_not_awaited()
        assert case.coord._grid_charge_engaged
        assert case.coord._battery_charge_deadline == slots[1].end
        assert case.coord._battery_watchdog is not None
        case.clock["now"] = slots[1].end
        await fire_watchdog(case)
        await asyncio.wait_for(stopped.wait(), .5)
        assert not finish.is_set()
        assert not case.coord._grid_charge_engaged
    finally:
        finish.set()
        if periodic is not None:
            await asyncio.gather(periodic, return_exceptions=True)
        await case.coord.async_stop_battery_controls()


@pytest.mark.parametrize("crossing", ["queued", "planning", "confirmation"])
async def test_expired_authorization_cannot_be_renewed_by_waiting_update(economic_case, crossing):
    case = economic_case([.1, .4], [0, 0], [0, 2000], battery_grid_charge_minimum_minutes=0)
    slots = install_charge_plan(case, actions=("charge", "self_consumption"))
    periodic = None
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        old_end = slots[0].end
        case.coord._battery_grid_plan = replace(case.coord._battery_grid_plan,
            slots=(replace(slots[0], end=old_end + timedelta(minutes=10)),))
        if crossing == "queued":
            callback = case.coord._battery_watchdog._callback
            case.coord._battery_watchdog.cancel()
            async with case.coord._battery_lock:
                periodic = asyncio.create_task(case.coord._run_grid_charge_state_machine(case.tariff, None))
                await asyncio.sleep(0)
                case.clock["now"] = old_end
                callback()
                await asyncio.sleep(0)
            await periodic
        else:
            async def advance(*_):
                case.clock["now"] = old_end
                await asyncio.sleep(0)
            if crossing == "planning":
                case.coord._async_refresh_battery_grid_plan.side_effect = advance
            else:
                case.controller.verify_engaged.side_effect = advance
            await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert not case.coord._grid_charge_engaged
        case.controller.disengage.assert_awaited_once()
        assert case.coord._battery_retry_after is not None
        assert case.coord._battery_charge_deadline is None
    finally:
        if periodic is not None:
            await asyncio.gather(periodic, return_exceptions=True)
        await case.coord.async_stop_battery_controls()


@pytest.mark.parametrize("change", ["price", "power", "soc"])
async def test_enable_confirmation_revalidates_current_price_power_and_target(economic_case, change):
    case, _ = preparation_case(economic_case)
    async def enabled(_, *, before_enable=None):
        await before_enable()
        # Physical enable has occurred, but its awaited readback has not returned.
        if change == "price":
            case.coord.config_entry.data = {**case.coord.config_entry.data, "battery_charge_price_threshold": .05}
        elif change == "power":
            case.coord.config_entry.data = {**case.coord.config_entry.data, "battery_grid_charge_power_w": 900}
        else:
            case.reading("sensor.soc", 100)
        await asyncio.sleep(0)
    case.controller.enable_prepared.side_effect = enabled
    case.ready.set()
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        case.controller.enable_prepared.assert_awaited_once()
        if change == "power":
            assert case.coord._grid_charge_engaged
            assert case.coord._battery_charge_power == 900
            case.controller.set_power.assert_awaited_with(900)
            case.controller.disengage.assert_not_awaited()
        else:
            assert not case.coord._grid_charge_engaged
            case.controller.disengage.assert_awaited_once()
            assert case.coord.battery_control_diagnostics()["battery_last_charge_stop"] is not None
    finally:
        await case.coord.async_stop_battery_controls()


async def test_missed_start_invalidates_cached_episode_before_any_hardware_write(economic_case):
    case = economic_case([.1, .4], [0, 0], [0, 2000], battery_grid_charge_minimum_minutes=5)
    case.controller.config = InverterGridChargeConfig("select.command", "Charge", "Stop",
        mode_entity_id="select.mode", mode_engage_value="Forced", mode_disengage_value="Self",
        power_entity_id="number.charge_power", timeout_seconds=15)
    install_charge_plan(case, actions=("charge", "self_consumption"), seconds=360)
    assert case.coord._battery_charge_start_allowed(case.coord._battery_charge_episode_now())
    case.coord._battery_plan_key = ("previously_executable",)
    case.clock["now"] += timedelta(seconds=30)
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.coord._battery_grid_plan is None
        assert case.coord._battery_plan_key is None
        case.controller.prepare.assert_not_awaited()
        case.controller.engage.assert_not_awaited()
        case.controller.disengage.assert_not_awaited()
    finally:
        await case.coord.async_stop_battery_controls()
