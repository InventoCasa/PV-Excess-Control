"""Conservative future EV solar reservation never invents external energy."""
from dataclasses import replace

import pytest

from tests.test_battery_ev_commitments import NOW, CONFIG, inputs
from custom_components.pv_excess_control import battery_planner as bp


def test_all_pv_reservation_withholds_surplus_without_inventing_ev_draw():
    tariffs,solar,profile=inputs([.1,.4],[8,0],[1,2])
    run=bp.BatteryLoadCommitment(NOW,tariffs[0].end,2000,reserve_all_pv=True,appliance_id='car')
    cfg=replace(CONFIG,charge_price_limit=0)
    plan=bp.build_battery_grid_plan(NOW,20,tariffs,solar,profile,cfg,load_commitments=(run,))
    assert plan.valid
    first=plan.slots[0]
    assert first.external_load_kwh==2
    assert first.reserved_pv_kwh==2
    assert first.pv_charge_kwh==0
    assert first.withheld_pv_kwh==5  # household1+EV2+withheld5 = actualPV8
    assert first.pv_kwh+first.reserved_pv_kwh+first.withheld_pv_kwh==8


def test_default_reservation_keeps_real_surplus_available():
    tariffs,solar,profile=inputs([.1,.4],[8,0],[1,2])
    run=bp.BatteryLoadCommitment(NOW,tariffs[0].end,2000)
    result=bp.build_battery_grid_plan(NOW,20,tariffs,solar,profile,replace(CONFIG,charge_price_limit=0),load_commitments=(run,))
    assert result.valid and result.slots[0].pv_charge_kwh>0 and result.slots[0].withheld_pv_kwh==0
