"""Unmapped load forecasts use time coverage without delaying known runs."""
from datetime import timedelta

import pytest

from tests.test_battery_control_economics import economic_case  # noqa: F401
from tests.test_battery_load_context import appliance


def sample(case, watts=None, *, seconds=0, profile=None):
    case.clock["now"] += timedelta(seconds=seconds)
    if watts is not None:
        case.reading("sensor.load_power", watts, unit_of_measurement="W")
    return case.coord._battery_load_context(case.clock["now"], case.tariff, profile or [500.] * 24)


def floor_at(rows, when, baseline=500):
    return max([baseline] + [row.household_power_floor_w for row in rows
                            if row.start <= when < row.end and row.household_power_floor_w is not None])


def later(case, seconds=120):
    return case.clock["now"] + timedelta(seconds=seconds)


def test_single_spike_is_only_projected_for_one_short_observation_window(economic_case):
    case = economic_case([.17, .35], [0, 0], [500, 500])
    sample(case, 500)
    rows, _, error = sample(case, 6500, seconds=30)
    assert error is None
    assert floor_at(rows, case.clock["now"]) == 6500
    assert floor_at(rows, later(case)) == 500
    short = [r for r in rows if r.household_power_floor_w == 6500]
    assert short[0].end == case.clock["now"] + timedelta(seconds=60)


def test_sustained_load_reaches_its_full_forecast_after_two_minutes(economic_case):
    case = economic_case([.17, .35], [0, 0], [500, 500])
    rows, _, _ = sample(case, 4000)
    assert floor_at(rows, later(case)) == 500
    for _ in range(4):
        rows, _, _ = sample(case, 4000, seconds=30)
    assert floor_at(rows, later(case)) == 4000


def test_alternating_power_preserves_time_weighted_energy_and_real_zero(economic_case):
    case = economic_case([.17, .35], [0, 0], [500, 500])
    sample(case, 4000)
    for watts in (0, 4000, 0, 4000):
        rows, _, error = sample(case, watts, seconds=30)
        assert error is None
    assert floor_at(rows, later(case)) == 2000


def test_repeated_report_neither_adds_weight_nor_extends_short_horizon(economic_case):
    case = economic_case([.17, .35], [0, 0], [500, 500])
    first, first_key, _ = sample(case, 4000)
    repeated, repeated_key, _ = sample(case, seconds=30)
    assert first_key == repeated_key
    assert first[0].end == repeated[0].end
    rows, final_key, _ = sample(case, seconds=45)
    assert floor_at(rows, case.clock["now"]) == 500
    assert final_key != first_key


def test_known_metered_run_and_stop_remain_immediate(economic_case):
    case = economic_case([.17, .35], [0, 0], [500, 500])
    appliance(case, ev=False, watts=4000)
    rows, _, _ = sample(case, 4500)
    assert floor_at(rows, later(case)) == 4500
    case.clock["now"] += timedelta(seconds=30)
    case.reading("switch.load", "off")
    case.reading("sensor.appliance_power", 0, unit_of_measurement="W")
    rows, _, _ = sample(case, 500)
    assert floor_at(rows, later(case)) == 500


def test_known_ev_demand_remains_immediate(economic_case):
    case = economic_case([.17, .35], [0, 0], [500, 500])
    appliance(case, watts=11000)
    rows, _, error = sample(case, 11500)
    assert error is None
    assert next(row for row in rows if row.power_w).power_w == 11000
    assert floor_at(rows, later(case)) == 500


def test_missing_ev_measurement_resets_unknown_background_history(economic_case):
    case = economic_case([.17, .35], [0, 0], [500, 500])
    appliance(case, watts=3000)
    sample(case, 7000)
    for _ in range(4):
        case.clock["now"] += timedelta(seconds=30)
        case.reading("sensor.appliance_power", 3000, unit_of_measurement="W")
        sample(case, 7000)
    case.reading("sensor.appliance_power", "unavailable")
    assert sample(case, 7000)[2] == "active_ev_power_missing_or_stale"
    case.clock["now"] += timedelta(seconds=10)
    case.reading("sensor.appliance_power", 3000, unit_of_measurement="W")
    rows, _, error = sample(case, 4500)
    assert error is None
    assert floor_at(rows, later(case)) == 500


def test_missing_primary_load_in_refresh_resets_background_history(economic_case):
    case = economic_case([.17, .35], [0, 0], [500, 500])
    sample(case, 4000)
    for _ in range(4):
        sample(case, 4000, seconds=30)
    case.reading("sensor.load_power", "unavailable")
    assert case.coord._refresh_battery_grid_plan(case.tariff, None) == "load_power_missing_or_stale"
    rows, _, error = sample(case, 1500, seconds=10)
    assert error is None
    assert floor_at(rows, later(case)) == 500


def test_unknown_background_of_known_run_is_not_extrapolated_immediately(economic_case):
    case = economic_case([.17, .17, .35], [0, 0, 0], [5000, 500, 500])
    appliance(case, ev=False, watts=1000)
    profile = [5000., 500.] + [500.] * 22
    sample(case, 1100, profile=profile)
    rows, _, _ = sample(case, 1600, seconds=30, profile=profile)
    tomorrow_hour = case.clock["now"].replace(minute=0, second=0) + timedelta(hours=1)
    assert floor_at(rows, tomorrow_hour) == 1100
    for _ in range(4):
        rows, _, _ = sample(case, 1600, seconds=30, profile=profile)
    assert floor_at(rows, tomorrow_hour) == 1500


def test_new_meter_mapping_resets_background_instead_of_counting_it_twice(economic_case):
    case = economic_case([.17, .35], [0, 0], [500, 500])
    sample(case, 4500)
    for _ in range(4):
        sample(case, 4500, seconds=30)
    appliance(case, ev=False, watts=4000)
    rows, _, error = sample(case, 4500)
    assert error is None
    assert floor_at(rows, later(case)) == 4500


def test_unknown_run_discharge_limit_remains_immediate_and_independent(economic_case):
    case = economic_case([.17, .35], [0, 0], [500, 500],
                         battery_max_discharge_entity="number.discharge")
    appliance(case, ev=False, watts=4000, on_only=True, is_big_consumer=True,
              battery_discharge_override=2000)
    case.reading("number.discharge", 5000, min=100, max=5000, unit_of_measurement="W")
    rows, _, error = sample(case, 4500)
    assert error is None
    limit = next(row for row in rows if row.max_discharge_power_w is not None)
    assert limit.max_discharge_power_w == 2000
    assert limit.end == case.clock["now"] + timedelta(minutes=30)


def test_fresh_below_profile_reports_do_not_add_phantom_short_windows(economic_case):
    case = economic_case([.17, .35], [0, 0], [5000, 5000])
    profile = [5000.] * 24
    rows, key, _ = sample(case, 500, profile=profile)
    assert rows == ()
    for _ in range(5):
        rows, next_key, _ = sample(case, 1500, seconds=30, profile=profile)
        assert rows == ()
        assert next_key == key


def test_short_observation_remains_effective_across_lower_profile_hour(economic_case):
    case = economic_case([.17, .35], [0, 0], [5000, 500])
    profile = [5000., 500.] + [500.] * 22
    case.clock["now"] = case.clock["now"].replace(minute=59)
    sample(case, 500, profile=profile)
    rows, _, _ = sample(case, 1600, seconds=30, profile=profile)
    boundary = case.clock["now"].replace(minute=0, second=0) + timedelta(hours=1)
    assert floor_at(rows, case.clock["now"], baseline=5000) == 5000
    assert floor_at(rows, boundary) == 1600
