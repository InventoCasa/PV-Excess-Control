"""Economic exchanges use extra early purchases to release later holds."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from math import sqrt

import pytest

from custom_components.pv_excess_control import battery_planner as bp
from custom_components.pv_excess_control.models import HourlyForecast, TariffWindow


START = datetime(2031, 2, 3, tzinfo=timezone.utc)
CONFIG = bp.BatteryPlanningConfig(
    capacity_kwh=2., reserve_soc=0., grid_target_soc=100.,
    max_charge_power_w=1000., max_discharge_power_w=1000.,
    roundtrip_efficiency=1., wear_cost_per_kwh=.03, charge_price_limit=.3,
    hold_supported=True, forced_charge_min_power_w=0.,
    forced_charge_max_power_w=1000., forced_charge_power_step_w=10.,
    grid_restart_delay_minutes=5., grid_charge_minimum_minutes=5.,
    grid_charge_startup_seconds=30., grid_charge_enable_seconds=15.,
)
PRICES = (.8, .2, .22, .65, .85, .75, .55, .3)
LOAD = (0., 0., 0., 400., 800., 700., 400., 0.)


def case(*, config=CONFIG, prices=PRICES, solar=None, commitment=(), loads=LOAD,
         charge_hours=(1, 2), initial_soc=0., charge_watts=800.):
    tariffs = [TariffWindow(START + timedelta(hours=i), START + timedelta(hours=i+1), p, False)
               for i, p in enumerate(prices)]
    forecast = [HourlyForecast(w.start, w.end, (solar or [0.] * 8)[i], 0.)
                for i, w in enumerate(tariffs)]
    timeline = bp._timeline(START, tariffs, forecast, list(loads) + [0.] * 16,
                            config.horizon_hours, commitment)
    initial = bp._Node(initial_soc * config.capacity_kwh / 100, 0., 0., None, None)
    node = initial
    for row in timeline:
        hour = row.start.hour
        action = "charge" if hour in charge_hours else "hold" if hour == 3 else "self_consumption"
        held, solar_stored = bp._transition(row, node, config, sqrt(config.roundtrip_efficiency), action)
        if action == "charge":
            grid = charge_watts / 1000 * held.slot.charging_hours - solar_stored / sqrt(config.roundtrip_efficiency)
            node = bp._add_grid_charge(node, held, config, sqrt(config.roundtrip_efficiency), grid, row.elapsed_hours)
        else:
            node = held
    return timeline, initial, node, tariffs, forecast


def disabled_searches(monkeypatch):
    monkeypatch.setattr(bp, "_EPISODE_RESOLUTION_SEARCHES", ())
    for name in ("_EPISODE_COARSE_TRANSITION_BUDGET", "_EPISODE_PRIMARY_TRANSITION_BUDGET",
                 "_REFINEMENT_TRANSITION_BUDGET", "_QUANTITY_REFINEMENT_TRANSITION_BUDGET",
                 "_CHARGE_WINDOW_TRANSITION_BUDGET", "_NATIVE_WINDOW_TRANSITION_BUDGET"):
        monkeypatch.setattr(bp, name, 0)


def plan(node):
    slots = bp._slots(node)
    return bp.BatteryGridPlan(slots, node.grid_energy, 0., "economic_plan",
                              max(s.soc_end for s in slots if s.action == "charge"))


def test_extra_early_purchase_releases_later_hold_with_lower_complete_cost(monkeypatch):
    timeline, initial, incumbent, tariffs, forecast = case()
    disabled_searches(monkeypatch)
    result = bp.build_battery_grid_plan(START, 0., tariffs, forecast, list(LOAD) + [0.] * 16,
                                       CONFIG, continuation_plan=plan(incumbent))
    # Both original partial charge intervals have room for cheaper extra input.
    # Independent reference:1000W for59.5min then60min, and native operation
    # throughout the later demand hours. All added energy is consumed.
    expected_purchase = 1000 * (119.5 / 60) / 1000
    expected_cost = (1000 * (59.5 / 60) / 1000 * .2 + .22
                     + (sum(LOAD) / 1000 - expected_purchase) * .55
                     + expected_purchase * CONFIG.wear_cost_per_kwh)
    actual_cost = sum((max(0., s.load_kwh-s.pv_kwh) + s.grid_charge_kwh-s.discharge_kwh)*s.price
                      + s.discharge_kwh*CONFIG.wear_cost_per_kwh for s in result.slots)
    assert result.grid_energy_kwh == pytest.approx(expected_purchase)
    assert next(s for s in result.slots if s.start.hour == 3).action == "self_consumption"
    assert actual_cost == pytest.approx(expected_cost)
    assert actual_cost < incumbent.cost - .16
    assert result.slots[-1].soc_end == pytest.approx(0.)


def test_cheaper_purchase_after_hold_does_not_hide_useful_earlier_purchase():
    from custom_components.pv_excess_control.battery_charge_hold_exchange import improve_charge_hold_exchange

    timeline, initial, incumbent, _, _ = case(
        prices=(.8, .2, .8, .6, .1, .9, .8, .3),
        loads=(0., 0., 0., 400., 0., 1000., 1000., 0.), charge_hours=(1, 4))
    result = improve_charge_hold_exchange(timeline, CONFIG, 1., initial, incumbent)
    assert result.cost < incumbent.cost - .03
    assert next(s for s in bp._slots(result) if s.start.hour == 3).action == "self_consumption"


def checked_exchange(timeline, initial, incumbent, config=CONFIG):
    from custom_components.pv_excess_control.battery_charge_hold_exchange import improve_charge_hold_exchange
    from custom_components.pv_excess_control.battery_continuation import replay_continuation

    eta = sqrt(config.roundtrip_efficiency)
    checked = replay_continuation(timeline, config, eta, initial, plan(incumbent))
    assert checked is not None, "The original comparison schedule must itself be admissible"
    assert checked.cost == pytest.approx(incumbent.cost)
    result = improve_charge_hold_exchange(timeline, config, eta, initial, incumbent)
    repeated = replay_continuation(timeline, config, eta, initial, plan(result))
    assert repeated is not None
    assert repeated.cost == pytest.approx(result.cost)
    assert repeated.grid_energy == pytest.approx(result.grid_energy)
    assert result.cost <= incumbent.cost + 1e-9
    return result


def test_losses_solar_and_reserve_remain_in_the_complete_comparison():
    config = replace(CONFIG, reserve_soc=10., roundtrip_efficiency=.85)
    timeline, initial, incumbent, _, _ = case(config=config, initial_soc=10.,
        solar=[0., 0., 0., 0., 1.05, 0., 0., 0.],
        loads=(0., 0., 0., 400., 800., 1000., 1000., 0.))
    result = checked_exchange(timeline, initial, incumbent, config)
    assert result.cost < incumbent.cost
    assert result.energy == pytest.approx(.2)
    assert sum(s.pv_charge_kwh for s in bp._slots(result)) == pytest.approx(
        sum(s.pv_charge_kwh for s in bp._slots(incumbent)))
    assert all(s.soc_end >= 10. - 1e-8 for s in bp._slots(result))


def test_external_discharge_limit_is_not_bypassed_by_releasing_a_hold():
    blocked = bp.BatteryLoadCommitment(START+timedelta(hours=3), START+timedelta(hours=4),
                                       0., max_discharge_power_w=0.)
    timeline, initial, incumbent, _, _ = case(commitment=(blocked,))
    result = checked_exchange(timeline, initial, incumbent)
    assert next(s for s in bp._slots(result) if s.start.hour == 3).discharge_kwh == 0.


@pytest.mark.parametrize("protected", [
    {"grid_charge_active": True}, {"grid_charge_prepared": True},
    {"grid_charge_minimum_until": START+timedelta(minutes=5)},
    {"hold_supported": False},
])
def test_current_runtime_authorizations_are_not_changed(protected):
    from custom_components.pv_excess_control.battery_charge_hold_exchange import improve_charge_hold_exchange

    timeline, initial, incumbent, _, _ = case()
    assert improve_charge_hold_exchange(timeline, replace(CONFIG, **protected), 1., initial, incumbent) is incumbent


def test_no_profitable_hold_exchange_preserves_incumbent():
    from custom_components.pv_excess_control.battery_charge_hold_exchange import improve_charge_hold_exchange

    timeline, initial, incumbent, _, _ = case(prices=(.8, .2, .22, .15, .85, .75, .55, .3))
    assert improve_charge_hold_exchange(timeline, CONFIG, 1., initial, incumbent) is incumbent


def test_existing_full_power_purchases_are_not_extended_or_restarted():
    from custom_components.pv_excess_control.battery_charge_hold_exchange import improve_charge_hold_exchange

    config = replace(CONFIG, max_charge_power_w=800., forced_charge_max_power_w=800.)
    timeline, initial, incumbent, _, _ = case(config=config)
    assert improve_charge_hold_exchange(timeline, config, 1., initial, incumbent) is incumbent


def test_all_failed_complete_candidates_preserve_incumbent_and_work_is_bounded(monkeypatch):
    from custom_components.pv_excess_control.battery_charge_hold_exchange import improve_charge_hold_exchange
    from custom_components.pv_excess_control import battery_continuation

    timeline, initial, incumbent, _, _ = case()
    proposals = []

    def reject(*args):
        proposals.append(args[-1])
        return None

    monkeypatch.setattr(battery_continuation, "replay_continuation", reject)
    assert improve_charge_hold_exchange(timeline, CONFIG, 1., initial, incumbent) is incumbent
    assert 0 < len(proposals) <= 64
    assert all(len(p.slots) <= 576 for p in proposals)


def test_subdivided_incumbent_does_not_require_matching_timeline_indices():
    from custom_components.pv_excess_control.battery_continuation import replay_continuation

    timeline, initial, incumbent, _, _ = case()
    # Fresh replay introduces the preparation boundary into the incumbent only.
    refined = replay_continuation(timeline, CONFIG, 1., initial, plan(incumbent))
    assert refined is not None and len(bp._slots(refined)) > len(timeline)
    result = checked_exchange(timeline, initial, refined)
    assert result.cost < refined.cost - .16
    assert result.energy == 0.


def test_current_unconfirmed_charge_is_not_changed():
    from custom_components.pv_excess_control.battery_charge_hold_exchange import improve_charge_hold_exchange

    timeline, initial, incumbent, _, _ = case()
    first_charge = next(s for s in bp._slots(incumbent) if s.action == "charge")
    current = replace(incumbent, previous=initial, slot=first_charge)
    assert improve_charge_hold_exchange(timeline, CONFIG, 1., initial, current) is current


def test_previous_candidate_cannot_displace_better_refined_cold_schedule(monkeypatch):
    from custom_components.pv_excess_control import battery_continuation

    loads = (0., 0., 0., 400., 800., 700., 500., 0.)
    _, _, cold, tariffs, forecast = case(loads=loads)
    _, _, previous, _, _ = case(loads=loads, charge_watts=1000.)
    assert previous.cost < cold.cost
    # Isolate two physically feasible completed inputs to final comparison.
    # The lower-cost original plan has no purchase headroom; refining the other
    # plan can instead avoid65-cent import and shift it to the55-cent suffix.
    monkeypatch.setattr(bp, "_search_schedule", lambda *args, **kwargs: cold)
    plain = bp.build_battery_grid_plan(START, 0., tariffs, forecast, list(loads)+[0.]*16, CONFIG)
    original = battery_continuation.replay_continuation
    calls = []

    def record(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(battery_continuation, "replay_continuation", record)
    retained = bp.build_battery_grid_plan(START, 0., tariffs, forecast, list(loads)+[0.]*16,
                                         CONFIG, continuation_plan=plan(previous))
    assert retained.estimated_savings >= plain.estimated_savings - 1e-9
    assert retained.grid_energy_kwh == pytest.approx(plain.grid_energy_kwh)
    assert retained.slots[-1].soc_end == 0.
    # Two bounded refinement seeds plus the initial previous-plan validation.
    assert 1 < len(calls) <= 129
