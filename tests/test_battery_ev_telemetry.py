"""Optional EV telemetry must not invalidate a fresh aggregate load reading."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest

from tests.test_battery_control_economics import economic_case  # noqa: F401
from tests.test_battery_load_context import appliance, refresh


def context(case):
    return case.coord._battery_load_context(case.clock["now"], case.tariff, [500.] * 24)


def demand(rows, when, profile=500):
    active = [row for row in rows if row.start <= when < row.end]
    return (max([profile] + [row.household_power_floor_w for row in active
                            if row.household_power_floor_w is not None])
            + sum(row.power_w for row in active))


@pytest.mark.parametrize("missing", ["power", "switch", "connected", "all", "stale"])
def test_optional_ev_outage_keeps_aggregate_demand_and_a_valid_plan(economic_case, missing):
    case = economic_case([.17, .35], [2, 0], [500, 500])
    appliance(case, watts=6000)
    if missing == "stale":
        case.clock["now"] += timedelta(seconds=301)
        for entity, value in (("sensor.soc", 10), ("sensor.pv_power", 2000),
                              ("sensor.price", .17), ("sensor.forecast", 2)):
            case.reading(entity, value)
    else:
        entities = {"power": "sensor.appliance_power", "switch": "switch.load",
                    "connected": "binary_sensor.connected"}
        for entity in entities.values() if missing == "all" else [entities[missing]]:
            case.reading(entity, "unavailable")
    case.reading("sensor.load_power", 6500, unit_of_measurement="W")
    rows, _, error = context(case)
    assert error is None
    assert demand(rows, case.clock["now"] + timedelta(seconds=90)) == 6500
    assert "ev_telemetry_unavailable_aggregate_load" in case.coord._battery_load_assumptions
    assert refresh(case) is None
    assert case.coord._battery_grid_plan.valid


def test_unavailable_ev_demand_expires_without_a_new_aggregate_report(economic_case):
    case = economic_case([.17, .35], [0, 0], [500, 500])
    appliance(case, watts=6000)
    case.reading("sensor.appliance_power", "unavailable")
    case.reading("sensor.load_power", 6500, unit_of_measurement="W")
    rows, key, error = context(case)
    assert error is None
    assert demand(rows, case.clock["now"] + timedelta(seconds=119)) == 6500
    case.clock["now"] += timedelta(seconds=60)
    repeated, repeated_key, error = context(case)
    assert error is None
    assert key == repeated_key
    assert repeated[0].end == rows[0].end
    case.clock["now"] += timedelta(seconds=61)
    expired, _, error = context(case)
    assert error is None
    assert demand(expired, case.clock["now"]) == 500
    case.reading("sensor.load_power", 6500, unit_of_measurement="W")
    renewed, _, error = context(case)
    assert error is None
    assert demand(renewed, case.clock["now"]) == 6500


def test_outage_does_not_assume_an_unenforced_previous_ev_discharge_limit(economic_case):
    case = economic_case([.17, .35], [0, 0], [500, 500],
                         battery_max_discharge_entity="number.discharge")
    appliance(case, watts=6000)
    case.reading("number.discharge", 100, min=100, max=5000, unit_of_measurement="W")
    case.reading("sensor.load_power", 6500, unit_of_measurement="W")
    assert any(row.max_discharge_power_w == 100 for row in context(case)[0])
    case.clock["now"] += timedelta(seconds=10)
    for entity in ("switch.load", "sensor.appliance_power", "binary_sensor.connected"):
        case.reading(entity, "unavailable")
    # Ordinary appliance control can release its cap when it cannot confirm
    # that the EV is running. The battery plan must model that native exposure.
    case.reading("number.discharge", 5000, min=100, max=5000, unit_of_measurement="W")
    case.reading("sensor.load_power", 6500, unit_of_measurement="W")
    rows, _, error = context(case)
    assert error is None
    assert not any(row.max_discharge_power_w is not None for row in rows)
    assert demand(rows, case.clock["now"]) == 6500


def test_outage_does_not_double_count_other_metered_loads(economic_case):
    case = economic_case([.17, .35], [0, 0], [500, 500])
    appliance(case, watts=6000)
    case.reading("sensor.appliance_power", "unavailable")
    for name, watts, ev in (("house", 4000, False), ("other_ev", 3000, True)):
        data = {"appliance_entity": f"switch.{name}", "actual_power_entity": f"sensor.{name}",
                "allow_grid_supplement": True, "cheap_price_threshold": .18,
                "nominal_power": watts, "end_before": "00:30:00"}
        if ev:
            data["ev_connected_entity"] = f"binary_sensor.{name}"
            case.reading(f"binary_sensor.{name}", "on")
        case.coord.config_entry.subentries[name] = SimpleNamespace(data=data)
        case.reading(f"switch.{name}", "on")
        case.reading(f"sensor.{name}", watts, unit_of_measurement="W")
    case.reading("sensor.load_power", 13500, unit_of_measurement="W")
    rows, _, error = context(case)
    assert error is None
    assert demand(rows, case.clock["now"] + timedelta(seconds=90)) == 13500
    assert demand(rows, case.clock["now"] + timedelta(minutes=3)) == 7500


def test_recovery_discards_unknown_composition_and_resumes_clean_learning(economic_case):
    case = economic_case([.17, .35], [0, 0], [500, 500])
    appliance(case, watts=6000)
    case.reading("sensor.load_power", 6500, unit_of_measurement="W")
    case.coord._observe_battery_load()
    before = deepcopy(case.coord._battery_load_hours)
    case.clock["now"] += timedelta(seconds=30)
    case.reading("sensor.appliance_power", "unavailable")
    case.reading("sensor.load_power", 6500, unit_of_measurement="W")
    assert context(case)[2] is None
    case.coord._observe_battery_load()
    assert case.coord._battery_load_hours == before
    case.clock["now"] += timedelta(seconds=30)
    case.reading("sensor.appliance_power", 0, unit_of_measurement="W")
    case.reading("switch.load", "off")
    case.reading("sensor.load_power", 500, unit_of_measurement="W")
    rows, _, error = context(case)
    assert error is None
    assert demand(rows, case.clock["now"]) == 500
    assert "ev_telemetry_unavailable_aggregate_load" not in case.coord._battery_load_assumptions
    case.coord._observe_battery_load()
    assert case.coord._battery_load_hours == before
    case.clock["now"] += timedelta(seconds=30)
    case.reading("sensor.load_power", 500, unit_of_measurement="W")
    case.coord._observe_battery_load()
    assert case.coord._battery_load_hours[0][0] == 500
    assert case.coord._battery_load_hours[0][1] == 30


def test_optional_ev_outage_cannot_bypass_missing_required_total_load(economic_case):
    case = economic_case([.17, .35], [0, 0], [500, 500])
    appliance(case)
    case.reading("sensor.appliance_power", "unavailable")
    case.reading("sensor.load_power", "unavailable")
    assert refresh(case) == "load_power_missing_or_stale"
    assert case.coord._battery_grid_plan is None


def test_stale_off_state_does_not_allow_unknown_ev_draw_into_learning(economic_case):
    case = economic_case([.17, .35], [0, 0], [500, 500], battery_input_max_age_seconds=60)
    appliance(case, watts=0)
    case.reading("switch.load", "off")
    case.reading("sensor.load_power", 500, unit_of_measurement="W")
    case.coord._observe_battery_load()
    case.clock["now"] += timedelta(seconds=61)
    case.reading("sensor.appliance_power", "unavailable")
    case.reading("sensor.load_power", 6500, unit_of_measurement="W")
    case.coord._observe_battery_load()
    assert case.coord._battery_load_hours[0][1] == 0
    assert case.coord._battery_load_last[1] is None


def test_missing_optional_ev_runtime_uses_bounded_observed_demand(economic_case):
    case = economic_case([.17, .35], [0, 0], [500, 500])
    appliance(case, watts=6000, remaining_runtime_entity="sensor.remaining")
    case.reading("sensor.remaining", "unavailable")
    case.reading("sensor.load_power", 6500, unit_of_measurement="W")
    rows, _, error = context(case)
    assert error is None
    assert demand(rows, case.clock["now"] + timedelta(seconds=90)) == 6500
    assert demand(rows, case.clock["now"] + timedelta(minutes=3)) == 500


def test_event_driven_ev_states_remain_valid_with_fresh_metered_power(economic_case):
    case = economic_case([.17, .35], [0, 0], [500, 500])
    appliance(case, watts=6000)
    case.clock["now"] += timedelta(minutes=10)
    case.reading("sensor.appliance_power", 6000, unit_of_measurement="W")
    case.reading("sensor.load_power", 6500, unit_of_measurement="W")
    rows, _, error = context(case)
    assert error is None
    assert any(row.power_w == 6000 for row in rows)
    assert "ev_telemetry_unavailable_aggregate_load" not in case.coord._battery_load_assumptions


def test_fresh_zero_ev_power_remains_zero_with_unavailable_control_states(economic_case):
    case = economic_case([.17, .35], [0, 0], [500, 500])
    appliance(case, watts=0)
    case.reading("switch.load", "unavailable")
    case.reading("binary_sensor.connected", "unavailable")
    case.reading("sensor.load_power", 500, unit_of_measurement="W")
    rows, _, error = context(case)
    assert error is None
    assert rows == ()
    assert "ev_telemetry_unavailable_aggregate_load" not in case.coord._battery_load_assumptions


async def test_whole_wallbox_outage_keeps_automatic_battery_control(economic_case):
    case = economic_case([.1, .4], [0, 0], [500, 2000])
    appliance(case, watts=6000)
    for entity in ("switch.load", "binary_sensor.connected", "sensor.appliance_power"):
        case.reading(entity, "unavailable")
    case.reading("sensor.load_power", 6500, unit_of_measurement="W")
    try:
        await case.coord._run_grid_charge_state_machine(case.tariff, None)
        assert case.coord._battery_control_state in ("charging", "holding", "self_consumption")
        assert case.coord._battery_grid_plan.valid
        assert case.coord._battery_grid_plan.grid_energy_kwh > 0
        case.controller.disengage.assert_not_awaited()
    finally:
        await case.coord.async_stop_battery_controls()


def test_recovered_ev_telemetry_restores_observed_demand_and_policy_limit(economic_case):
    case = economic_case([.17, .35], [0, 0], [500, 500],
                         battery_max_discharge_entity="number.discharge")
    appliance(case, watts=6000)
    case.reading("number.discharge", 100, min=100, max=5000, unit_of_measurement="W")
    case.reading("sensor.load_power", 6500, unit_of_measurement="W")
    assert context(case)[2] is None
    case.reading("sensor.appliance_power", "unavailable")
    assert not any(row.max_discharge_power_w is not None for row in context(case)[0])
    case.clock["now"] += timedelta(seconds=30)
    case.reading("sensor.appliance_power", 6000, unit_of_measurement="W")
    case.reading("sensor.load_power", 6500, unit_of_measurement="W")
    rows, _, error = context(case)
    assert error is None
    assert any(row.power_w == 6000 and row.max_discharge_power_w == 100 for row in rows)
    assert demand(rows, case.clock["now"]) == 6500
    assert "ev_telemetry_unavailable_aggregate_load" not in case.coord._battery_load_assumptions
