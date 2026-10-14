"""A queued hold-boundary handoff cannot overwrite a newer consumer limit."""
import asyncio

import pytest

from custom_components.pv_excess_control.models import BatteryDischargeAction
from tests.test_battery_control_economics import economic_case  # noqa: F401
from tests.test_battery_numeric_hold import cleanup, numeric_case  # noqa: F401
from tests.test_battery_hold_continuity import install_plan


@pytest.mark.parametrize("old_action,new_action,expected", [
    (BatteryDischargeAction(False), BatteryDischargeAction(True, 0), [10]),
    (BatteryDischargeAction(True, 100), BatteryDischargeAction(False), [10, 5000]),
])
async def test_handoff_waiting_for_shared_writer_keeps_new_consumer_limit(numeric_case, old_action, new_action, expected):
    case = numeric_case
    coord = case.coord
    slots = install_plan(case)
    tasks = []

    def create_task(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    coord.hass.async_create_task.side_effect = create_task
    try:
        await coord._run_grid_charge_state_machine(case.tariff, None)
        await coord._apply_battery_discharge_limit(old_action)
        assert case.writes == [10]
        callback = coord._battery_watchdog._callback
        coord._battery_watchdog.cancel()
        case.clock["now"] = slots[1].end
        async with coord._battery_lock:
            callback()
            await asyncio.sleep(0)  # The old boundary task waits first.
            newer = asyncio.create_task(coord._apply_shared_discharge_limit(new_action))
            await asyncio.sleep(0)  # Fresh consumer protection waits behind it.
        await asyncio.gather(newer, *tasks)
        # The callback releases the lock before calling the shared writer.
        # The newer limit therefore becomes current before that second acquire.
        assert case.writes == expected
        assert float(case.states["sensor.discharge_limit"].state) == expected[-1]
        assert coord._battery_discharge_last_action is new_action
        assert coord._battery_discharge_cleanup_pending is new_action.should_limit
    finally:
        await cleanup(case)
    assert case.writes == [10, 5000]  # Explicit cleanup remains authoritative.
