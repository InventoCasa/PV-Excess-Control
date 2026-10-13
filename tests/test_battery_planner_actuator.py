"""Forecast purchases use executable total battery charging setpoints."""
from dataclasses import replace
from datetime import timedelta

import pytest

from custom_components.pv_excess_control.battery_planner import build_battery_grid_plan
from tests.test_battery_grid_planner import CONFIG, NOW, make_inputs


def constrained_plan(*, load=.9, solar=0., minutes=60, offset=0, **limits):
    windows, forecast, profile = make_inputs(
        [.1, .5], solar=[solar, 0], now=NOW, minutes=minutes,
    )
    profile[windows[1].start.hour] = load * 60000 / minutes
    config = replace(CONFIG, forced_charge_min_power_w=0.,
                     forced_charge_power_step_w=10., **limits)
    return build_battery_grid_plan(
        NOW + timedelta(minutes=offset), 10., windows, forecast, profile, config,
    )


def test_unexecutable_tiny_residual_cannot_be_counted_as_charge_or_hold():
    result = constrained_plan(load=.006)
    assert result.valid
    assert result.grid_energy_kwh == 0
    assert all(slot.action == "self_consumption" for slot in result.slots)


def test_rounding_down_recalculates_actual_energy_and_savings():
    result = constrained_plan()
    assert result.slots[0].battery_charge_power_w == pytest.approx(1050)
    assert result.grid_energy_kwh == pytest.approx(1.05)
    assert result.slots[1].discharge_kwh == pytest.approx(1.05 * .85)
    assert result.estimated_savings == pytest.approx(1.05 * .85 * .5 - 1.05 * .1)


def test_pv_and_grid_are_quantized_as_one_total_command():
    result = constrained_plan(solar=.2528)
    slot = result.slots[0]
    assert slot.battery_charge_power_w == pytest.approx(1050)
    assert slot.pv_charge_kwh == pytest.approx(.2528)
    assert slot.grid_charge_kwh == pytest.approx(1.05 - .2528)


def test_forced_actuator_maximum_does_not_reduce_native_pv_charge():
    result = constrained_plan(solar=2., forced_charge_max_power_w=600.)
    assert result.grid_energy_kwh == 0
    assert result.slots[0].pv_charge_kwh == pytest.approx(2.)


@pytest.mark.parametrize("offset", [0, 20, 59])
def test_partial_slot_setpoint_respects_remaining_time(offset):
    result = constrained_plan(offset=offset, forced_charge_max_power_w=730.)
    slot = result.slots[0]
    assert slot.battery_charge_power_w == pytest.approx(730.)
    assert slot.grid_charge_kwh == pytest.approx(.730 * (60 - offset) / 60)


def test_nonzero_minimum_is_the_actuator_lattice_origin():
    windows, forecast, profile = make_inputs([.1, .5], loads=[0., .9])
    config = replace(CONFIG, forced_charge_min_power_w=55.,
                     forced_charge_max_power_w=2000., forced_charge_power_step_w=100.)
    result = build_battery_grid_plan(NOW, 10., windows, forecast, profile, config)
    assert result.slots[0].battery_charge_power_w == pytest.approx(1055.)
    for slot in result.slots:
        if slot.action == "charge":
            assert (slot.battery_charge_power_w - 55.) / 100. == pytest.approx(
                round((slot.battery_charge_power_w - 55.) / 100.)
            )


@pytest.mark.parametrize("limits", [
    {"forced_charge_min_power_w": -1},
    {"forced_charge_min_power_w": True},
    {"forced_charge_max_power_w": 0},
    {"forced_charge_max_power_w": float("inf")},
    {"forced_charge_power_step_w": 0},
    {"forced_charge_power_step_w": float("nan")},
    {"forced_charge_min_power_w": 100, "forced_charge_max_power_w": 50},
])
def test_invalid_actuator_constraints_fail_closed(limits):
    windows, forecast, profile = make_inputs([.1, .5], loads=[0., 1.])
    result = build_battery_grid_plan(
        NOW, 10., windows, forecast, profile, replace(CONFIG, **limits),
    )
    assert not result.valid
    assert result.grid_energy_kwh == 0
