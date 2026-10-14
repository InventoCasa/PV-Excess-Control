"""Contiguous validated holds keep their limit without interval-boundary pulses."""
import asyncio
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import State

from custom_components.pv_excess_control.battery_planner import BatteryGridPlan, BatteryGridSlot
from custom_components.pv_excess_control.models import BatteryDischargeAction, InverterGridChargeConfig
from tests.test_battery_control_economics import economic_case  # noqa: F401
from tests.test_battery_numeric_hold import cleanup, numeric_case  # noqa: F401


def install_plan(case, *, actions=("hold", "hold", "self_consumption")):
    start = case.clock["now"]
    slots = tuple(BatteryGridSlot(
        start + timedelta(seconds=20 * index), start + timedelta(seconds=20 * (index + 1)),
        .2 + index * .01, 0., .01, 0., 0., 0., 30., action,
    ) for index, action in enumerate(actions))
    case.coord._battery_grid_plan = BatteryGridPlan(slots, 0., .1, "economic_plan", 30.)
    case.coord._refresh_battery_grid_plan = MagicMock(return_value=None)
    if "_battery_action_after_wait" in case.coord.__dict__:
        del case.coord._battery_action_after_wait
    case.coord._latest_tariff = case.tariff
    case.reading("sensor.soc", 30)
    return slots


def independent_hold(case):
    mapping = InverterGridChargeConfig("switch.hold", "on", "off")
    controller = MagicMock(config=mapping, confirmation_level="physical")
    controller.engage = AsyncMock()
    controller.disengage = AsyncMock()
    controller.verify_engaged = AsyncMock()
    case.coord._battery_hold_ctl = controller
    case.coord.config_entry.data["battery_hold_entity"] = mapping.enable_entity_id
    return controller


async def test_shared_hold_keeps_one_limit_across_adjacent_intervals(numeric_case):
    case = numeric_case
    slots = install_plan(case)
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(True, 100))
        assert case.coord._battery_numeric_hold_deadline == slots[1].end
        assert 39 <= case.coord._battery_watchdog.when() - case.coord.hass.loop.time() <= 40.1
        case.clock["now"] = slots[0].end
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(True, 100))
        assert case.writes == [10]
        assert case.coord._battery_numeric_hold_deadline == slots[1].end
        case.clock["now"] = slots[1].end
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(True, 100))
        assert case.writes == [10, 100]
    finally:
        await cleanup(case)


@pytest.mark.parametrize("boundary", ["self_consumption", "charge", "gap", "overlap", "zero_length"])
async def test_hold_never_crosses_an_unvalidated_boundary(numeric_case, boundary):
    case = numeric_case
    slots = list(install_plan(case))
    if boundary in {"self_consumption", "charge"}:
        slots[1] = replace(slots[1], action=boundary)
    elif boundary == "gap":
        slots[1] = replace(slots[1], start=slots[1].start + timedelta(microseconds=1))
    elif boundary == "overlap":
        slots[1] = replace(slots[1], start=slots[1].start - timedelta(microseconds=1))
    else:
        slots[1] = replace(slots[1], end=slots[1].start)
    case.coord._battery_grid_plan = replace(case.coord._battery_grid_plan, slots=tuple(slots))
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(False))
        assert case.coord._battery_numeric_hold_deadline == slots[0].end
    finally:
        await cleanup(case)


async def test_shorter_replan_immediately_shortens_the_owned_hold(numeric_case):
    case = numeric_case
    slots = install_plan(case, actions=("hold", "hold", "hold"))
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(True, 100))
        assert case.coord._battery_numeric_hold_deadline == slots[2].end
        replacement = (slots[0], replace(slots[1], action="self_consumption"), slots[2])
        case.coord._battery_grid_plan = replace(case.coord._battery_grid_plan, slots=replacement)
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(True, 100))
        assert case.coord._battery_numeric_hold_deadline == slots[0].end
        assert 19 <= case.coord._battery_watchdog.when() - case.coord.hass.loop.time() <= 20.1
        assert case.writes == [10]
    finally:
        await cleanup(case)


async def test_hold_chain_cannot_extend_the_soc_freshness_deadline(numeric_case):
    case = numeric_case
    install_plan(case)
    old = case.clock["now"] - timedelta(seconds=290)
    case.states["sensor.soc"] = State("sensor.soc", "30", last_updated=old, last_reported=old)
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(False))
        assert 9 <= case.coord._battery_watchdog.when() - case.coord.hass.loop.time() <= 10.1
    finally:
        await cleanup(case)


@pytest.mark.parametrize("failure", ["pv", "load", "soc", "price"])
async def test_hold_chain_does_not_bypass_current_input_validation(numeric_case, failure):
    case = numeric_case
    install_plan(case)
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(True, 100))
        if failure == "price":
            case.coord._refresh_battery_grid_plan.return_value = "price_window_mismatch"
        else:
            case.reading({"pv": "sensor.pv_power", "load": "sensor.load_power", "soc": "sensor.soc"}[failure], "unavailable")
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(True, 100))
        assert case.writes == [10, 100]
        assert not case.coord._battery_hold_engaged
    finally:
        await cleanup(case)


