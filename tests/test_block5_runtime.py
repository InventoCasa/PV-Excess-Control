"""Live remaining runtime and bounded contiguous-cycle regression tests."""

from dataclasses import replace
from datetime import timedelta
import pytest
from custom_components.pv_excess_control.models import Action, runtime_demand_seconds
from tests.test_optimizer import _make_appliance, _make_state
from tests.test_block4_controls import run


def appliance(**kwargs):
    return replace(
        _make_appliance(), remaining_runtime_entity="sensor.remaining", **kwargs
    )


def test_live_remaining_is_not_daily_total_and_is_capped():
    app = appliance(max_daily_runtime=timedelta(minutes=120))
    state = replace(
        _make_state(runtime_today=timedelta(minutes=90)), remaining_runtime_minutes=45
    )
    assert runtime_demand_seconds(app, state) == 1800
    assert runtime_demand_seconds(replace(app, max_daily_runtime=None), state) == 2700


@pytest.mark.parametrize("remaining", [None, 0])
def test_unknown_and_complete_demand_cannot_start(remaining):
    app = appliance()
    state = replace(_make_state(), remaining_runtime_minutes=remaining)
    assert run([app], [state])[0].action == Action.IDLE


def test_completed_demand_stops_except_on_only():
    state = replace(
        _make_state(is_on=True, current_power=1000), remaining_runtime_minutes=0
    )
    assert run([appliance()], [state])[0].action == Action.OFF
    assert run([appliance(on_only=True)], [state])[0].action == Action.ON


def test_contiguous_run_survives_solar_drop_but_daily_max_stops_it():
    app = appliance(
        require_contiguous_runtime=True, max_daily_runtime=timedelta(hours=2)
    )
    state = replace(
        _make_state(is_on=True, current_power=1000), remaining_runtime_minutes=45
    )
    assert run([app], [state], excess=-1000)[0].action == Action.ON
    assert (
        run([app], [replace(state, runtime_today=timedelta(hours=2))], excess=-1000)[
            0
        ].action
        == Action.OFF
    )


def test_noncontiguous_live_demand_does_not_protect_against_solar_loss():
    state = replace(
        _make_state(is_on=True, current_power=1000), remaining_runtime_minutes=45
    )
    assert run([appliance()], [state], excess=-1000)[0].action == Action.OFF


from unittest.mock import MagicMock, AsyncMock
from tests.test_init import _make_coordinator, _make_config_entry, MockState
from custom_components.pv_excess_control.number import (
    ApplianceMinDailyRuntimeNumber,
    ApplianceMaxDailyRuntimeNumber,
)
from homeassistant.exceptions import HomeAssistantError


def coordinator(remaining="45", **options):
    sub = MagicMock()
    sub.data = {
        "appliance_entity": "switch.load",
        "remaining_runtime_entity": "sensor.remaining",
        **options,
    }
    return _make_coordinator(
        entry=_make_config_entry(subentries={"load": sub}),
        states={
            "switch.load": MockState("on"),
            "sensor.remaining": MockState(remaining),
        },
    )


def test_coordinator_reads_live_minutes_and_unknown():
    coord = coordinator()
    cfg = coord._get_appliance_configs()
    assert cfg[0].remaining_runtime_entity == "sensor.remaining"
    assert coord._get_appliance_states(cfg)["load"].remaining_runtime_minutes == 45
    coord.hass.states._states["sensor.remaining"].state = "unavailable"
    assert coord._get_appliance_states(cfg)["load"].remaining_runtime_minutes is None


async def test_runtime_number_rejects_conflicting_source_and_unbounded_continuity():
    coord = coordinator(require_contiguous_runtime=True, max_daily_runtime=120)
    for entity, value in [
        (ApplianceMinDailyRuntimeNumber(coord, "load", "Load"), 30),
        (ApplianceMaxDailyRuntimeNumber(coord, "load", "Load"), 0),
    ]:
        entity.hass = coord.hass
        with pytest.raises(HomeAssistantError):
            await entity.async_set_native_value(value)


