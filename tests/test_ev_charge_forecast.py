"""SOC demand follows chronological tariff eligibility, not a one-hour quota."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone, time
from importlib import import_module, util
from zoneinfo import ZoneInfo

import pytest

from custom_components.pv_excess_control.models import TariffInfo, TariffWindow
from tests.test_optimizer import _make_appliance, _make_state

NOW = datetime(2030, 6, 1, 10, tzinfo=timezone.utc)


def module():
    name = "custom_components.pv_excess_control.ev_charge_forecast"
    assert util.find_spec(name) is not None, "Shared EV demand forecasting is not implemented"
    return import_module(name)


def inputs(prices=None, *, soc=42., capacity=65.4, efficiency=.9, now=NOW):
    m = module()
    app = replace(_make_appliance(id="car", dynamic_current=True, phases=3, min_current=6, max_current=16,
                  allow_grid_supplement=True), ev_soc_entity="sensor.car_soc",
                  ev_connected_entity="binary_sensor.car_plug", ev_target_soc=100.,
                  cheap_price_threshold=.17, cheap_grid_target_current=16.,
                  max_grid_power=12000., ev_battery_capacity_kwh=capacity,
                  ev_charge_efficiency=efficiency, ev_reserve_all_pv=True)
    state = replace(_make_state("car"), ev_connected=True, ev_soc=soc,
                    ev_soc_observed_at=now, ev_capacity_kwh=capacity)
    prices = prices if prices is not None else [.18] * 2 + [.16] * 12 + [.30] * 2
    tariffs = [TariffWindow(now + timedelta(minutes=15*i), now + timedelta(minutes=15*(i+1)), p, False)
               for i, p in enumerate(prices)]
    tariff = TariffInfo(prices[0], .08, .1, .8, tariffs)
    return m, app, state, tariff


def test_all_three_eligible_hours_are_forecast_when_soc_needs_longer():
    m, app, state, tariff = inputs()
    result = m.build_ev_charge_forecast(NOW, app, state, tariff, grid_voltage=230)
    assert result.status == "available"
    assert result.remaining_ac_kwh == pytest.approx(65.4 * .58 / .9)
    assert result.required_minutes == pytest.approx(65.4 * .58 / .9 / 11.04 * 60)
    assert len(result.runs) == 1
    assert result.runs[0].start == NOW + timedelta(minutes=30)
    assert result.runs[0].end == NOW + timedelta(minutes=210)
    assert result.runs[0].power_w == 11040
    assert result.planned_ac_kwh == pytest.approx(33.12)
    assert result.unmet_ac_kwh > 0


def test_target_bounds_partial_last_window_without_forced_runtime():
    m, app, state, tariff = inputs(soc=95, capacity=60)
    result = m.build_ev_charge_forecast(NOW, app, state, tariff, grid_voltage=230)
    assert result.planned_ac_kwh == pytest.approx(3 / .9)
    assert result.unmet_ac_kwh == pytest.approx(0)
    assert (result.runs[-1].end-result.runs[0].start).total_seconds() == pytest.approx(3/.9/11.04*3600, abs=1e-5)


@pytest.mark.parametrize("soc", [None, float("nan"), -1, 101])
def test_unknown_or_invalid_soc_never_means_empty_or_completed(soc):
    m, app, state, tariff = inputs(soc=soc)
    result = m.build_ev_charge_forecast(NOW, app, state, tariff)
    assert result.status == "soc_unavailable"
    assert result.remaining_ac_kwh is None and result.runs == ()


@pytest.mark.parametrize("connected,status", [(False,"disconnected"),(None,"connection_unknown")])
def test_connection_is_not_invented(connected, status):
    m, app, state, tariff = inputs()
    result = m.build_ev_charge_forecast(NOW, app, replace(state,ev_connected=connected), tariff)
    assert result.status == status and result.runs == ()


def test_actual_zero_soc_is_valid_and_reached_target_is_complete():
    m, app, state, tariff = inputs(soc=0)
    assert m.build_ev_charge_forecast(NOW,app,state,tariff).remaining_ac_kwh == pytest.approx(65.4/.9)
    done = m.build_ev_charge_forecast(NOW,app,replace(state,ev_soc=100),tariff)
    assert done.status == "target_reached" and done.remaining_ac_kwh == 0 and not done.runs


def test_known_soc_does_not_expire_merely_because_power_meter_is_stale():
    m, app, state, tariff = inputs()
    result = m.build_ev_charge_forecast(NOW,app,replace(state,current_power_available=False),tariff)
    assert result.status == "available" and result.planned_ac_kwh > 0
    stale = m.build_ev_charge_forecast(NOW,app,replace(state,ev_soc_observed_at=NOW-timedelta(hours=25)),tariff)
    assert stale.status == "soc_stale" and stale.remaining_ac_kwh is None


def test_chronological_funding_matches_runtime_including_feed_in_opportunity():
    m, app, state, tariff = inputs([.16,.15,.3,.04],soc=99,capacity=60)
    result = m.build_ev_charge_forecast(NOW,app,state,tariff)
    assert result.runs[0].start == NOW  # actual controller does not wait for cheaper .15
    app = replace(app,cheap_price_threshold=0)
    result = m.build_ev_charge_forecast(NOW,app,state,tariff)
    assert result.runs[0].start == NOW+timedelta(minutes=45)
    assert result.runs[0].power_w == 6*230*3  # cheap target only at cheap threshold


def test_time_and_daily_limit_are_not_ev_energy_resets():
    now = datetime(2030,6,1,23,tzinfo=timezone.utc)
    m, app, state, tariff = inputs([.16]*12, soc=80,capacity=10,now=now)
    result = m.build_ev_charge_forecast(now,replace(app,start_after=time(0),end_before=time(1)),state,tariff)
    assert result.runs[0].start == now+timedelta(hours=1)
    assert result.planned_ac_kwh == pytest.approx(2/.9)


def test_horizon_can_include_tomorrow_evening_beyond_24_hours():
    m, app, state, tariff = inputs([.3]*104+[.16]*12)
    result = m.build_ev_charge_forecast(NOW,app,state,tariff)
    assert result.runs[0].start == NOW+timedelta(hours=26)


def test_extended_ev_horizon_does_not_extend_ordinary_consumer_policy():
    from custom_components.pv_excess_control.models import ForecastData, HourlyForecast
    from custom_components.pv_excess_control.planner import Planner
    m,app,state,tariff=inputs([.3]*104+[.16]*12)
    ev=m.build_ev_charge_forecast(NOW,app,state,tariff)
    other=_make_appliance(id='other',nominal_power=2000,allow_grid_supplement=True,cheap_price_threshold=.17)
    pv=ForecastData(0,[HourlyForecast(w.start,w.end,0,0) for w in tariff.windows],0)
    plan=Planner().create_plan(forecast=pv,tariff=tariff,appliances=[app,other],
        appliance_states={app.id:state,'other':_make_state('other')},battery_config=None,
        current_soc=None,export_limit=None,now=NOW,ev_forecasts={app.id:ev})
    assert any(e.appliance_id==app.id for e in plan.entries)
    assert not any(e.appliance_id=='other' for e in plan.entries)


def test_grid_cap_and_current_steps_limit_funded_power():
    m, app, state, tariff = inputs()
    result = m.build_ev_charge_forecast(NOW,replace(app,max_grid_power=5000,current_step=1),state,tariff)
    assert result.runs[0].power_w == 7*690
    assert not m.build_ev_charge_forecast(NOW,replace(app,max_grid_power=1000),state,tariff).runs


def test_missing_capacity_and_invalid_efficiency_do_not_assume_defaults():
    m, app, state, tariff = inputs()
    assert m.build_ev_charge_forecast(NOW,app,replace(state,ev_capacity_kwh=None),tariff).status == "capacity_unavailable"
    assert m.build_ev_charge_forecast(NOW,replace(app,ev_charge_efficiency=0),state,tariff).status == "invalid_configuration"


def test_tariff_gap_does_not_imply_free_or_ineligible_time():
    m, app, state, tariff = inputs()
    tariff.windows.pop(5)
    assert m.build_ev_charge_forecast(NOW,app,state,tariff).status == "tariff_gap_or_overlap"


def test_current_measured_power_does_not_survive_future_operating_gap():
    m,app,state,tariff=inputs([.16]*16)
    tariff.windows=[TariffWindow(NOW,NOW+timedelta(hours=4),.16,True)]
    app=replace(app,start_after=time(11))
    state=replace(state,is_on=True,current_power=1000,current_power_observed_at=NOW)
    result=m.build_ev_charge_forecast(NOW,app,state,tariff)
    assert result.runs[0].start==NOW+timedelta(hours=1)
    assert result.runs[0].power_w==11040


@pytest.mark.parametrize('minutes',[None,0,5])
def test_external_runtime_source_is_explicitly_unsupported_not_ignored(minutes):
    m,app,state,tariff=inputs()
    app=replace(app,remaining_runtime_entity='sensor.remaining')
    result=m.build_ev_charge_forecast(NOW,app,replace(state,remaining_runtime_minutes=minutes),tariff)
    assert result.status=='runtime_constraint_unavailable' and not result.runs
    assert result.remaining_ac_kwh is None


def test_tomorrow_ev_survives_today_exhausted_runtime_limit():
    from custom_components.pv_excess_control.models import ForecastData, HourlyForecast
    from custom_components.pv_excess_control.planner import Planner
    m,app,state,tariff=inputs([.3]*64+[.16]*12)
    app=replace(app,max_daily_runtime=timedelta(hours=1))
    state=replace(state,runtime_today=timedelta(hours=1))
    ev=m.build_ev_charge_forecast(NOW,app,state,tariff)
    assert ev.runs and ev.runs[0].start.date()>NOW.date()
    pv=ForecastData(0,[HourlyForecast(w.start,w.end,0,0) for w in tariff.windows],0)
    plan=Planner().create_plan(forecast=pv,tariff=tariff,appliances=[app],appliance_states={app.id:state},
         battery_config=None,current_soc=None,export_limit=None,now=NOW,ev_forecasts={app.id:ev})
    assert [(e.window.start,e.window.end) for e in plan.entries if e.appliance_id==app.id] == [(r.start,r.end) for r in ev.runs]



def test_repeated_hour_operating_gate_applies_in_both_folds():
    from datetime import datetime,time,timezone
    from zoneinfo import ZoneInfo
    from custom_components.pv_excess_control.models import TariffWindow
    local=ZoneInfo('Europe/Berlin')
    now=datetime(2026,10,25,1,tzinfo=local)
    m,app,state,tariff=inputs([.16],now=now)
    app=replace(app,start_after=time(2,30),end_before=time(3))
    start=now.astimezone(timezone.utc)
    tariff=replace(tariff,windows=[TariffWindow(start,start+timedelta(hours=5),.16,True)])
    model=m.build_ev_charge_forecast(now,app,state,tariff)
    assert sum((r.end-r.start).total_seconds() for r in model.runs)==3600
    for run in model.runs:
        instant=run.start
        while instant<run.end:
            assert time(2,30)<=instant.astimezone(local).time()<time(3)
            instant+=timedelta(minutes=5)