async def test_independent_hold_and_shared_rearming_use_the_same_chain_end(numeric_case):
    case = numeric_case
    install_plan(case)
    hold = independent_hold(case)
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert 39 <= case.coord._battery_watchdog.when() - case.coord.hass.loop.time() <= 40.1
        case.coord._arm_shared_discharge_watchdog()
        assert 39 <= case.coord._battery_watchdog.when() - case.coord.hass.loop.time() <= 40.1
        hold.engage.assert_awaited_once_with(0)
        hold.disengage.assert_not_awaited()
    finally:
        await cleanup(case)


@pytest.mark.parametrize("explicit_stop", ["disabled", "unload"])
async def test_fired_watchdog_waiting_on_lock_cannot_stop_a_new_valid_period(economic_case, explicit_stop):
    case = economic_case([.1, .4], [0, 0], [0, 2000])
    coord = case.coord
    coord._grid_charge_engaged = True
    coord._grid_charge_cleanup_pending = True
    coord._battery_charge_owner = case.controller
    tasks = []

    def create_task(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    coord.hass.async_create_task.side_effect = create_task
    coord._arm_battery_watchdog(case.clock["now"] + timedelta(seconds=20))
    callback = coord._battery_watchdog._callback
    coord._battery_watchdog.cancel()
    try:
        async with coord._battery_lock:
            case.clock["now"] += timedelta(seconds=20)
            callback()
            await asyncio.sleep(0)
            assert tasks and not tasks[0].done()
            case.reading("sensor.soc", 30)
            coord._arm_battery_watchdog(case.clock["now"] + timedelta(seconds=40))
        await asyncio.gather(*tasks)
        case.controller.disengage.assert_not_awaited()
        assert coord._grid_charge_engaged
        if explicit_stop == "unload":
            assert await coord.async_prepare_battery_unload()
        else:
            assert await coord.async_stop_battery_controls(explicit_stop)
        case.controller.disengage.assert_awaited_once()
        assert not coord._grid_charge_engaged
    finally:
        await coord.async_stop_battery_controls()


async def test_cancelled_callback_cannot_orphan_a_new_watchdog(economic_case):
    case = economic_case([.1, .4], [0, 0], [0, 2000])
    coord = case.coord
    coord._arm_battery_watchdog(case.clock["now"] + timedelta(seconds=20))
    obsolete_callback = coord._battery_watchdog._callback
    coord._arm_battery_watchdog(case.clock["now"] + timedelta(seconds=40))
    current = coord._battery_watchdog
    try:
        obsolete_callback()
        assert coord._battery_watchdog is current
        assert not current.cancelled()
    finally:
        current.cancel()
        coord._cancel_battery_watchdog()
        await asyncio.sleep(0)


async def test_adjacent_hold_window_crosses_local_midnight(numeric_case):
    case = numeric_case
    case.clock["now"] = case.clock["now"].replace(hour=23, minute=59, second=40)
    for entity, state in list(case.states.items()):
        case.reading(entity, state.state, **state.attributes)
    slots = install_plan(case)
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(False))
        assert slots[0].end.day != slots[0].start.day
        assert case.coord._battery_numeric_hold_deadline == slots[1].end
        case.clock["now"] = slots[0].end
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        await case.coord._apply_battery_discharge_limit(BatteryDischargeAction(False))
        assert case.writes == [10]
    finally:
        await cleanup(case)


@pytest.mark.parametrize("after_wait", ["adjacent_hold", "window_end", "changed_plan", "invalid_price"])
async def test_independent_hold_revalidates_after_hardware_wait(numeric_case, after_wait):
    case = numeric_case
    slots = install_plan(case)
    hold = independent_hold(case)

    async def engage(_power):
        case.clock["now"] = slots[0].end
        if after_wait == "window_end":
            case.clock["now"] = slots[1].end
        elif after_wait == "changed_plan":
            changed = (slots[0], replace(slots[1], action="self_consumption"), slots[2])
            case.coord._battery_grid_plan = replace(case.coord._battery_grid_plan, slots=changed)
        elif after_wait == "invalid_price":
            case.coord._refresh_battery_grid_plan.return_value = "price_window_mismatch"

    hold.engage.side_effect = engage
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        if after_wait == "adjacent_hold":
            assert case.coord._battery_hold_engaged
            assert case.coord._battery_control_state == "holding"
            assert 19 <= case.coord._battery_watchdog.when() - case.coord.hass.loop.time() <= 20.1
            hold.disengage.assert_not_awaited()
        else:
            assert not case.coord._battery_hold_engaged
            hold.disengage.assert_awaited_once()
    finally:
        await cleanup(case)


@pytest.mark.parametrize("change", ["non_hold", "gap", "invalid_plan", "missing_plan"])
async def test_rearming_an_independent_hold_cannot_bridge_a_missing_hold_slot(numeric_case, change):
    case = numeric_case
    slots = install_plan(case)
    independent_hold(case)
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        if change == "non_hold":
            case.clock["now"] = slots[1].end
        elif change == "gap":
            case.clock["now"] = slots[0].end
            replacement = (slots[0], replace(slots[1], start=slots[1].start + timedelta(seconds=1)), slots[2])
            case.coord._battery_grid_plan = replace(case.coord._battery_grid_plan, slots=replacement)
        elif change == "invalid_plan":
            case.coord._battery_grid_plan = replace(case.coord._battery_grid_plan, valid=False)
        else:
            case.coord._battery_grid_plan = None
        case.coord._arm_shared_discharge_watchdog()
        assert 0 <= case.coord._battery_watchdog.when() - case.coord.hass.loop.time() <= .11
    finally:
        await cleanup(case)