async def test_missing_forecast_clears_stale_plan_and_recovers():
    coord = _make_coordinator(
        entry=_make_config_entry(
            data={
                "forecast_provider": "generic",
                "forecast_sensor": "sensor.forecast",
                "additional_forecast_sensors": ["sensor.second"],
            }
        ),
        states={"sensor.forecast": MockState("4")},
    )
    from custom_components.pv_excess_control.forecast import AggregatingForecastProvider

    coord._forecast_entities = ["sensor.forecast", "sensor.second"]
    coord._forecast_tomorrow_entities = []
    coord._forecast_provider = AggregatingForecastProvider(
        "generic", coord._forecast_entities
    )
    coord.current_plan = MagicMock()
    await coord._run_planner()
    assert coord.current_plan is None
    assert coord._forecast_status == "unavailable"
    coord.hass.states._states["sensor.second"] = MockState("3")
    await coord._run_planner()
    assert coord.current_plan is not None
    assert coord._forecast_status == "available"


from datetime import datetime, timezone
from custom_components.pv_excess_control.models import (
    ForecastData,
    HourlyForecast,
    TariffInfo,
)
from custom_components.pv_excess_control.planner import Planner


def make_plan(app, state, watts=(2000, 500, 2000, 2000)):
    now = datetime(2026, 9, 8, 10, tzinfo=timezone.utc)
    forecast = ForecastData(
        10,
        [
            HourlyForecast(
                now + timedelta(hours=i), now + timedelta(hours=i + 1), w / 1000, w
            )
            for i, w in enumerate(watts)
        ],
        0,
    )
    return Planner().create_plan(
        forecast=forecast,
        tariff=TariffInfo(0.3, 0.05, 0, 0),
        appliances=[app],
        appliance_states={app.id: state},
        battery_config=None,
        current_soc=None,
        export_limit=1000,
        now=now,
    )


def duration(plan):
    return sum((e.window.end - e.window.start).total_seconds() for e in plan.entries)


def test_planner_uses_remaining_minutes_once_and_no_weather_or_export_extras():
    app = appliance(max_daily_runtime=timedelta(hours=3))
    state = replace(
        _make_state(runtime_today=timedelta(hours=1)), remaining_runtime_minutes=45
    )
    assert duration(make_plan(app, state)) == 2700


def test_fixed_daily_requirement_only_plans_unfulfilled_part():
    app = _make_appliance(
        min_daily_runtime=timedelta(hours=2), max_daily_runtime=timedelta(hours=2)
    )
    assert (
        duration(make_plan(app, _make_state(runtime_today=timedelta(minutes=90))))
        == 1800
    )


def test_contiguous_planner_chooses_adjacent_available_run():
    app = appliance(
        require_contiguous_runtime=True, max_daily_runtime=timedelta(hours=3)
    )
    state = replace(_make_state(), remaining_runtime_minutes=120)
    entries = make_plan(app, state).entries
    assert duration(make_plan(app, state)) == 7200
    assert min(e.window.start.hour for e in entries) == 12
    entries = sorted(entries, key=lambda e: e.window.start)
    assert all(a.window.end == b.window.start for a, b in zip(entries, entries[1:]))


@pytest.mark.parametrize("remaining", [0, None])
def test_planner_does_not_schedule_completed_or_unknown_demand(remaining):
    assert (
        make_plan(
            appliance(), replace(_make_state(), remaining_runtime_minutes=remaining)
        ).entries
        == []
    )


from datetime import time
from unittest.mock import patch


@pytest.mark.parametrize("dynamic", [False, True])
def test_deadline_uses_external_remaining_even_after_long_runtime(dynamic):
    app = appliance(
        schedule_deadline=time(12, 30),
        dynamic_current=dynamic,
        current_entity="number.amps" if dynamic else None,
        min_current=6,
        max_current=16,
        start_delay=3600,
    )
    state = replace(
        _make_state(runtime_today=timedelta(hours=3)), remaining_runtime_minutes=45
    )
    with patch("custom_components.pv_excess_control.optimizer.datetime") as dt:
        dt.now.return_value = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
        dt.combine.side_effect = datetime.combine
        # Allocation's legacy local datetime import must see the same clock.
        with patch("datetime.datetime", dt):
            decision = run([app], [state], 0)[0]
    assert decision.action == (Action.SET_CURRENT if dynamic else Action.ON)
    assert decision.bypasses_cooldown


