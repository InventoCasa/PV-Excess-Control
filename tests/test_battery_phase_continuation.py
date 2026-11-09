"""Previous charge shapes remain candidates through fresh control phases."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.pv_excess_control import battery_planner as bp
from custom_components.pv_excess_control.battery_continuation import replay_continuation
from custom_components.pv_excess_control.models import HourlyForecast, TariffWindow


NOW = datetime(2031, 2, 3, tzinfo=timezone.utc)
CONFIG = bp.BatteryPlanningConfig(
    capacity_kwh=2., reserve_soc=0., grid_target_soc=100.,
    max_charge_power_w=1000., max_discharge_power_w=1000.,
    roundtrip_efficiency=1., wear_cost_per_kwh=.03,
    charge_price_limit=.4, hold_supported=True,
    forced_charge_min_power_w=0., forced_charge_max_power_w=1000.,
    forced_charge_power_step_w=10., grid_restart_delay_minutes=5.,
    grid_charge_minimum_minutes=5., grid_charge_startup_seconds=30.,
    grid_charge_enable_seconds=15., grid_charge_active=True,
)


def inputs():
    tariffs = [TariffWindow(NOW + timedelta(hours=i), NOW + timedelta(hours=i + 1),
                           .1 if i < 3 else .8, False) for i in range(5)]
    forecast = [HourlyForecast(row.start, row.end, 0., 0.) for row in tariffs]
    profile = [0., 0., 0., 1000., 1000.] + [0.] * 19
    return tariffs, forecast, profile


def initial(soc, config):
    return bp._Node(soc * config.capacity_kwh / 100, 0., 0., None, None,
                    charge_active=config.grid_charge_active,
                    charge_grid_delivered=config.grid_charge_active,
                    charge_not_before=config.grid_charge_not_before,
                    charge_minimum_until=config.grid_charge_minimum_until)


def planned_shape(*, current_minutes=15, later_charge=True):
    tariffs, forecast, profile = inputs()
    end = NOW + timedelta(minutes=current_minutes)
    timeline = bp._timeline(NOW, tariffs, forecast, profile, CONFIG.horizon_hours,
                            protected_instants=(end,))
    node = initial(35., CONFIG)
    for row in timeline:
        charging = row.start < end or (later_charge and NOW + timedelta(hours=1) <= row.start < NOW + timedelta(hours=2))
        action = "charge" if charging else "hold" if row.start < NOW + timedelta(hours=3) else "self_consumption"
        held, _ = bp._transition(row, node, CONFIG, 1., action)
        node = (bp._add_grid_charge(node, held, CONFIG, 1., held.slot.charging_hours, row.elapsed_hours)
                if charging else held)
    slots = bp._slots(node)
    return bp.BatteryGridPlan(slots, node.grid_energy, 0., "economic_plan",
                              max(row.soc_end for row in slots if row.action == "charge"))


@pytest.fixture
def previous():
    return planned_shape()

def replay(previous, *, now=NOW, soc=35., config=CONFIG):
    tariffs, forecast, profile = inputs()
    timeline = bp._timeline(now, tariffs, forecast, profile, config.horizon_hours,
                            grid_charge_not_before=config.grid_charge_not_before)
    return replay_continuation(timeline, config, 1., initial(soc, config), previous)


@pytest.mark.parametrize("prepared", [False, True])
def test_unconfirmed_current_charge_is_revalidated_as_a_fresh_start(previous, prepared):
    before = deepcopy(previous)
    now = NOW + timedelta(minutes=2)
    config = replace(CONFIG, grid_charge_active=False, grid_charge_prepared=prepared)
    result = replay(previous, now=now, config=config)
    assert result is not None
    rows = bp._slots(result)
    assert rows[0].start == now
    assert rows[0].grid_charge_available_at == now + timedelta(seconds=0 if prepared else 30)
    assert rows[0].grid_charge_kwh == pytest.approx((13 * 60 - (0 if prepared else 30)) / 3600)
    assert result.energy == pytest.approx(0., abs=1e-8)
    assert previous == before


@pytest.mark.parametrize("prepared", [False, True])
def test_new_current_charge_still_needs_fresh_minimum_duration(previous, prepared):
    config = replace(CONFIG, grid_charge_active=False, grid_charge_prepared=prepared)
    assert replay(previous, now=NOW + timedelta(minutes=11), config=config) is None


@pytest.mark.parametrize("prepared", [False, True])
def test_new_current_charge_cannot_bypass_restart_delay(previous, prepared):
    config = replace(CONFIG, grid_charge_active=False, grid_charge_prepared=prepared,
                     grid_charge_not_before=NOW + timedelta(minutes=3))
    assert replay(previous, now=NOW + timedelta(minutes=2), config=config) is None


def test_active_episode_stays_exact_while_later_purchase_shrinks_to_fresh_capacity(previous):
    before = deepcopy(previous)
    config = replace(CONFIG, grid_charge_minimum_until=NOW + timedelta(minutes=5))
    result = replay(previous, soc=40., config=config)
    assert result is not None
    rows = bp._slots(result)
    current = [row for row in rows if row.end <= NOW + timedelta(minutes=15)]
    later = next(row for row in rows if row.start >= NOW + timedelta(hours=1) and row.grid_charge_kwh > 0)
    assert all(row.battery_charge_power_w == pytest.approx(1000.) for row in current)
    assert sum(row.grid_charge_kwh for row in current) == pytest.approx(.25)
    assert later.battery_charge_power_w == pytest.approx(950.)
    assert later.grid_charge_kwh < previous.slots[2].grid_charge_kwh
    assert max(row.soc_end for row in rows) <= 100.
    assert result.energy == pytest.approx(0., abs=1e-8)
    assert previous == before
    rerun = replay(bp.BatteryGridPlan(rows, result.grid_energy, 0., "economic_plan", 100.),
                   soc=40., config=config)
    assert rerun is not None and rerun.grid_energy == pytest.approx(result.grid_energy)


def test_protected_current_command_cannot_shrink_to_hide_its_own_capacity_violation(previous):
    config = replace(CONFIG, grid_charge_minimum_until=NOW + timedelta(minutes=5))
    assert replay(previous, soc=96., config=config) is None


def test_active_current_episode_keeps_original_minimum_deadline(previous):
    deadline = NOW + timedelta(minutes=16)
    assert replay(previous, config=replace(CONFIG, grid_charge_minimum_until=deadline)) is None


def test_current_or_later_commands_reject_changed_actuator_limits(previous):
    assert replay(previous, config=replace(CONFIG, max_charge_power_w=500.)) is None


def test_active_context_does_not_replay_an_old_noncharging_current_slot(previous):
    assert replay(previous, now=NOW + timedelta(minutes=20)) is None


@pytest.mark.parametrize("protected", [False, True])
def test_active_current_tail_can_shrink_after_unchanged_minimum(protected):
    previous = planned_shape(current_minutes=60, later_charge=False)
    deadline = NOW + timedelta(minutes=5) if protected else None
    config = replace(CONFIG, grid_charge_minimum_until=deadline)
    result = replay(previous, soc=95., config=config)
    assert result is not None
    charged = [row for row in bp._slots(result) if row.action == "charge"]
    assert charged[-1].end == NOW + timedelta(hours=1)
    assert all(row.grid_charge_kwh > 0 for row in charged)
    assert max(row.soc_end for row in charged) <= 100.
    assert result.energy == pytest.approx(0., abs=1e-8)
    if protected:
        assert charged[0].end == deadline
        assert charged[0].battery_charge_power_w == pytest.approx(1000.)
        assert charged[1].start == deadline
        assert charged[1].battery_charge_power_w == pytest.approx(10.)
    else:
        assert charged[0].battery_charge_power_w == pytest.approx(100.)
