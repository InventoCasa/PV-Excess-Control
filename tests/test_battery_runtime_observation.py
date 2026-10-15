"""Daily-runtime bounds describe an observed counter, not validation time."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from tests.test_battery_control_economics import economic_case  # noqa: F401
from tests.test_battery_load_context import appliance


def runtime_case(factory, *, sampled=True):
    case = factory([.1, .4, .4], [0, 0, 0], [500, 2000, 2000])
    data = appliance(case, ev=False, watts=12.5, max_daily_runtime=180,
                     allow_grid_supplement=False)
    state = SimpleNamespace(runtime_today=timedelta(minutes=10), is_on=True)
    if sampled:
        state.runtime_observed_at = case.clock["now"]
    case.coord.appliance_states["load"] = state
    return case, data, state


@pytest.mark.parametrize("sampled", [False, True])
async def test_daily_runtime_plan_survives_elapsed_executor_time(economic_case, sampled):
    case, _, _ = runtime_case(economic_case, sampled=sampled)
    calls = 0
    async def executor(function, *args):
        nonlocal calls
        calls += 1
        result = await asyncio.to_thread(function, *args)
        case.clock["now"] += timedelta(seconds=.1)
        return result
    case.coord.hass.async_add_executor_job = executor
    assert await case.coord._async_refresh_battery_grid_plan(case.tariff, None) is None
    assert calls == 1
    assert case.coord._battery_grid_plan.valid


def test_repeated_validation_uses_same_observation_instead_of_current_clock(economic_case):
    case, data, state = runtime_case(economic_case)
    original = case.coord._battery_load_run_end("load", data, case.clock["now"], case.tariff)[0]
    case.clock["now"] += timedelta(seconds=20)
    repeated = case.coord._battery_load_run_end("load", data, case.clock["now"], case.tariff)[0]
    assert repeated == original
    assert original == state.runtime_observed_at + timedelta(minutes=170)


@pytest.mark.parametrize("sampled,change", [
    (False, "counter"), (False, "maximum"), (False, "run"),
    (True, "counter"), (True, "maximum"),
])
async def test_real_runtime_inputs_still_invalidate_pending_plan(economic_case, sampled, change):
    case, data, state = runtime_case(economic_case, sampled=sampled)
    calls = 0
    async def executor(function, *args):
        nonlocal calls
        calls += 1
        result = await asyncio.to_thread(function, *args)
        case.clock["now"] += timedelta(seconds=.1)
        if calls == 1:
            if change == "counter":
                state.runtime_today += timedelta(seconds=30)
                if sampled:
                    state.runtime_observed_at = case.clock["now"]
            elif change == "maximum":
                data["max_daily_runtime"] = 120
            else:
                case.reading("switch.load", "on")
        return result
    case.coord.hass.async_add_executor_job = executor
    assert await case.coord._async_refresh_battery_grid_plan(case.tariff, None) is None
    assert calls == 2
    assert case.coord._battery_grid_plan.valid


def test_new_maximum_uses_original_counter_sample(economic_case):
    case, data, state = runtime_case(economic_case)
    first = case.coord._battery_load_run_end("load", data, case.clock["now"], case.tariff)[0]
    case.clock["now"] += timedelta(seconds=20)
    data["max_daily_runtime"] = 120
    assert case.coord._battery_load_run_end("load", data, case.clock["now"], case.tariff)[0] == first - timedelta(minutes=60)


def test_previous_local_day_counter_does_not_promise_today_stop(economic_case):
    case, data, state = runtime_case(economic_case)
    case.clock["now"] = datetime(2026, 10, 9, tzinfo=timezone.utc)
    state.runtime_observed_at = case.clock["now"] - timedelta(seconds=1)
    assert case.coord._battery_load_run_end("load", data, case.clock["now"], case.tariff)[0] is None


def test_runtime_deadline_counts_actual_elapsed_time_through_dst(economic_case):
    case, data, state = runtime_case(economic_case)
    case.clock["now"] = datetime(2026, 10, 25, 2, 30, tzinfo=ZoneInfo("Europe/Berlin"))
    state.runtime_observed_at = case.clock["now"]
    data["max_daily_runtime"] = 100
    end = case.coord._battery_load_run_end("load", data, case.clock["now"], case.tariff)[0]
    assert end == datetime(2026, 10, 25, 2, tzinfo=timezone.utc)
    case.clock["now"] += timedelta(seconds=.1)
    assert case.coord._battery_load_run_end("load", data, case.clock["now"], case.tariff)[0] == end


def test_prior_dst_fold_sample_is_not_rejected_as_future_wall_time(economic_case):
    case, data, state = runtime_case(economic_case)
    zone = ZoneInfo("Europe/Berlin")
    state.runtime_observed_at = datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=0)
    case.clock["now"] = datetime(2026, 10, 25, 2, 15, tzinfo=zone, fold=1)
    data["max_daily_runtime"] = 100
    assert case.coord._battery_load_run_end("load", data, case.clock["now"], case.tariff)[0] == datetime(
        2026, 10, 25, 2, tzinfo=timezone.utc,
    )


@pytest.mark.parametrize("stamp", ["unknown", datetime(2026, 10, 9), datetime(2026, 10, 9, 1, tzinfo=timezone.utc)])
def test_invalid_or_future_counter_clock_does_not_invent_a_daily_stop(economic_case, stamp):
    case, data, state = runtime_case(economic_case)
    state.runtime_observed_at = stamp
    assert case.coord._battery_load_run_end("load", data, case.clock["now"], case.tariff)[0] is None


def test_real_zero_counter_remains_valid_and_missing_counter_state_is_unknown(economic_case):
    case, data, state = runtime_case(economic_case)
    state.runtime_today = timedelta()
    assert case.coord._battery_load_run_end("load", data, case.clock["now"], case.tariff)[0] == case.clock["now"] + timedelta(minutes=180)
    del case.coord.appliance_states["load"]
    assert case.coord._battery_load_run_end("load", data, case.clock["now"], case.tariff)[0] is None


def test_removed_appliance_drops_legacy_runtime_anchor(economic_case):
    case, data, _ = runtime_case(economic_case, sampled=False)
    case.coord._battery_load_run_end("load", data, case.clock["now"], case.tariff)
    assert "load" in case.coord._battery_runtime_observations
    case.coord.config_entry.subentries = {}
    case.coord._battery_load_context(case.clock["now"], case.tariff, case.coord._battery_load_profile())
    assert case.coord._battery_runtime_observations == {}


def test_replaced_legacy_counter_snapshot_gets_its_own_observation(economic_case):
    case, data, state = runtime_case(economic_case, sampled=False)
    end = case.coord._battery_load_run_end("load", data, case.clock["now"], case.tariff)[0]
    case.clock["now"] += timedelta(seconds=1)
    case.coord.appliance_states["load"] = SimpleNamespace(**vars(state))
    assert case.coord._battery_load_run_end("load", data, case.clock["now"], case.tariff)[0] == end + timedelta(seconds=1)


@pytest.mark.parametrize("clock_change", ["rollback", "next_day"])
def test_unchanged_legacy_counter_cannot_be_redated_by_validation(economic_case, clock_change):
    case, data, state = runtime_case(economic_case, sampled=False)
    case.clock["now"] += timedelta(hours=12)
    assert case.coord._battery_load_run_end("load", data, case.clock["now"], case.tariff)[0] is not None
    case.clock["now"] += timedelta(seconds=-1) if clock_change == "rollback" else timedelta(days=1)
    assert case.coord._battery_load_run_end("load", data, case.clock["now"], case.tariff)[0] is None
    # A newly observed counter snapshot can establish a new clock/day.
    case.coord.appliance_states["load"] = SimpleNamespace(**vars(state))
    assert case.coord._battery_load_run_end("load", data, case.clock["now"], case.tariff)[0] == case.clock["now"] + timedelta(minutes=170)


def test_remaining_runtime_sensor_keeps_its_own_observation_clock(economic_case):
    case, data, _ = runtime_case(economic_case)
    data["remaining_runtime_entity"] = "sensor.remaining"
    case.reading("sensor.remaining", 10)
    end = case.coord._battery_load_run_end("load", data, case.clock["now"], case.tariff)[0]
    case.clock["now"] += timedelta(seconds=20)
    assert case.coord._battery_load_run_end("load", data, case.clock["now"], case.tariff)[0] == end


def test_runtime_counter_observation_is_unchanged_by_read_only_state_collection(economic_case):
    case, _, _ = runtime_case(economic_case)
    case.coord.appliance_states = {}
    configs = case.coord._get_appliance_configs()
    first = case.coord._get_appliance_states(configs)["load"]
    assert first.runtime_observed_at == case.clock["now"]
    case.clock["now"] += timedelta(seconds=30)
    current = case.coord._get_appliance_states(configs)["load"]
    assert current.runtime_observed_at == case.clock["now"]
    assert current.runtime_today == first.runtime_today + case.coord.update_interval
    case.clock["now"] += timedelta(seconds=.1)
    readonly = case.coord._get_appliance_states(configs, update_runtime=False)["load"]
    assert readonly.runtime_observed_at == current.runtime_observed_at
    assert readonly.runtime_today == current.runtime_today
    assert case.coord.appliance_states["load"] is current


def test_midnight_counter_reset_is_a_new_observation(economic_case):
    case, _, _ = runtime_case(economic_case)
    case.coord.appliance_states = {}
    configs = case.coord._get_appliance_configs()
    case.coord._get_appliance_states(configs)
    case.clock["now"] += timedelta(days=1)
    case.coord.reset_daily()
    state = case.coord.appliance_states["load"]
    assert state.runtime_today == timedelta()
    assert state.runtime_observed_at == case.clock["now"]