@pytest.mark.parametrize("remaining", [None, 45])
def test_contiguous_unknown_is_bounded_and_does_not_bypass_gate(remaining):
    app = appliance(
        require_contiguous_runtime=True,
        max_daily_runtime=timedelta(hours=2),
        enable_condition_entity="binary_sensor.ready",
        enable_condition_mode="while_running",
    )
    state = replace(
        _make_state(is_on=True, current_power=1000), remaining_runtime_minutes=remaining
    )
    assert run([app], [state], -1000)[0].action == Action.ON
    assert (
        run([app], [replace(state, enable_condition=False)], -1000)[0].action
        == Action.OFF
    )


def test_contiguous_low_priority_run_is_not_preempted():
    app = appliance(
        priority=100,
        require_contiguous_runtime=True,
        max_daily_runtime=timedelta(hours=2),
    )
    higher = _make_appliance(id="higher", priority=1, nominal_power=1000)
    state = replace(
        _make_state(is_on=True, current_power=1000), remaining_runtime_minutes=45
    )
    decisions = {
        d.appliance_id: d
        for d in run([higher, app], [_make_state(id="higher"), state], 0)
    }
    assert decisions[app.id].action == Action.ON
    assert decisions[higher.id].action == Action.IDLE


def test_planner_clips_past_and_partial_slots_before_allocating():
    now = datetime(2026, 9, 8, 12, 30, tzinfo=timezone.utc)
    forecast = ForecastData(
        6,
        [
            HourlyForecast(
                now.replace(hour=10, minute=0), now.replace(hour=14, minute=0), 8, 2000
            )
        ],
    )
    app = appliance(max_daily_runtime=timedelta(hours=3))
    plan = Planner().create_plan(
        forecast=forecast,
        tariff=TariffInfo(0.3, 0.05, 0, 0),
        appliances=[app],
        appliance_states={app.id: replace(_make_state(), remaining_runtime_minutes=45)},
        battery_config=None,
        current_soc=None,
        export_limit=None,
        now=now,
    )
    assert duration(plan) == 2700
    assert plan.entries[0].window.start == now
    assert plan.entries[0].window.end == now + timedelta(minutes=45)


def test_planner_partial_window_budget_is_available_to_next_consumer():
    now = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
    forecast = ForecastData(
        1.5, [HourlyForecast(now, now + timedelta(hours=1), 1.5, 1500)]
    )
    first = appliance(max_daily_runtime=timedelta(hours=1))
    second = replace(
        _make_appliance(id="second", priority=2, nominal_power=500),
        remaining_runtime_entity="sensor.second",
    )
    plan = Planner().create_plan(
        forecast=forecast,
        tariff=TariffInfo(0.3, 0.05, 0, 0),
        appliances=[first, second],
        appliance_states={
            first.id: replace(_make_state(), remaining_runtime_minutes=30),
            second.id: replace(_make_state(id="second"), remaining_runtime_minutes=60),
        },
        battery_config=None,
        current_soc=None,
        export_limit=None,
        now=now,
    )
    assert {e.appliance_id for e in plan.entries} == {first.id, second.id}
    second_entries = [e for e in plan.entries if e.appliance_id == second.id]
    assert (
        sum(
            (e.window.end - e.window.start).total_seconds()
            for e in second_entries
            if e.reason.value == "excess_available"
        )
        == 1800
    )
    assert all(
        e.window.start >= now + timedelta(minutes=30)
        for e in second_entries
        if e.reason.value == "excess_available"
    )


def test_planner_state_read_does_not_increment_daily_runtime():
    coord = coordinator()
    cfg = coord._get_appliance_configs()
    coord._get_appliance_states(cfg)
    coord._get_appliance_states(cfg)
    before = coord.appliance_states["load"].runtime_today
    coord._get_appliance_states(cfg, update_runtime=False)
    coord._get_appliance_states(cfg, update_runtime=False)
    assert coord.appliance_states["load"].runtime_today == before


def test_contiguous_bound_counts_on_time_when_power_threshold_says_complete():
    coord = coordinator(
        require_contiguous_runtime=True,
        max_daily_runtime=1,
        completion_power_threshold=100,
        actual_power_entity="sensor.draw",
    )
    coord.hass.states._states["sensor.draw"] = MockState("0")
    cfg = coord._get_appliance_configs()
    coord._get_appliance_states(cfg)
    states = coord._get_appliance_states(cfg)
    assert states["load"].runtime_today == coord.update_interval


