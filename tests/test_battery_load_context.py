"""Live major loads must not become fictitious future battery solar input."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import StateMachine

from tests.test_battery_control_economics import economic_case  # noqa: F401


def appliance(case, *, ev=True, watts=11000, **settings):
    data = {
        "appliance_entity": "switch.load", "actual_power_entity": "sensor.appliance_power",
        "appliance_name": "Major load", "allow_grid_supplement": True,
        "cheap_price_threshold": .18, "nominal_power": watts, **settings,
    }
    if ev:
        data["ev_connected_entity"] = "binary_sensor.connected"
        case.reading("binary_sensor.connected", "on")
    case.coord.config_entry.subentries = {"load": SimpleNamespace(data=data)}
    case.reading("switch.load", "on")
    case.reading("sensor.appliance_power", watts, unit_of_measurement="W")
    case.reading("sensor.load_power", watts, unit_of_measurement="W")
    return data


def refresh(case):
    return case.coord._refresh_battery_grid_plan(case.tariff, None)


def test_active_ev_reserves_solar_until_its_private_cheap_window_ends(economic_case):
    case = economic_case([.17, .35], [2.5, 0], [0, 2000])
    assert refresh(case) is None
    assert case.coord._battery_grid_plan.grid_energy_kwh == 0
    appliance(case)
    case.tariff.cheap_price_threshold = 0
    assert refresh(case) is None
    plan = case.coord._battery_grid_plan
    assert plan.grid_energy_kwh > 2
    assert plan.slots[0].pv_charge_kwh == 0
    assert sum(s.load_kwh for s in plan.slots) == pytest.approx(2)
    assert case.coord.battery_control_diagnostics()["battery_load_commitments"][0]["external_power_w"] == 11000


def test_ev_stop_invalidates_plan_without_waiting_five_minutes(economic_case):
    case = economic_case([.17, .35], [2.5, 0], [0, 2000])
    appliance(case)
    assert refresh(case) is None
    old = case.coord._battery_grid_plan
    case.reading("switch.load", "off")
    case.reading("sensor.appliance_power", 0, unit_of_measurement="W")
    case.reading("sensor.load_power", 0, unit_of_measurement="W")
    assert refresh(case) is None
    assert case.coord._battery_grid_plan is not old
    assert case.coord._battery_grid_plan.grid_energy_kwh == 0


@pytest.mark.parametrize("problem", ["stale", "disabled", "paused", "override", "unbounded"])
def test_uncertain_active_ev_clears_existing_plan(economic_case, problem):
    case = economic_case([.17, .35], [2.5, 0], [0, 2000])
    appliance(case)
    assert refresh(case) is None
    if problem == "stale":
        del case.states["sensor.appliance_power"]
    elif problem == "disabled":
        case.coord.appliance_enabled["load"] = False
    elif problem == "paused":
        case.coord.appliance_paused = {"load": True}
    elif problem == "override":
        case.coord.appliance_overrides["load"] = True
    else:
        case.tariff.windows[-1] = replace(case.tariff.windows[-1], price=.17)
    assert refresh(case).startswith("active_ev_")
    assert case.coord._battery_grid_plan is None


def test_runtime_sensor_shortens_ev_run_without_extrapolating_full_day(economic_case):
    case = economic_case([.17, .35], [2.5, 0], [0, 2000])
    appliance(case, remaining_runtime_entity="sensor.remaining")
    case.reading("sensor.remaining", 15, unit_of_measurement="min")
    assert refresh(case) is None
    slots = case.coord._battery_grid_plan.slots
    assert slots[0].end == case.clock["now"] + timedelta(minutes=15)
    assert slots[0].pv_charge_kwh == 0
    assert any(s.pv_charge_kwh > 0 for s in slots[1:])


def test_unmapped_house_load_reserves_observed_solar_for_only_thirty_minutes(economic_case):
    case = economic_case([.17, .35], [2.5, 0], [0, 2000])
    case.reading("sensor.load_power", 4000, unit_of_measurement="W")
    assert refresh(case) is None
    slots = case.coord._battery_grid_plan.slots
    assert slots[0].end == case.clock["now"] + timedelta(minutes=30)
    assert slots[0].pv_charge_kwh == 0
    assert sum(s.load_kwh for s in slots) == pytest.approx(4)
    assert "observed_30_minute_persistence" in case.coord.battery_control_diagnostics()["battery_load_assumptions"]


def test_known_non_ev_load_is_a_household_floor_not_an_added_profile_load(economic_case):
    case = economic_case([.17, .35], [3, 0], [2500, 2000])
    appliance(case, ev=False, watts=4000)
    assert refresh(case) is None
    plan = case.coord._battery_grid_plan
    assert sum(s.load_kwh for s in plan.slots) == pytest.approx(6)
    assert all(s.pv_charge_kwh == 0 for s in plan.slots)


def test_small_power_jitter_keeps_current_slot_target_stable(economic_case):
    case = economic_case([.17, .35], [2.5, 0], [0, 2000])
    appliance(case)
    assert refresh(case) is None
    old = case.coord._battery_grid_plan
    case.clock["now"] += timedelta(seconds=10)
    case.reading("sensor.appliance_power", 11003, unit_of_measurement="W")
    case.reading("sensor.load_power", 11003, unit_of_measurement="W")
    assert refresh(case) is None
    assert case.coord._battery_grid_plan is old


def test_grid_supported_ev_models_native_minimum_discharge_limit(economic_case):
    case = economic_case([.17, .35], [0, 0], [1000, 2000],
                         battery_max_discharge_entity="number.discharge")
    appliance(case)
    case.reading("sensor.load_power", 12000, unit_of_measurement="W")
    case.reading("sensor.soc", 80)
    case.reading("number.discharge", 100, min=100, max=5000, unit_of_measurement="W")
    assert refresh(case) is None
    first = case.coord._battery_grid_plan.slots[0]
    assert first.discharge_kwh <= .1 + 1e-6
    assert case.coord.battery_control_diagnostics()["battery_load_commitments"][0]["max_discharge_power_w"] == 100


@pytest.mark.parametrize("policy", ["disconnected", "condition", "outside_hours", "target_reached"])
def test_ev_due_to_stop_for_safety_is_not_projected_through_cheap_hours(economic_case, policy):
    case = economic_case([.17, .35], [2.5, 0], [0, 2000])
    data = appliance(case)
    if policy == "disconnected":
        case.reading("binary_sensor.connected", "off")
    elif policy == "condition":
        data.update(enable_condition_entity="binary_sensor.condition", enable_condition_mode="while_running")
        case.reading("binary_sensor.condition", "off")
    elif policy == "outside_hours":
        data["start_after"] = "08:00:00"
    else:
        data.update(ev_soc_entity="sensor.ev_soc", ev_target_soc=80)
        case.reading("sensor.ev_soc", 80)
    assert refresh(case) == "active_ev_policy_uncertain"
    assert case.coord._battery_grid_plan is None


@pytest.mark.parametrize("unit,minimum", [("W", 100), ("kW", .1)])
def test_grid_supported_non_ev_load_applies_global_native_cap(economic_case, unit, minimum):
    case = economic_case([.17, .35], [0, 0], [1000, 2000],
                         battery_max_discharge_entity="number.discharge")
    appliance(case, ev=False, watts=4000)
    case.reading("sensor.soc", 80)
    case.reading("number.discharge", minimum, min=minimum, max=5000, unit_of_measurement=unit)
    assert refresh(case) is None
    assert sum(s.discharge_kwh for s in case.coord._battery_grid_plan.slots if s.start.hour == 0) <= .1 + 1e-6


def test_configured_but_unavailable_discharge_actuator_blocks_ev_projection(economic_case):
    case = economic_case([.17, .35], [2.5, 0], [0, 2000],
                         battery_max_discharge_entity="number.discharge")
    appliance(case)
    assert refresh(case) == "load_discharge_limit_unavailable"


def test_daily_runtime_ceiling_shortens_active_ev_run(economic_case):
    case = economic_case([.17, .35], [2.5, 0], [0, 2000])
    appliance(case, max_daily_runtime=120)
    case.coord.appliance_states["load"] = SimpleNamespace(runtime_today=timedelta(minutes=100))
    assert refresh(case) is None
    assert case.coord._battery_grid_plan.slots[0].end == case.clock["now"] + timedelta(minutes=20)


def test_unknown_runtime_is_not_replaced_by_a_cheap_window_estimate(economic_case):
    case = economic_case([.17, .35], [2.5, 0], [0, 2000])
    appliance(case, remaining_runtime_entity="sensor.missing")
    assert refresh(case) == "active_ev_duration_unknown"


def test_end_before_splits_the_actual_cheap_window(economic_case):
    case = economic_case([.17, .35], [2.5, 0], [0, 2000])
    appliance(case, end_before="00:20:00")
    assert refresh(case) is None
    assert case.coord._battery_grid_plan.slots[0].end == case.clock["now"] + timedelta(minutes=20)


def test_observed_load_uses_thirty_elapsed_minutes_across_autumn_clock_change(economic_case):
    case = economic_case([.17, .35], [2.5, 0], [0, 2000])
    case.clock["now"] = datetime(2026, 10, 25, 2, 45, tzinfo=ZoneInfo("Europe/Berlin"), fold=0)
    case.reading("sensor.load_power", 4000, unit_of_measurement="W")
    rows, _, error = case.coord._battery_load_context(case.clock["now"], case.tariff, [0.] * 24)
    assert error is None
    assert rows[0].end.astimezone(timezone.utc) - rows[0].start.astimezone(timezone.utc) == timedelta(minutes=30)


def test_daily_runtime_reset_is_not_a_fictitious_stop_after_midnight(economic_case):
    case = economic_case([.17, .17], [0, 0], [0, 0])
    data = appliance(case, max_daily_runtime=120)
    case.coord.appliance_states["load"] = SimpleNamespace(runtime_today=timedelta(minutes=60))
    now = case.clock["now"] + timedelta(hours=23, minutes=30)
    case.tariff.windows = [replace(w, start=w.start + timedelta(hours=23, minutes=30),
                                  end=w.end + timedelta(hours=23, minutes=30)) for w in case.tariff.windows]
    end, _ = case.coord._battery_load_run_end("load", data, now, case.tariff)
    assert end is None


def test_manual_override_does_not_inherit_runtime_sensor_stop(economic_case):
    case = economic_case([.17, .35], [2.5, 0], [0, 2000])
    appliance(case, remaining_runtime_entity="sensor.remaining")
    case.reading("sensor.remaining", 15)
    case.coord.appliance_overrides["load"] = True
    assert refresh(case) == "active_ev_duration_unknown"


async def test_corrected_live_context_starts_bounded_real_charge_command(economic_case):
    case = economic_case([.17, .35], [2.5, 0], [0, 2000])
    appliance(case)
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.coord._grid_charge_engaged
        watts = case.controller.engage.await_args.args[0]
        assert 2000 < watts <= 2500
    finally:
        await case.coord.async_stop_battery_controls()


def test_nonzero_discharge_limit_rejects_unsupported_actuator_unit_conversion(economic_case):
    case = economic_case([.17, .35], [0, 0], [1000, 2000],
                         battery_max_discharge_entity="number.discharge")
    appliance(case, ev=False, watts=4000, cheap_price_threshold=0,
              end_before="00:30:00", is_big_consumer=True, battery_discharge_override=2000)
    case.reading("number.discharge", 5, min=.1, max=5, unit_of_measurement="kW")
    # The existing writer does not convert nonzero watt requests to kW.
    assert refresh(case) == "load_discharge_limit_units_unsupported"


@pytest.mark.parametrize("dynamic", [False, True])
def test_on_only_ev_has_no_cheap_or_remaining_runtime_stop(economic_case, dynamic):
    case = economic_case([.17, .35], [2.5, 0], [0, 2000])
    appliance(case, on_only=True, dynamic_current=dynamic, remaining_runtime_entity="sensor.remaining")
    case.reading("sensor.remaining", 15)
    assert refresh(case) == "active_ev_duration_unknown"


def test_fixed_on_only_ev_ignores_operating_window_stop(economic_case):
    case = economic_case([.17, .35], [2.5, 0], [0, 2000])
    appliance(case, on_only=True, end_before="00:15:00")
    assert refresh(case) == "active_ev_duration_unknown"


def test_fixed_on_only_house_load_has_no_grid_funding_cap_or_cheap_stop(economic_case):
    case = economic_case([.17, .35], [0, 0], [1000, 2000],
                         battery_max_discharge_entity="number.discharge")
    appliance(case, ev=False, watts=4000, on_only=True)
    case.reading("number.discharge", 100, min=100, max=5000, unit_of_measurement="W")
    assert refresh(case) is None
    rows = case.coord.battery_control_diagnostics()["battery_load_commitments"]
    assert all(row["max_discharge_power_w"] is None for row in rows)
    assert all(row["end"] == (case.clock["now"] + timedelta(minutes=30)).isoformat() for row in rows)


def test_fixed_on_only_hard_daily_stop_and_big_consumer_cap_remain_effective(economic_case):
    case = economic_case([.17, .35], [0, 0], [1000, 2000],
                         battery_max_discharge_entity="number.discharge")
    appliance(case, watts=4000, on_only=True, max_daily_runtime=120,
              is_big_consumer=True, battery_discharge_override=2000, end_before="00:10:00")
    case.coord.appliance_states["load"] = SimpleNamespace(runtime_today=timedelta(minutes=100))
    case.reading("number.discharge", 2000, min=100, max=5000, unit_of_measurement="W")
    assert refresh(case) is None
    row = case.coord.battery_control_diagnostics()["battery_load_commitments"][0]
    assert row["max_discharge_power_w"] == 2000
    assert row["end"] == (case.clock["now"] + timedelta(minutes=20)).isoformat()


def test_dynamic_on_only_uses_real_time_stop_but_ignores_runtime_stop(economic_case):
    case = economic_case([.17, .35], [0, 0], [1000, 2000])
    appliance(case, on_only=True, dynamic_current=True, remaining_runtime_entity="sensor.remaining",
              end_before="00:30:00")
    case.reading("sensor.remaining", 15)
    assert refresh(case) is None
    assert case.coord._battery_load_commitments[0].end == case.clock["now"] + timedelta(minutes=30)


def test_dynamic_on_only_cannot_extend_current_grid_policy_past_cheap_window(economic_case):
    case = economic_case([.17, .35], [0, 0], [1000, 2000])
    appliance(case, on_only=True, dynamic_current=True, end_before="01:30:00")
    assert refresh(case) == "active_ev_duration_unknown"


def context(case, watts):
    case.reading("sensor.load_power", watts, unit_of_measurement="W")
    rows, _, error = case.coord._battery_load_context(case.clock["now"], case.tariff, [500.] * 24)
    assert error is None
    return rows


def test_observed_house_horizon_starts_when_excess_begins_not_at_baseline(economic_case):
    case = economic_case([.17, .35], [0, 0], [500, 500])
    assert context(case, 500) == ()
    case.clock["now"] += timedelta(minutes=29)
    rows = context(case, 4000)
    assert rows[0].end == case.clock["now"] + timedelta(minutes=30)
    previous_end = rows[0].end
    case.clock["now"] += timedelta(seconds=10)
    assert context(case, 4003)[0].end == previous_end


@pytest.mark.parametrize("event", ["increase", "stop_restart"])
def test_observed_house_horizon_reanchors_after_new_demand(economic_case, event):
    case = economic_case([.17, .35], [0, 0], [500, 500])
    context(case, 4000)
    case.clock["now"] += timedelta(minutes=10)
    if event == "stop_restart":
        assert context(case, 500) == ()
        case.clock["now"] += timedelta(minutes=10)
    rows = context(case, 6000 if event == "increase" else 4000)
    assert rows[0].end == case.clock["now"] + timedelta(minutes=30)


def test_unknown_run_discharge_horizon_reanchors_when_cap_changes(economic_case):
    case = economic_case([.17, .35], [0, 0], [5000, 5000],
                         battery_max_discharge_entity="number.discharge")
    data = appliance(case, ev=False, watts=4000, on_only=True, is_big_consumer=True,
                     battery_discharge_override=2000)
    case.reading("number.discharge", 2000, min=100, max=5000, unit_of_measurement="W")
    rows, _, error = case.coord._battery_load_context(case.clock["now"], case.tariff, [5000.] * 24)
    assert error is None
    case.clock["now"] += timedelta(minutes=10)
    data["battery_discharge_override"] = 1000
    case.reading("sensor.appliance_power", 4000, unit_of_measurement="W")
    case.reading("sensor.load_power", 4000, unit_of_measurement="W")
    rows, _, error = case.coord._battery_load_context(case.clock["now"], case.tariff, [5000.] * 24)
    assert error is None
    assert rows[0].max_discharge_power_w == 1000
    assert rows[0].end == case.clock["now"] + timedelta(minutes=30)


def test_known_house_run_recomputes_base_floor_at_hour_boundaries(economic_case):
    case = economic_case([.17, .17, .35], [0, 0, 0], [5000, 500, 0])
    appliance(case, ev=False, watts=1000)
    case.reading("sensor.load_power", 4000, unit_of_measurement="W")
    assert refresh(case) is None
    slots = case.coord._battery_grid_plan.slots
    assert slots[0].load_kwh == pytest.approx(5)
    assert slots[1].load_kwh == pytest.approx(1.5)


def test_unmetered_base_change_invalidates_plan_even_below_current_house_profile(economic_case):
    case = economic_case([.17, .17, .35], [0, 0, 0], [5000, 500, 0])
    appliance(case, ev=False, watts=1000)
    case.reading("sensor.load_power", 1100, unit_of_measurement="W")
    assert refresh(case) is None
    previous = case.coord._battery_grid_plan
    case.reading("sensor.load_power", 1600, unit_of_measurement="W")
    assert refresh(case) is None
    assert case.coord._battery_grid_plan is not previous
    assert case.coord._battery_grid_plan.slots[1].load_kwh == pytest.approx(1.5)


@pytest.mark.parametrize("dynamic,solar", [(False, 20), (True, 3)])
def test_cheap_transition_is_not_a_stop_when_solar_can_continue_the_load(economic_case, dynamic, solar):
    case = economic_case([.17, .35], [2.5, solar], [0, 1000])
    appliance(case, dynamic_current=dynamic, min_current=6, phases=1)
    assert refresh(case) == "active_ev_duration_unknown"


def test_explicit_stop_beyond_cheap_transition_releases_only_funding_cap(economic_case):
    case = economic_case([.17, .35], [2.5, 3], [0, 1000],
                         battery_max_discharge_entity="number.discharge")
    appliance(case, dynamic_current=True, min_current=6, phases=1, end_before="01:30:00")
    case.reading("number.discharge", 100, min=100, max=5000, unit_of_measurement="W")
    assert refresh(case) is None
    rows = [row for row in case.coord._battery_load_commitments if row.power_w > 0]
    assert len(rows) == 2
    assert rows[0].end == case.clock["now"] + timedelta(hours=1)
    assert rows[0].max_discharge_power_w == 100
    assert rows[1].end == case.clock["now"] + timedelta(minutes=90)
    assert rows[1].max_discharge_power_w is None


def test_helper_only_load_uses_dependency_policy_not_its_own_cheap_runtime_window(economic_case):
    case = economic_case([.17, .35], [0, 0], [1000, 2000],
                         battery_max_discharge_entity="number.discharge")
    appliance(case, ev=False, watts=4000, helper_only=True,
              remaining_runtime_entity="sensor.remaining", end_before="00:10:00")
    case.reading("sensor.remaining", 15)
    case.reading("number.discharge", 100, min=100, max=5000, unit_of_measurement="W")
    assert refresh(case) is None
    rows = case.coord._battery_load_commitments
    assert all(row.max_discharge_power_w is None for row in rows)
    assert all(row.end == case.clock["now"] + timedelta(minutes=30) for row in rows)


def test_optional_discharge_entity_is_safe_with_real_home_assistant_state_machine(economic_case):
    case = economic_case([.17, .35], [2.5, 0], [0, 2000])
    states = StateMachine(case.coord.hass.bus, case.coord.hass.loop)
    states._states_data = case.states
    case.coord.hass.states = states
    assert refresh(case) is None
