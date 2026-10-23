"""Explicit EV demand settings validate and survive both appliance wizards."""
from unittest.mock import AsyncMock

import pytest

from tests.test_config_flow_subentry import _make_subentry_flow, _schema_has_key


BASE={"dynamic_current":True,"current_entity":"number.car","min_current":6.,"max_current":16.,
      "ev_soc_entity":"sensor.car_soc","ev_connected_entity":"binary_sensor.car_connected",
      "ev_target_soc":100.,"ev_battery_capacity_entity":"sensor.capacity",
      "ev_charge_efficiency":.9,"ev_reserve_all_pv":True}


@pytest.mark.parametrize('step',['current','reconfigure_current'])
async def test_ev_fields_roundtrip_and_mapping_removal(step):
    flow=_make_subentry_flow();flow._data={'unrelated_setting':123}
    following='async_step_constraints' if step=='current' else 'async_step_reconfigure_constraints'
    setattr(flow,following,AsyncMock(return_value={'type':'next'}))
    show=await getattr(flow,'async_step_'+step)(None)
    for key in ['ev_battery_capacity_entity','ev_battery_capacity_kwh','ev_charge_efficiency','ev_reserve_all_pv']:
        assert _schema_has_key(show,key)
    result=await getattr(flow,'async_step_'+step)(dict(BASE))
    assert result['type']=='next' and flow._data['ev_battery_capacity_entity']=='sensor.capacity'
    assert flow._data['unrelated_setting']==123
    removed={k:v for k,v in BASE.items() if k not in ['ev_battery_capacity_entity','ev_reserve_all_pv','ev_charge_efficiency']}
    removed['ev_battery_capacity_kwh']=60
    await getattr(flow,'async_step_'+step)(removed)
    assert 'ev_battery_capacity_entity' not in flow._data
    assert flow._data['ev_battery_capacity_kwh']==60


@pytest.mark.parametrize('step',['current','reconfigure_current'])
@pytest.mark.parametrize('change,key',[
    ({'ev_battery_capacity_entity':'switch.capacity'},'ev_battery_capacity_entity'),
    ({'ev_battery_capacity_entity':'sensor.'},'ev_battery_capacity_entity'),
    ({'ev_battery_capacity_kwh':0},'ev_battery_capacity_kwh'),
    ({'ev_battery_capacity_kwh':float('nan')},'ev_battery_capacity_kwh'),
    ({'ev_battery_capacity_kwh':True},'ev_battery_capacity_kwh'),
    ({'ev_charge_efficiency':0},'ev_charge_efficiency'),
    ({'ev_charge_efficiency':float('inf')},'ev_charge_efficiency'),
    ({'ev_charge_efficiency':1.1},'ev_charge_efficiency'),
    ({'ev_soc_entity':None},'ev_battery_capacity_entity'),
    ({'ev_connected_entity':None},'ev_battery_capacity_entity'),
    ({'dynamic_current':False},'ev_battery_capacity_entity'),
    ({'ev_reserve_all_pv':'yes'},'ev_reserve_all_pv'),
])
async def test_invalid_ev_settings_never_advance(step,change,key):
    flow=_make_subentry_flow();following='async_step_constraints' if step=='current' else 'async_step_reconfigure_constraints'
    setattr(flow,following,AsyncMock(return_value={'type':'next'}))
    result=await getattr(flow,'async_step_'+step)({**BASE,**change})
    assert result['type']=='form' and key in result['errors']
    getattr(flow,following).assert_not_awaited()