@pytest.mark.parametrize(
    "step", ["async_step_constraints", "async_step_reconfigure_constraints"]
)
@pytest.mark.parametrize(
    "inputs,key,error",
    [
        (
            {"remaining_runtime_entity": "sensor.remaining", "min_daily_runtime": 10},
            "min_daily_runtime",
            "runtime_source_conflict",
        ),
        (
            {"require_contiguous_runtime": True, "max_daily_runtime": 0},
            "max_daily_runtime",
            "contiguous_requires_maximum",
        ),
        (
            {"helper_only": True, "remaining_runtime_entity": "sensor.remaining"},
            "helper_only",
            "helper_only_with_runtime",
        ),
    ],
)
async def test_runtime_flow_rejects_conflicting_settings(step, inputs, key, error):
    from tests.test_config_flow_subentry import _make_subentry_flow

    flow = _make_subentry_flow()
    flow._data = {
        "appliance_name": "Load",
        "appliance_entity": "switch.load",
        "nominal_power": 1000,
    }
    flow._get_entry = MagicMock(return_value=MagicMock(subentries={}))
    flow._get_reconfigure_subentry = MagicMock(return_value=MagicMock(data=flow._data))
    result = await getattr(flow, step)({"switch_interval": 5, **inputs})
    assert result["errors"][key] == error


def test_standard_appliance_does_not_suggest_invalid_cheap_current():
    from custom_components.pv_excess_control.config_flow import (
        _appliance_current_schema,
    )

    for key in _appliance_current_schema({"dynamic_current": False}).schema:
        if str(key) == "cheap_grid_target_current":
            assert key.description["suggested_value"] is None


async def test_forecast_pause_yields_cap_once_and_resumes_on_recovery():
    from tests.test_coordinator_battery_charge import (
        _coordinator_with_dyn_charge_enabled,
    )
    from custom_components.pv_excess_control.coordinator import PvExcessCoordinator

    coord = _coordinator_with_dyn_charge_enabled()
    coord._forecast_status = "unavailable"
    coord._dyn_charge_loop_active = True
    coord._write_battery_max_charge = AsyncMock(return_value=True)
    coord.current_plan = None
    dispatch = PvExcessCoordinator._dispatch_dynamic_battery_charge.__get__(coord)
    await dispatch(None)
    await dispatch(None)
    coord._write_battery_max_charge.assert_awaited_once_with(5000)
    coord._forecast_status = "available"
    await dispatch(None)
    assert coord._dyn_charge_loop_active
    assert coord._write_battery_max_charge.await_count == 2


@pytest.mark.parametrize("remaining", [None, 0])
@pytest.mark.parametrize("cheap", [False, True])
def test_dependency_runtime_gate_cannot_be_bypassed_by_injection(remaining, cheap):
    from tests.test_optimizer import _make_tariff

    parent = _make_appliance(
        id="parent", priority=1, requires_appliance="dep", allow_grid_supplement=cheap
    )
    dep = appliance(id="dep", priority=2)
    decisions = {
        d.appliance_id: d
        for d in run(
            [parent, dep],
            [
                _make_state(id="parent"),
                replace(_make_state(id="dep"), remaining_runtime_minutes=remaining),
            ],
            0 if cheap else 10000,
            tariff=_make_tariff(current_price=-0.1 if cheap else 0.3),
        )
    }
    assert decisions["parent"].action == Action.IDLE
    assert decisions["dep"].action == Action.IDLE


def test_contiguous_cheap_grid_keeps_import_credit_and_battery_protection():
    from tests.test_optimizer import _make_tariff, _make_power, _empty_plan
    from custom_components.pv_excess_control.optimizer import Optimizer

    app = appliance(
        require_contiguous_runtime=True,
        max_daily_runtime=timedelta(hours=2),
        allow_grid_supplement=True,
    )
    state = replace(
        _make_state(is_on=True, current_power=1000), remaining_runtime_minutes=45
    )
    power = _make_power(-1000)
    result = Optimizer(min_good_samples=1).optimize(
        power, [app], [state], _empty_plan(), [power], _make_tariff(current_price=-0.1)
    )
    assert result.decisions[0].uses_grid_supplement
    assert result.decisions[0].grid_supplement_watts == 1000
    assert result.battery_discharge_action.should_limit


