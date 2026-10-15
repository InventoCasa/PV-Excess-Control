"""The published charging plan must honor actual restart eligibility."""
from datetime import timedelta

from tests.test_battery_control_economics import economic_case  # noqa: F401


def refresh_inputs(case):
    for entity, state in list(case.states.items()):
        case.reading(entity, state.state, **state.attributes)


def test_existing_restart_delay_removes_unexecutable_current_charge(economic_case):
    case = economic_case([.1, .4], [0, 0], [0, 2000])
    until = case.clock['now'] + timedelta(minutes=5)
    case.coord._battery_retry_after = until
    assert case.coord._refresh_battery_grid_plan(case.tariff, None) is None
    plan = case.coord._battery_grid_plan
    assert plan.valid and plan.grid_energy_kwh > 0
    assert plan.slots[0].action == 'self_consumption'
    assert plan.slots[0].end == until
    assert all(slot.start >= until for slot in plan.slots if slot.action == 'charge')


def test_new_deadline_invalidates_cached_plan_and_expiry_allows_fresh_start(economic_case):
    case = economic_case([.1, .4], [0, 0], [0, 2000])
    assert case.coord._refresh_battery_grid_plan(case.tariff, None) is None
    old = case.coord._battery_grid_plan
    assert old.slots[0].action == 'charge'
    case.coord._battery_retry_after = case.clock['now'] + timedelta(minutes=7)
    assert case.coord._refresh_battery_grid_plan(case.tariff, None) is None
    delayed = case.coord._battery_grid_plan
    assert delayed is not old
    assert all(s.start >= case.coord._battery_retry_after for s in delayed.slots if s.action == 'charge')
    case.clock['now'] = case.coord._battery_retry_after
    refresh_inputs(case)
    assert case.coord._refresh_battery_grid_plan(case.tariff, None) is None
    assert case.coord._battery_grid_plan.slots[0].action == 'charge'


def test_delay_setting_change_invalidates_cache_but_confirmed_start_does_not(economic_case):
    case = economic_case([.1, .4], [0, 0], [0, 2000], grid_charge_engage_min_duration_minutes=5)
    assert case.coord._refresh_battery_grid_plan(case.tariff, None) is None
    original = case.coord._battery_grid_plan
    case.coord._grid_charge_engaged = True
    assert case.coord._refresh_battery_grid_plan(case.tariff, None) is None
    assert case.coord._battery_grid_plan is original
    case.coord.config_entry.data['grid_charge_engage_min_duration_minutes'] = 9
    assert case.coord._refresh_battery_grid_plan(case.tariff, None) is None
    assert case.coord._battery_grid_plan is not original


def test_invalid_restart_delay_blocks_plan_instead_of_silently_ignoring_it(economic_case):
    case = economic_case([.1, .4], [0, 0], [0, 2000], grid_charge_engage_min_duration_minutes=-1)
    assert case.coord._refresh_battery_grid_plan(case.tariff, None) is not None
    assert not case.coord._battery_grid_plan.valid
