"""One stable EV forecast feeds consumer preview and battery future demand."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.test_battery_control_economics import economic_case  # noqa: F401


def configure(case, *, key="car", soc=42, **extra):
    data={"appliance_name":key,"appliance_entity":"switch."+key,"actual_power_entity":"sensor."+key+"_power",
          "dynamic_current":True,"current_entity":"number."+key,"min_current":6.,"max_current":16.,
          "phases":3,"nominal_power":11000.,"ev_soc_entity":"sensor."+key+"_soc",
          "ev_connected_entity":"binary_sensor."+key,"ev_target_soc":100.,
          "ev_battery_capacity_entity":"sensor."+key+"_capacity","ev_charge_efficiency":.9,
          "ev_reserve_all_pv":True,"cheap_price_threshold":.17,"allow_grid_supplement":True,
          "cheap_grid_target_current":16.,"max_grid_power":12000.,**extra}
    case.coord.config_entry.subentries[key]=SimpleNamespace(data=data)
    case.reading("switch."+key,"off");case.reading("sensor."+key+"_power",0,unit_of_measurement="W")
    case.reading("number."+key,0,min=0,max=16,step=.1)
    case.reading("sensor."+key+"_soc",soc)
    case.reading("binary_sensor."+key,"on")
    case.reading("sensor."+key+"_capacity",235440,unit_of_measurement="kJ")
    return data


def test_capacity_sensor_units_and_shared_frozen_windows(economic_case):
    case=economic_case([.3,.16,.3],[0,3,0],[500]*3)
    configure(case)
    forecasts,key=case.coord._get_ev_forecasts(case.tariff)
    model=forecasts['car']
    assert model.remaining_ac_kwh == pytest.approx(65.4*.58/.9)
    plan=case.coord.planner.create_plan(forecast=case.coord._forecast_data,tariff=case.tariff,
        appliances=case.coord._get_appliance_configs(),appliance_states=case.coord._get_appliance_states(case.coord._get_appliance_configs(),update_runtime=False),
        battery_config=None,current_soc=10,export_limit=None,now=case.clock['now'],ev_forecasts=forecasts)
    assert plan.ev_forecasts['car'] is model
    assert [(e.window.start,e.window.end) for e in plan.entries if e.appliance_id=='car']==[(r.start,r.end) for r in model.runs]
    commitments,_,reason=case.coord._battery_load_context(case.clock['now'],case.tariff,[500.]*24)
    assert reason is None
    ev=[x for x in commitments if x.appliance_id=='car']
    assert [(r.start,r.end,r.power_w) for r in ev]==[(r.start,r.end,r.power_w) for r in model.runs]
    assert all(r.reserve_all_pv for r in ev)


def test_advancing_worker_clock_does_not_slide_ev_deadline(economic_case):
    case=economic_case([.16,.3],[0,0],[500]*2);configure(case,soc=99)
    first,key=case.coord._get_ev_forecasts(case.tariff)
    case.clock['now']+=timedelta(seconds=8)
    later,later_key=case.coord._get_ev_forecasts(case.tariff)
    assert later is first and later_key==key


def test_missed_charge_is_reestimated_from_actual_soc_at_refresh(economic_case):
    case=economic_case([.16,.16,.3],[0,0,0],[500]*3);configure(case,soc=99)
    first,_=case.coord._get_ev_forecasts(case.tariff)
    case.clock['now']+=timedelta(minutes=20)
    second,_=case.coord._get_ev_forecasts(case.tariff)
    assert second['car'].remaining_ac_kwh==first['car'].remaining_ac_kwh
    assert second['car'].runs[0].start==case.clock['now']


def test_target_and_connection_changes_invalidate_without_waiting_epoch(economic_case):
    case=economic_case([.16,.3],[0,0],[500]*2);configure(case)
    case.coord._get_ev_forecasts(case.tariff)
    case.reading('sensor.car_soc',100)
    forecast,_=case.coord._get_ev_forecasts(case.tariff)
    assert forecast['car'].status=='target_reached' and not forecast['car'].runs
    case.reading('binary_sensor.car','off')
    forecast,_=case.coord._get_ev_forecasts(case.tariff)
    assert forecast['car'].status=='disconnected'


def test_measured_same_vehicle_run_replaces_only_its_overlapping_forecast(economic_case):
    case=economic_case([.16,.3,.16,.3],[2,0,2,0],[500]*4);configure(case)
    case.reading('switch.car','on');case.reading('sensor.car_power',7000,unit_of_measurement='W')
    case.reading('sensor.load_power',7500,unit_of_measurement='W')
    configure(case,key='second')
    commitments,_,reason=case.coord._battery_load_context(case.clock['now'],case.tariff,[500.]*24)
    assert reason is None
    active=[r for r in commitments if r.start<=case.clock['now']<r.end and r.power_w>0]
    assert sum(r.power_w for r in active if r.appliance_id=='car')==7000
    assert sum(r.power_w for r in active if r.appliance_id=='second')==11040
    assert any(r.appliance_id=='car' and r.start>=case.clock['now']+timedelta(hours=2) for r in commitments)


def test_missing_capacity_or_soc_is_explicit_and_preserves_aggregate_fallback(economic_case):
    case=economic_case([.16,.3],[2,0],[500]*2);configure(case)
    case.reading('sensor.car_capacity','unavailable')
    forecasts,_=case.coord._get_ev_forecasts(case.tariff)
    assert forecasts['car'].status=='capacity_unavailable'
    commitments,_,reason=case.coord._battery_load_context(case.clock['now'],case.tariff,[500.]*24)
    assert reason is None and not any(r.appliance_id=='car' for r in commitments)
    assert 'future_ev_unknown' in case.coord._battery_load_assumptions


def test_paused_vehicle_does_not_reserve_future_solar(economic_case):
    case=economic_case([.16,.3],[2,0],[500]*2);configure(case)
    case.coord.appliance_paused={'car':True}
    forecasts,_=case.coord._get_ev_forecasts(case.tariff)
    assert 'car' not in forecasts


def test_actual_power_sensor_age_does_not_erase_known_future_soc_demand(economic_case):
    case=economic_case([.3,.16,.3],[0,2,0],[500]*3);configure(case)
    case.clock['now']+=timedelta(minutes=6)
    case.reading('sensor.load_power',500,unit_of_measurement='W')
    forecasts,_=case.coord._get_ev_forecasts(case.tariff)
    assert forecasts['car'].status=='available' and forecasts['car'].runs


def test_active_ev_near_target_does_not_reserve_whole_cheap_window(economic_case):
    case=economic_case([.16,.16,.3],[2,2,0],[500]*3);configure(case,soc=99)
    case.reading('switch.car','on');case.reading('sensor.car_power',7000,unit_of_measurement='W')
    case.reading('sensor.load_power',7500,unit_of_measurement='W')
    forecast,_=case.coord._get_ev_forecasts(case.tariff)
    run=forecast['car'].runs[0]
    assert run.power_w==7000 and run.target_current==16
    assert (run.end-run.start).total_seconds()==pytest.approx(65.4*.01/.9/7*3600,abs=1e-5)
    commitments,_,reason=case.coord._battery_load_context(case.clock['now'],case.tariff,[500.]*24)
    assert reason is None
    measured=[r for r in commitments if r.appliance_id=='car']
    assert max(r.end for r in measured)==run.end


def test_dependency_condition_blocker_removes_future_ev(economic_case):
    case=economic_case([.16,.3],[0,0],[500]*2)
    configure(case,requires_appliance='dependency')
    configure(case,key='dependency',ev_battery_capacity_entity=None,ev_battery_capacity_kwh=None,
              ev_soc_entity=None,ev_connected_entity=None,dynamic_current=False,
              enable_condition_entity='binary_sensor.ready')
    case.reading('binary_sensor.ready','off')
    models,_=case.coord._get_ev_forecasts(case.tariff)
    assert not models['car'].runs
    assert models['car'].status=='dependency_unavailable'



@pytest.mark.asyncio
async def test_epoch_crossing_during_worker_does_not_discard_unchanged_ev_request(economic_case):
    import asyncio
    case=economic_case([.16,.3],[0,0],[500]*2)
    case.clock['now']+=timedelta(minutes=4,seconds=58)
    configure(case,soc=99)
    case.reading('sensor.soc',10)
    case.reading('sensor.load_power',500,unit_of_measurement='W')
    case.reading('sensor.pv_power',0,unit_of_measurement='W')
    case.reading('sensor.price',.16)
    calls=[]
    async def executor(function,*args):
        calls.append(True)
        result=function(*args)
        if len(calls)==1:
            case.clock['now']+=timedelta(seconds=5)
        await asyncio.sleep(0)
        return result
    case.coord.hass.async_add_executor_job=executor
    assert await case.coord._async_refresh_battery_grid_plan(case.tariff,None) is None
    assert len(calls)==1