async def test_forecast_options_can_remove_optional_arrays():
    from tests.test_options_flow import _make_options_flow

    flow = _make_options_flow(
        {
            "forecast_provider": "generic",
            "forecast_sensor": "sensor.one",
            "additional_forecast_sensors": ["sensor.two"],
            "additional_forecast_tomorrow_sensors": ["sensor.tomorrow"],
        }
    )
    flow.async_step_settings = AsyncMock(return_value={})
    await flow.async_step_forecast(
        {"forecast_provider": "generic", "forecast_sensor": "sensor.one"}
    )
    assert "additional_forecast_sensors" not in flow.data
    assert "additional_forecast_tomorrow_sensors" not in flow.data


async def test_source_changes_replan_at_controller_cadence_with_long_planner_interval():
    coord = _make_coordinator()
    coord._forecast_entities = ["sensor.forecast"]
    coord._forecast_tomorrow_entities = []
    coord._forecast_status = "available"
    coord._planner_interval = 900
    coord.hass.states._states["sensor.forecast"] = MockState("3", {"raw_today": []})
    coord._run_planner = AsyncMock()
    await coord._async_update_data()
    coord._run_planner.reset_mock()
    coord.hass.states._states["sensor.forecast"].state = "unavailable"
    await coord._async_update_data()
    coord._run_planner.assert_awaited_once()
    coord._run_planner.reset_mock()
    coord.hass.states._states["sensor.forecast"] = MockState(
        "3", {"raw_today": [{"pv_estimate": 2}]}
    )
    await coord._async_update_data()
    coord._run_planner.assert_awaited_once()


async def test_live_remaining_demand_replans_without_double_runtime_counting():
    coord = coordinator()
    coord._planner_interval = 900
    coord._run_planner = AsyncMock()
    await coord._async_update_data()
    before = coord.appliance_states["load"].runtime_today
    coord.hass.states._states["sensor.remaining"].state = "40"
    await coord._async_update_data()
    coord._run_planner.assert_awaited_once()
    assert (
        coord.appliance_states["load"].runtime_today - before == coord.update_interval
    )


def test_runtime_planner_preserves_elapsed_duration_across_dst_fold():
    from zoneinfo import ZoneInfo

    berlin = ZoneInfo("Europe/Berlin")
    start = datetime(2026, 10, 25, 1, 30, tzinfo=berlin)
    end = datetime(2026, 10, 25, 3, 30, tzinfo=berlin)
    forecast = ForecastData(6, [HourlyForecast(start, end, 6, 2000)])
    app = appliance(
        require_contiguous_runtime=True, max_daily_runtime=timedelta(hours=3)
    )
    state = replace(_make_state(), remaining_runtime_minutes=180)
    plan = Planner(timezone_str="Europe/Berlin").create_plan(
        forecast=forecast,
        tariff=TariffInfo(0.3, 0.05, 0, 0),
        appliances=[app],
        appliance_states={app.id: state},
        battery_config=None,
        current_soc=None,
        export_limit=None,
        now=start,
    )
    assert duration(plan) == 10800
    assert plan.entries[-1].window.end == end.astimezone(timezone.utc)


@pytest.mark.parametrize("parent_on", [False, True])
def test_completed_running_dependency_stops_parent_in_same_cycle(parent_on):
    parent = _make_appliance(id="parent", priority=1, requires_appliance="dep")
    dep = appliance(id="dep", priority=2)
    states = [
        _make_state(id="parent", is_on=parent_on),
        replace(
            _make_state(id="dep", is_on=True, current_power=1000),
            remaining_runtime_minutes=0,
        ),
    ]
    decisions = {d.appliance_id: d for d in run([parent, dep], states, 10000)}
    assert decisions["parent"].action == (Action.OFF if parent_on else Action.IDLE)
    assert decisions["dep"].action == Action.OFF


@pytest.mark.parametrize("override,on_only", [(True, False), (False, True)])
def test_completed_dependency_keeps_explicit_override_or_on_only_supply(
    override, on_only
):
    parent = _make_appliance(id="parent", priority=1, requires_appliance="dep")
    dep = appliance(id="dep", priority=2, override_active=override, on_only=on_only)
    states = [
        _make_state(id="parent"),
        replace(
            _make_state(id="dep", is_on=True, current_power=1000),
            remaining_runtime_minutes=0,
        ),
    ]
    assert all(d.action == Action.ON for d in run([parent, dep], states, 10000))


