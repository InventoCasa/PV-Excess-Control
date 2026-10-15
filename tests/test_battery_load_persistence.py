"""Transient household demand must earn its longer forecast horizon."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.pv_excess_control.battery_load_forecast import RecentHouseholdLoad
from tests.test_battery_control_economics import economic_case  # noqa: F401
from tests.test_battery_load_context import appliance
from tests.test_battery_load_stability import floor_at, sample

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def observe(model, seconds, watts, *, prior=500, reference=500):
    return model.observe(BASE + timedelta(seconds=seconds), watts, prior,
                         reference_watts=reference)


def test_reference_requires_thirty_minutes_and_does_not_cancel_excess_evidence():
    model = RecentHouseholdLoad()
    for seconds in range(0, 1800, 30):
        observe(model, seconds, 3500)
        assert model.background_reference_watts == 500
    assert observe(model, 1800, 3500) == 3500
    assert model.background_reference_watts == 3500
    for seconds in range(1830, 3630, 30):
        observe(model, seconds, 3500)
        assert model.persistence_seconds == 1800


def test_stopped_long_load_and_real_zero_clear_evidence_over_observation_window():
    model = RecentHouseholdLoad()
    for seconds in range(0, 1830, 30):
        observe(model, seconds, 3500)
    observe(model, 1830, 0, prior=0)
    for seconds in range(1860, 1980, 30):
        observe(model, seconds, 0, prior=0)
    assert model.background_reference_watts == 0
    assert model.persistence_seconds == 120


@pytest.mark.parametrize("event", ["backwards", "gap", "invalid", "reset"])
def test_mean_reset_also_resets_reference_and_evidence(event):
    model = RecentHouseholdLoad()
    for seconds in range(0, 1830, 30):
        observe(model, seconds, 3500)
    if event == "invalid":
        with pytest.raises(ValueError):
            observe(model, 1810, 3500, reference=float("nan"))
    if event == "reset":
        model.reset()
    seconds = -1 if event == "backwards" else 1921 if event == "gap" else 1810
    assert observe(model, seconds, 2500, prior=300) == 300
    assert model.background_reference_watts == 300
    assert model.persistence_seconds == 120


def test_duplicate_report_cannot_promote_the_background_reference():
    model = RecentHouseholdLoad()
    for seconds in range(0, 1800, 30):
        observe(model, seconds, 600, prior=100, reference=5000)
    for _ in range(100):
        observe(model, 1770, 900, prior=900, reference=0)
    assert model.background_reference_watts == 100
    assert model.persistence_seconds == 600
    observe(model, 1800, 600, prior=100, reference=5000)
    assert model.background_reference_watts == 600


def test_long_pwm_preserves_average_reference_without_following_off_samples():
    model = RecentHouseholdLoad()
    for seconds in range(0, 1890, 30):
        estimate = observe(model, seconds, 3500 if seconds % 60 == 0 else 500)
    assert estimate == 2000
    assert model.background_reference_watts == 2000
    assert model.persistence_seconds == 1800


def horizon(rows, now, baseline=500):
    return max((r.end for r in rows if r.household_power_floor_w is not None
                and r.household_power_floor_w > baseline), default=now)


def test_three_minute_kitchen_load_does_not_invent_thirty_minutes_of_demand(economic_case):
    case = economic_case([.44, .49], [0, 0], [500, 500])
    sample(case, 500)
    for index in range(7):
        rows, _, error = sample(case, 3500, seconds=30)
        assert error is None
    assert floor_at(rows, case.clock["now"]) == 3500
    assert floor_at(rows, case.clock["now"] + timedelta(minutes=3)) == 500
    assert horizon(rows, case.clock["now"]) <= case.clock["now"] + timedelta(minutes=2)
    for _ in range(5):
        rows, _, _ = sample(case, 500, seconds=30)
    assert floor_at(rows, case.clock["now"]) == 500


@pytest.mark.parametrize("minutes,expected", [(3, 2), (6, 5), (11, 10), (31, 30), (60, 30)])
def test_sustained_unknown_load_earns_bounded_horizon(economic_case, minutes, expected):
    case = economic_case([.44, .49], [0, 0], [500, 500])
    for index in range(minutes * 2 + 1):
        rows, _, error = sample(case, 3500, seconds=30 if index else 0)
        assert error is None
    remaining = horizon(rows, case.clock["now"]) - case.clock["now"]
    assert timedelta(0) < remaining <= timedelta(minutes=expected)
    assert remaining >= timedelta(minutes=expected - 2)
    assert floor_at(rows, case.clock["now"] + timedelta(minutes=expected + 1)) == 500


def test_repeated_duty_cycle_preserves_mean_and_earns_longer_horizon(economic_case):
    case = economic_case([.44, .49], [0, 0], [500, 500])
    for index in range(23):
        rows, _, _ = sample(case, 3500 if index % 2 == 0 else 500,
                            seconds=30 if index else 0)
    assert floor_at(rows, case.clock["now"] + timedelta(minutes=3)) == 2000
    assert floor_at(rows, case.clock["now"] + timedelta(minutes=11)) == 500


def test_same_report_cannot_renew_or_mature_unknown_load_forecast(economic_case):
    case = economic_case([.44, .49], [0, 0], [500, 500])
    sample(case, 3500)
    for _ in range(4):
        rows, key, _ = sample(case, 3500, seconds=30)
    end = horizon(rows, case.clock["now"])
    for _ in range(4):
        repeated, repeated_key, _ = sample(case, seconds=20)
        assert repeated_key == key
        assert horizon(repeated, case.clock["now"]) == end
    rows, _, error = sample(case, seconds=45)
    assert error is None  # The source is still within the ordinary freshness TTL.
    assert rows == ()


def test_known_run_does_not_carry_short_unknown_background_into_later_hours(economic_case):
    case = economic_case([.17, .17, .35], [0, 0, 0], [5000, 500, 500])
    appliance(case, ev=False, watts=1000)
    profile = [5000., 500.] + [500.] * 22
    sample(case, 1100, profile=profile)
    for _ in range(7):
        rows, _, _ = sample(case, 1600, seconds=30, profile=profile)
    future = case.clock["now"].replace(minute=0, second=0) + timedelta(hours=1)
    assert floor_at(rows, future) == 1100
    assert floor_at(rows, case.clock["now"], baseline=0) == 1600


def test_established_background_can_support_known_run_but_is_capped_by_future_profile(economic_case):
    case = economic_case([.17, .17, .35], [0, 0, 0], [5000, 500, 500])
    appliance(case, ev=False, watts=1000)
    profile = [5000., 500.] + [500.] * 22
    sample(case, 1100, profile=profile)
    for _ in range(63):
        case.reading("sensor.appliance_power", 1000, unit_of_measurement="W")
        rows, _, error = sample(case, 1800, seconds=30, profile=profile)
        assert error is None
    future = case.clock["now"].replace(minute=0, second=0) + timedelta(hours=1)
    assert floor_at(rows, future) == 1500


def test_unknown_excess_keeps_its_power_but_not_known_metered_run_horizon(economic_case):
    case = economic_case([.44, .49], [0, 0], [500, 500],
                         battery_max_discharge_entity="number.discharge")
    appliance(case, ev=False, watts=4000, on_only=True, is_big_consumer=True,
              battery_discharge_override=2000)
    case.reading("number.discharge", 5000, min=100, max=5000, unit_of_measurement="W")
    sample(case, 4500)
    for _ in range(5):
        rows, _, error = sample(case, 7500, seconds=30)
        assert error is None
    assert floor_at(rows, case.clock["now"]) == 7500
    assert floor_at(rows, case.clock["now"] + timedelta(minutes=3)) == 4500
    limit = next(row for row in rows if row.max_discharge_power_w is not None)
    assert limit.max_discharge_power_w == 2000
    assert limit.end > case.clock["now"] + timedelta(minutes=20)


def test_standby_meter_does_not_invent_a_hundred_watt_household_floor(economic_case):
    case = economic_case([.44, .49], [0, 0], [500, 500])
    appliance(case, ev=False, watts=4, on_only=True)
    rows, _, _ = sample(case, 504)
    assert floor_at(rows, case.clock["now"]) == 500


def test_unknown_excess_can_outlive_a_known_run_without_extending_that_run(economic_case):
    case = economic_case([.44, .49], [0, 0], [500, 500])
    appliance(case, ev=False, watts=4000, end_before="00:03:00")
    sample(case, 4500)
    for _ in range(5):
        rows, _, _ = sample(case, 7500, seconds=30)
    assert floor_at(rows, case.clock["now"]) == 7500
    assert floor_at(rows, case.clock["now"].replace(minute=3, second=1)) == 3500


@pytest.mark.parametrize("event", ["missing", "gap", "composition", "reset"])
def test_history_restart_does_not_inherit_long_persistence(economic_case, event):
    case = economic_case([.44, .49], [0, 0], [500, 500])
    for index in range(24):
        sample(case, 3500, seconds=30 if index else 0)
    if event == "missing":
        case.reading("sensor.load_power", "unavailable")
        assert sample(case)[2] == "load_power_missing_or_stale"
    elif event == "gap":
        case.clock["now"] += timedelta(seconds=121)
    elif event == "composition":
        appliance(case, ev=False, watts=0)
    else:
        case.coord._reset_battery_load_forecast()
    for _ in range(5):
        rows, _, error = sample(case, 3500, seconds=30)
        assert error is None
    assert horizon(rows, case.clock["now"]) <= case.clock["now"] + timedelta(minutes=2)


def test_horizon_uses_report_time_and_elapsed_utc_through_clock_fallback(economic_case):
    case = economic_case([.44, .49], [0, 0], [500, 500])
    berlin = ZoneInfo("Europe/Berlin")
    base = datetime(2026, 10, 25, 0, 58, tzinfo=timezone.utc)
    for index in range(7):
        case.clock["now"] = (base + timedelta(seconds=index * 30)).astimezone(berlin)
        case.reading("sensor.load_power", 3500, unit_of_measurement="W")
        rows, _, error = case.coord._battery_load_context(case.clock["now"], case.tariff, [500.] * 24)
        assert error is None
    deadline = horizon(rows, case.clock["now"])
    elapsed = deadline.astimezone(timezone.utc) - case.clock["now"].astimezone(timezone.utc)
    assert timedelta(0) < elapsed <= timedelta(minutes=2)
