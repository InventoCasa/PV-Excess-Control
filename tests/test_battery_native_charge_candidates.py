"""Bounded complete trajectories remain useful when broader searches exhaust."""
from dataclasses import replace
from datetime import timedelta

import pytest

from custom_components.pv_excess_control import battery_planner as bp
from tests.test_battery_charge_opportunities import START, CONFIG, solar_refill_inputs, assert_physical_plan


def disable_broad_searches(monkeypatch):
    for name in ['_EPISODE_COARSE_TRANSITION_BUDGET','_EPISODE_PRIMARY_TRANSITION_BUDGET',
                 '_REFINEMENT_TRANSITION_BUDGET','_QUANTITY_REFINEMENT_TRANSITION_BUDGET',
                 '_CHARGE_WINDOW_TRANSITION_BUDGET']:
        monkeypatch.setattr(bp,name,0)


def test_complete_native_future_candidate_survives_exhausted_broad_budgets(monkeypatch):
    disable_broad_searches(monkeypatch)
    plan=bp.build_battery_grid_plan(START,0,*solar_refill_inputs(),CONFIG)
    assert plan.valid and plan.grid_energy_kwh>0 and plan.estimated_savings>0
    assert_physical_plan(plan)


def test_bounded_native_candidates_preserve_solar_and_terminal_inventory(monkeypatch):
    disable_broad_searches(monkeypatch)
    args=solar_refill_inputs()
    native=bp.build_battery_grid_plan(START,0,*args,replace(CONFIG,charge_price_limit=0))
    result=bp.build_battery_grid_plan(START,0,*args,CONFIG)
    assert result.valid and result.grid_energy_kwh>0
    assert sum(s.pv_charge_kwh for s in result.slots)==pytest.approx(sum(s.pv_charge_kwh for s in native.slots))
    assert result.slots[-1].soc_end<=native.slots[-1].soc_end+1e-7


def test_native_candidates_do_not_override_future_restart_pause(monkeypatch):
    disable_broad_searches(monkeypatch)
    cfg=replace(CONFIG,grid_charge_not_before=START+timedelta(hours=1))
    plan=bp.build_battery_grid_plan(START,0,*solar_refill_inputs(),cfg)
    assert plan.valid and not any(s.action=='charge' and s.start<cfg.grid_charge_not_before for s in plan.slots)