def test_runtime_planner_uses_feasible_stepped_current_in_partial_solar():
    now = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
    forecast = ForecastData(2, [HourlyForecast(now, now + timedelta(hours=1), 2, 2000)])
    ev = _make_appliance(
        dynamic_current=True,
        current_entity="number.amps",
        min_current=6,
        max_current=16,
        current_step=0.5,
        nominal_power=3680,
    )
    plan = Planner().create_plan(
        forecast=forecast,
        tariff=TariffInfo(0.3, 0.05, 0, 0),
        appliances=[ev],
        appliance_states={ev.id: _make_state()},
        battery_config=None,
        current_soc=None,
        export_limit=None,
        now=now,
        base_load_watts=0,
    )
    assert len(plan.entries) == 1
    assert plan.entries[0].target_current == 8.5
    assert plan.entries[0].reason.value == "excess_available"


def test_dynamic_partial_plan_reserves_actual_current_power_for_next_load():
    now = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
    forecast = ForecastData(2, [HourlyForecast(now, now + timedelta(hours=1), 2, 2000)])
    ev = replace(
        _make_appliance(
            dynamic_current=True,
            current_entity="number.amps",
            min_current=6,
            max_current=16,
            current_step=1,
            nominal_power=3680,
        ),
        remaining_runtime_entity="sensor.minutes",
    )
    other = _make_appliance(id="other", priority=2, nominal_power=160)
    plan = Planner().create_plan(
        forecast=forecast,
        tariff=TariffInfo(0.3, 0.05, 0, 0),
        appliances=[ev, other],
        appliance_states={
            ev.id: replace(_make_state(), remaining_runtime_minutes=30),
            other.id: _make_state(id="other"),
        },
        battery_config=None,
        current_soc=None,
        export_limit=None,
        now=now,
        base_load_watts=0,
    )
    assert plan.entries[0].target_current == 8
    assert (
        sum(
            (e.window.end - e.window.start).total_seconds()
            for e in plan.entries
            if e.appliance_id == other.id
        )
        == 3600
    )


def test_weather_extra_uses_disjoint_precise_runtime_span():
    now = datetime(2026, 9, 8, 10, tzinfo=timezone.utc)
    forecast = ForecastData(
        12, [HourlyForecast(now, now + timedelta(hours=4), 12, 3000)], 0
    )
    app = _make_appliance(min_daily_runtime=timedelta(hours=2))
    plan = Planner().create_plan(
        forecast=forecast,
        tariff=TariffInfo(0.3, 0.05, 0, 0),
        appliances=[app],
        appliance_states={app.id: _make_state()},
        battery_config=None,
        current_soc=None,
        export_limit=None,
        now=now,
        base_load_watts=0,
    )
    entries = sorted(plan.entries, key=lambda e: e.window.start)
    assert duration(plan) == 10800
    assert entries[0].window.end <= entries[1].window.start
    assert entries[1].reason.value == "weather_preplanning"
    assert (entries[1].window.end - entries[1].window.start) == timedelta(hours=1)


def test_runtime_plan_does_not_reserve_for_gate_stopping_running_load():
    now = datetime(2026, 9, 8, 10, tzinfo=timezone.utc)
    forecast = ForecastData(1, [HourlyForecast(now, now + timedelta(hours=1), 1, 1000)])
    gated = replace(
        _make_appliance(),
        enable_condition_entity="binary_sensor.ready",
        enable_condition_mode="while_running",
    )
    other = _make_appliance(id="other", priority=2)
    plan = Planner().create_plan(
        forecast=forecast,
        tariff=TariffInfo(0.3, 0.05, 0, 0),
        appliances=[gated, other],
        appliance_states={
            gated.id: replace(_make_state(is_on=True), enable_condition=False),
            other.id: _make_state(id="other"),
        },
        battery_config=None,
        current_soc=None,
        export_limit=None,
        now=now,
        base_load_watts=0,
    )
    assert [entry.appliance_id for entry in plan.entries] == [other.id]


def test_already_running_contiguous_cycle_is_planned_now_despite_better_later_sun():
    app = appliance(
        require_contiguous_runtime=True, max_daily_runtime=timedelta(hours=3)
    )
    state = replace(
        _make_state(is_on=True, current_power=1000), remaining_runtime_minutes=60
    )
    plan = make_plan(app, state, watts=(500, 2000, 2000, 2000))
    assert plan.entries[0].window.start.hour == 10
    assert duration(plan) == 3600


def test_battery_curve_credits_partial_export_span_only():
    from tests.test_planner import _make_battery_config
    from custom_components.pv_excess_control.const import BatteryStrategy

    now = datetime(2026, 9, 8, 10, tzinfo=timezone.utc)
    forecast = ForecastData(3, [HourlyForecast(now, now + timedelta(hours=1), 3, 3000)])
    app = _make_appliance(min_daily_runtime=timedelta(minutes=30))
    plan = Planner().create_plan(
        forecast=forecast,
        tariff=TariffInfo(0.3, 0.05, 0, 0),
        appliances=[app],
        appliance_states={app.id: _make_state()},
        battery_config=_make_battery_config(strategy=BatteryStrategy.APPLIANCE_FIRST),
        current_soc=30,
        export_limit=1000,
        now=now,
        base_load_watts=0,
        dynamic_battery_charge_enabled=True,
        battery_max_charge_power_w=5000,
    )
    export = [e for e in plan.entries if e.reason.value == "export_limit"]
    assert len(export) == 1 and export[0].window.start == now + timedelta(minutes=30)
    assert plan.battery_charge_curve.expected_curtailment_kwh == pytest.approx(1.5)


@pytest.mark.parametrize("dynamic", [False, True])
def test_battery_curve_export_absorption_spans_multiple_forecast_intervals(dynamic):
    from tests.test_planner import _make_battery_config
    from custom_components.pv_excess_control.const import BatteryStrategy

    now = datetime(2026, 9, 8, 10, tzinfo=timezone.utc)
    forecast = ForecastData(
        12 if dynamic else 6,
        [
            HourlyForecast(
                now + timedelta(hours=i),
                now + timedelta(hours=i + 1),
                6 if dynamic else 3,
                6000 if dynamic else 3000,
            )
            for i in range(2)
        ],
    )
    app = _make_appliance(
        min_daily_runtime=timedelta(minutes=30),
        dynamic_current=dynamic,
        current_entity="number.amps" if dynamic else None,
        min_current=6,
        max_current=10 if dynamic else 16,
        current_step=1,
        nominal_power=3680 if dynamic else 1000,
    )
    plan = Planner().create_plan(
        forecast=forecast,
        tariff=TariffInfo(0.3, 0.05, 0, 0),
        appliances=[app],
        appliance_states={app.id: _make_state()},
        battery_config=_make_battery_config(strategy=BatteryStrategy.APPLIANCE_FIRST),
        current_soc=30,
        export_limit=1000,
        now=now,
        base_load_watts=0,
        dynamic_battery_charge_enabled=True,
        battery_max_charge_power_w=5000,
    )
    # EXPORT covers 10:30–12:00. The dynamic load actually absorbs2300W,
    # not its3680W nominal: 10kWh gross minus3.45kWh absorption.
    assert plan.battery_charge_curve.expected_curtailment_kwh == pytest.approx(
        6.55 if dynamic else 2.5
    )


def test_battery_curve_excludes_past_sun_and_clips_current_interval():
    from tests.test_planner_battery_curve import TestBatteryChargeCurve

    now = datetime(2026, 5, 5, 10, 30, tzinfo=timezone.utc)
    forecast = ForecastData(
        3,
        [
            HourlyForecast(
                now.replace(hour=8, minute=0), now.replace(hour=10, minute=0), 20, 10000
            ),
            HourlyForecast(
                now.replace(minute=0), now.replace(hour=11, minute=0), 3, 3000
            ),
        ],
    )
    kwargs = TestBatteryChargeCurve()._common_kwargs()
    kwargs.update(now=now, export_soft_ceiling_w=1000, base_load_watts=0)
    curve = Planner().plan_battery_charge_curve(forecast=forecast, **kwargs)
    assert curve.expected_curtailment_kwh == pytest.approx(1)
    assert curve.setpoints[0].start == now
