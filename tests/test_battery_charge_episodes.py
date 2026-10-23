"""A new charge episode must have useful execution time after preparation."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.pv_excess_control import battery_planner as bp
from custom_components.pv_excess_control.models import HourlyForecast, TariffWindow

NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
BASE = bp.BatteryPlanningConfig(
    capacity_kwh=8.64, reserve_soc=0, grid_target_soc=100,
    max_charge_power_w=5000, max_pv_charge_power_w=6570,
    max_discharge_power_w=5000, roundtrip_efficiency=.85,
    wear_cost_per_kwh=.03, charge_price_limit=.3, hold_supported=True,
    forced_charge_min_power_w=0, forced_charge_max_power_w=6570,
    forced_charge_power_step_w=10,
)


def config(**changes):
    return replace(BASE, grid_charge_minimum_minutes=5,
                   grid_charge_startup_seconds=45, **changes)


def build(periods, *, cfg=None, soc=0, floor_seconds=None, pv_w=0, load_w=1000):
    tariffs, forecast = [], []
    start = NOW
    for minutes, price in periods:
        end = start + timedelta(minutes=minutes)
        tariffs.append(TariffWindow(start, end, price, False))
        watts = pv_w if price <= .3 else 0
        forecast.append(HourlyForecast(start, end, watts * minutes / 60000, watts))
        start = end
    commitments = () if floor_seconds is None else (
        bp.BatteryLoadCommitment(NOW, NOW + timedelta(seconds=floor_seconds), 0,
                                 household_power_floor_w=2000),
    )
    return bp.build_battery_grid_plan(
        NOW, soc, tariffs, forecast, [load_w] * 24, cfg or config(),
        load_commitments=commitments,
    )


def episodes(plan):
    result = []
    for slot in plan.slots:
        if slot.action == 'charge' and (not result or slot.start >= result[-1].end):
            result.append(bp.charge_episode_at(plan, slot.start))
    return result


def assert_executable_new_episodes(plan, minimum_minutes=5, setup_seconds=45):
    assert plan.valid
    for episode in episodes(plan):
        assert episode.available_at - episode.start == timedelta(seconds=setup_seconds)
        assert episode.end - episode.available_at >= timedelta(minutes=minimum_minutes)
        assert episode.power_w > 0
        for slot in plan.slots:
            if episode.start <= slot.start < episode.end:
                assert slot.action == 'charge'
                if slot.end <= episode.available_at:
                    assert slot.grid_charge_kwh == slot.pv_charge_kwh == slot.discharge_kwh == 0
                    assert slot.battery_charge_power_w == slot.charge_power_w == 0
                else:
                    assert slot.grid_charge_kwh > 0
                    assert 0 < slot.charge_power_w <= 5000 + 1e-6
                    assert slot.battery_charge_power_w <= 6570 + 1e-6
                    assert slot.battery_charge_power_w / 10 == pytest.approx(round(slot.battery_charge_power_w / 10))


def test_pure_zero_defaults_keep_short_economic_window_compatible():
    legacy = build([(2, .1), (60, .6)], cfg=BASE)
    explicit = build([(2, .1), (60, .6)], cfg=replace(
        BASE, grid_charge_minimum_minutes=0, grid_charge_startup_seconds=0,
        grid_charge_prepared=False,
    ))
    assert legacy == explicit
    assert legacy.slots[0].action == 'charge'


@pytest.mark.parametrize('cheap_minutes', [1, 2, 5, 5.5])
def test_fresh_start_cannot_use_window_shorter_than_setup_plus_five_minutes(cheap_minutes):
    result = build([(cheap_minutes, .1), (60, .6)])
    assert result.valid
    assert result.grid_energy_kwh == 0
    assert all(slot.action != 'charge' for slot in result.slots)


def test_new_episode_has_five_minutes_of_input_after_startup():
    result = build([(6, .1), (60, .6)])
    assert_executable_new_episodes(result)
    episode = episodes(result)[0]
    assert episode.start == NOW
    assert episode.available_at == NOW + timedelta(seconds=45)
    assert episode.end == NOW + timedelta(minutes=6)
    assert result.slots[0].grid_charge_kwh <= 5000 * 5.25 / 60000
    assert episode.target_soc == result.slots[0].soc_end


def test_compatible_adjacent_prices_count_toward_one_continuous_episode():
    result = build([(3, .1), (3, .12), (60, .6)])
    assert_executable_new_episodes(result)
    first, second = result.slots[:2]
    assert first.action == second.action == 'charge'
    episode = bp.charge_episode_at(result, NOW + timedelta(minutes=3))
    assert episode.start == NOW
    assert episode.end == NOW + timedelta(minutes=6)
    assert episode.slot is second
    assert episode.target_soc == second.soc_end
    assert episode.power_w == second.battery_charge_power_w


def test_real_load_boundary_keeps_zero_energy_setup_slot_in_same_episode():
    result = build([(8, .1), (60, .6)], floor_seconds=30)
    assert_executable_new_episodes(result)
    first = result.slots[0]
    assert first.end == NOW + timedelta(seconds=30)
    assert first.action == 'charge' and first.grid_charge_kwh == 0
    episode = bp.charge_episode_at(result, NOW)
    assert episode.slot is first
    assert episode.power_slot.start == first.end
    assert episode.power_w == episode.power_slot.battery_charge_power_w > 0
    assert episode.end == NOW + timedelta(minutes=8)


def test_prepared_initial_start_skips_setup_but_keeps_five_minute_requirement():
    result = build([(5, .1), (60, .6)], cfg=config(grid_charge_prepared=True))
    assert_executable_new_episodes(result, setup_seconds=0)
    assert episodes(result)[0].available_at == NOW
    assert result.grid_energy_kwh > 0
    too_short = build([(4, .1), (60, .6)], cfg=config(grid_charge_prepared=True))
    assert too_short.grid_energy_kwh == 0


def test_prepared_flag_does_not_skip_startup_for_later_new_episode():
    result = build([(10, .6), (5, .1), (60, .6)], cfg=config(grid_charge_prepared=True))
    assert result.grid_energy_kwh == 0


def test_existing_active_charge_can_finish_with_less_than_five_minutes_left():
    result = build([(2, .1), (60, .6)], cfg=config(grid_charge_active=True))
    assert result.valid and result.grid_energy_kwh > 0
    episode = bp.charge_episode_at(result, NOW)
    assert episode.available_at == NOW
    assert episode.end == NOW + timedelta(minutes=2)


def test_existing_active_flag_does_not_exempt_a_future_new_start():
    result = build([(2, .1), (10, .6), (3, .1), (60, .6)], cfg=config(grid_charge_active=True))
    assert result.grid_energy_kwh > 0
    assert all(s.action != 'charge' for s in result.slots if s.start >= NOW + timedelta(minutes=2))


def test_active_continuation_preserves_remaining_confirmed_minimum():
    deadline = NOW + timedelta(minutes=5)
    result = build([(2, .1), (3, .12), (60, .6)], cfg=config(
        grid_charge_active=True, grid_charge_minimum_until=deadline,
    ))
    episode = bp.charge_episode_at(result, NOW)
    assert episode is not None and episode.available_at == NOW
    assert episode.end >= deadline


def test_active_commitment_may_stop_immediately_when_continuation_is_not_allowed():
    result = build([(2, .1), (3, .6), (20, .1), (60, .6)], cfg=config(
        grid_charge_active=True, grid_charge_minimum_until=NOW + timedelta(minutes=5),
        grid_restart_delay_minutes=5,
    ))
    assert result.valid
    assert result.slots[0].action != 'charge'
    assert all(s.start >= NOW + timedelta(minutes=5) for s in result.slots if s.action == 'charge')


def test_expired_active_minimum_does_not_restart_a_five_minute_requirement():
    result = build([(2, .1), (60, .6)], cfg=config(
        grid_charge_active=True, grid_charge_minimum_until=NOW - timedelta(seconds=1),
    ))
    assert bp.charge_episode_at(result, NOW).end == NOW + timedelta(minutes=2)


def test_expired_or_inactive_carried_deadline_is_ignored():
    for active in (False, True):
        plain = replace(BASE, grid_charge_active=active)
        expired = replace(plain, grid_charge_minimum_until=NOW - timedelta(seconds=1))
        assert build([(2, .1), (60, .6)], cfg=expired) == build([(2, .1), (60, .6)], cfg=plain)
    inactive_future = replace(BASE, grid_charge_minimum_until=NOW + timedelta(minutes=5))
    assert build([(2, .1), (60, .6)], cfg=inactive_future) == build([(2, .1), (60, .6)], cfg=BASE)


@pytest.mark.parametrize('round_up', [False, True])
def test_rolling_replans_keep_confirmed_episode_with_quantized_soc(round_up):
    """Normal clock/SoC progress must not recreate short remaining pulses."""
    tariffs = [TariffWindow(NOW, NOW + timedelta(minutes=8), .1, False),
               TariffWindow(NOW + timedelta(minutes=8), NOW + timedelta(minutes=68), .6, False)]
    forecast = [HourlyForecast(row.start, row.end, 0., 0.) for row in tariffs]
    commitments = (bp.BatteryLoadCommitment(
        NOW, NOW + timedelta(seconds=100), 0., household_power_floor_w=5000),)
    cfg = replace(config(), grid_charge_startup_seconds=30, grid_charge_enable_seconds=15)
    first = bp.build_battery_grid_plan(NOW, 0, tariffs, forecast, [4000.] * 24, cfg,
                                      load_commitments=commitments)
    assert bp.charge_episode_at(first, NOW) is not None
    confirmed = NOW + timedelta(seconds=45)
    deadline = confirmed + timedelta(minutes=5)
    cfg = replace(cfg, grid_charge_active=True, grid_charge_minimum_until=deadline)
    stored = 0.
    for seconds in range(0, 301, 30):
        now = confirmed + timedelta(seconds=seconds)
        # The physical meter reports roughly 1.1-point increments. Exercise
        # both adjacent readings instead of feeding exact model energy back.
        fraction = stored / cfg.capacity_kwh * 100 / 1.1
        soc = (int(fraction) + int(round_up and fraction % 1 > 0)) * 1.1
        plan = bp.build_battery_grid_plan(now, soc, tariffs, forecast, [4000.] * 24, cfg,
                                         load_commitments=commitments)
        episode = bp.charge_episode_at(plan, now)
        assert episode is not None
        assert episode.available_at == now
        assert episode.end >= deadline
        assert episode.end == NOW + timedelta(minutes=8)
        assert cfg.grid_charge_minimum_until == deadline
        stored += episode.power_w * 30 / 3_600_000 * .85 ** .5


def test_initial_restart_delay_does_not_count_as_preparation_or_charge_time():
    result = build([(6, .1), (60, .6)], cfg=config(
        grid_charge_not_before=NOW + timedelta(minutes=2), grid_restart_delay_minutes=5,
    ))
    assert result.grid_energy_kwh == 0


def test_daytime_start_explicitly_accounts_for_only_unavoidable_setup_solar_loss():
    result = build([(8, .1), (60, .6)], pv_w=2000)
    assert_executable_new_episodes(result)
    first = result.slots[0]
    assert first.startup_pv_loss_kwh == pytest.approx(1000 * 45 / 3_600_000)
    assert first.pv_charge_kwh == pytest.approx(1000 * (480 - 45) / 3_600_000)
    supplied_later = (first.pv_charge_kwh + first.grid_charge_kwh) * .85
    actual_cost = first.grid_charge_kwh * .1 + (1 - supplied_later) * .6 + supplied_later * .03
    native_solar_supply = 1000 * 480 / 3_600_000 * .85
    native_cost = (1 - native_solar_supply) * .6 + native_solar_supply * .03
    assert result.estimated_savings == pytest.approx(native_cost - actual_cost)
    active = build([(8, .1), (60, .6)], cfg=config(grid_charge_active=True), pv_w=2000)
    assert active.grid_energy_kwh > 0
    assert active.slots[0].startup_pv_loss_kwh == 0
    assert active.slots[0].pv_charge_kwh == pytest.approx(1000 * 480 / 3_600_000)


def test_negative_price_cannot_replace_unused_solar_by_manufacturing_setup_loss():
    result = build([(8, -.1), (60, .6)], pv_w=2000, load_w=0)
    assert result.valid and result.grid_energy_kwh == 0
    assert all(s.action != 'charge' for s in result.slots)


def test_startup_loss_allowance_never_permits_later_capacity_crowding():
    tariffs = [TariffWindow(NOW + timedelta(hours=i), NOW + timedelta(hours=i + 1), p, False)
               for i, p in enumerate([-.1, .2, .6])]
    solar = [HourlyForecast(w.start, w.end, pv, pv * 1000) for w, pv in zip(tariffs, [0, 2, 0])]
    profile = [0., 0., 500.] + [0.] * 21
    result = bp.build_battery_grid_plan(NOW, 0, tariffs, solar, profile,
                                      replace(config(), capacity_kwh=1, max_pv_charge_power_w=2000))
    assert result.valid and result.grid_energy_kwh == 0
    assert result.slots[1].pv_charge_kwh == pytest.approx(1 / .85 ** .5)


def test_setup_solar_consumed_before_later_refill_is_not_unused_terminal_inventory():
    tariffs = [TariffWindow(NOW + timedelta(hours=i), NOW + timedelta(hours=i + 1), p, False)
               for i, p in enumerate([.1, .6, .2])]
    solar = [HourlyForecast(w.start, w.end, pv, pv * 1000) for w, pv in zip(tariffs, [.2, 0, 1])]
    cfg = replace(config(), capacity_kwh=2, grid_charge_startup_seconds=30)
    result = bp.build_battery_grid_plan(NOW, 0, tariffs, solar, [0., 1000., 0.] + [0.] * 21, cfg)
    assert result.valid and result.grid_energy_kwh > 0
    assert result.estimated_savings > .35
    assert result.slots[0].startup_pv_loss_kwh == pytest.approx(.2 * 30 / 3600)
    assert result.slots[-1].soc_end == pytest.approx(.85 ** .5 / 2 * 100)


def test_enable_confirmation_reserve_is_additional_to_minimum_input_window():
    cfg = replace(config(), grid_charge_startup_seconds=30, grid_charge_enable_seconds=15)
    too_short = build([(5.5, .1), (60, .6)], cfg=cfg)
    assert too_short.grid_energy_kwh == 0
    sufficient = build([(5.75, .1), (60, .6)], cfg=cfg)
    episode = bp.charge_episode_at(sufficient, NOW)
    assert episode is not None
    assert episode.available_at == NOW + timedelta(seconds=30)
    assert episode.end - episode.available_at == timedelta(minutes=5, seconds=15)
    prepared = build([(5, .1), (60, .6)], cfg=replace(cfg, grid_charge_prepared=True))
    assert prepared.grid_energy_kwh == 0
    ready = build([(5.25, .1), (60, .6)], cfg=replace(cfg, grid_charge_prepared=True))
    assert ready.grid_energy_kwh > 0


def test_below_reserve_start_cannot_raise_unused_terminal_bound():
    result = build([(8, -.1), (60, .6)], cfg=config(reserve_soc=10), load_w=0)
    assert result.valid and result.grid_energy_kwh == 0
    assert result.slots[-1].soc_end == 0


def test_episode_helper_does_not_join_gaps_or_non_charge_slots():
    result = build([(6, .1), (60, .6)])
    assert bp.charge_episode_at(result, NOW + timedelta(minutes=10)) is None
    assert bp.charge_episode_at(result, result.slots[-1].end) is None
    assert bp.charge_episode_at(replace(result, valid=False), NOW) is None


def test_small_complete_episode_candidate_survives_when_finer_work_is_abandoned(monkeypatch):
    monkeypatch.setattr(bp, '_EPISODE_PRIMARY_TRANSITION_BUDGET', 0)
    monkeypatch.setattr(bp, '_REFINEMENT_TRANSITION_BUDGET', 0)
    monkeypatch.setattr(bp, '_QUANTITY_REFINEMENT_TRANSITION_BUDGET', 0)
    result = build([(8, .1), (60, .6)])
    assert result.grid_energy_kwh > 0
    assert_executable_new_episodes(result)


def test_small_episode_candidate_completes_dense_real_boundary_horizon(monkeypatch):
    monkeypatch.setattr(bp, '_EPISODE_PRIMARY_TRANSITION_BUDGET', 0)
    monkeypatch.setattr(bp, '_REFINEMENT_TRANSITION_BUDGET', 0)
    monkeypatch.setattr(bp, '_QUANTITY_REFINEMENT_TRANSITION_BUDGET', 0)
    tariffs, forecast, commitments = [], [], []
    for i in range(96):
        start = NOW + timedelta(minutes=i * 15)
        end = start + timedelta(minutes=15)
        tariffs.append(TariffWindow(start, end, .1 if i % 4 < 2 else .6, False))
        forecast.append(HourlyForecast(start, end, 0., 0.))
        commitments.append(bp.BatteryLoadCommitment(
            start, start + timedelta(seconds=30), 0., household_power_floor_w=2100))
    result = bp.build_battery_grid_plan(NOW, 10, tariffs, forecast, [1000.] * 24,
                                       config(grid_restart_delay_minutes=5, charge_price_limit=.8),
                                       load_commitments=tuple(commitments))
    assert result.grid_energy_kwh > 0
    assert len(result.slots) == 192
    assert_executable_new_episodes(result)


def test_interrupted_episode_search_keeps_complete_native_fallback(monkeypatch):
    monkeypatch.setattr(bp, '_NATIVE_WINDOW_TRANSITION_BUDGET', 1)
    monkeypatch.setattr(bp, '_EPISODE_COARSE_TRANSITION_BUDGET', 0)
    monkeypatch.setattr(bp, '_EPISODE_PRIMARY_TRANSITION_BUDGET', 0)
    monkeypatch.setattr(bp, '_REFINEMENT_TRANSITION_BUDGET', 0)
    monkeypatch.setattr(bp, '_QUANTITY_REFINEMENT_TRANSITION_BUDGET', 0)
    monkeypatch.setattr(bp, '_CHARGE_WINDOW_TRANSITION_BUDGET', 0)
    result = build([(8, .1), (60, .6)])
    assert result.valid and result.grid_energy_kwh == 0
    assert result.slots[0].start == NOW
    assert result.slots[-1].end == NOW + timedelta(minutes=68)
    assert all(s.action == 'self_consumption' for s in result.slots)


@pytest.mark.parametrize('budget', [0, 1, 40])
def test_incomplete_refinement_cannot_replace_completed_usable_episode(monkeypatch, budget):
    monkeypatch.setattr(bp, '_REFINEMENT_TRANSITION_BUDGET', 0)
    monkeypatch.setattr(bp, '_QUANTITY_REFINEMENT_TRANSITION_BUDGET', 0)
    incumbent = build([(8, .1), (60, .6)])
    monkeypatch.setattr(bp, '_REFINEMENT_TRANSITION_BUDGET', budget)
    monkeypatch.setattr(bp, '_QUANTITY_REFINEMENT_TRANSITION_BUDGET', budget)
    result = build([(8, .1), (60, .6)])
    assert result == incumbent
    assert_executable_new_episodes(result)


def test_no_charge_episode_is_invented_only_to_hold_without_grid_input():
    result = build([(8, .1), (60, .6)], soc=100)
    assert result.grid_energy_kwh == 0
    assert all(s.action != 'charge' for s in result.slots)


@pytest.mark.parametrize('field,value', [
    ('grid_charge_minimum_minutes', -1), ('grid_charge_minimum_minutes', True),
    ('grid_charge_minimum_minutes', float('nan')), ('grid_charge_minimum_minutes', 61),
    ('grid_charge_startup_seconds', -1), ('grid_charge_startup_seconds', True),
    ('grid_charge_startup_seconds', float('inf')), ('grid_charge_startup_seconds', 301),
    ('grid_charge_enable_seconds', -1), ('grid_charge_enable_seconds', True),
    ('grid_charge_enable_seconds', float('nan')), ('grid_charge_enable_seconds', 301),
    ('grid_charge_prepared', 1),
    ('grid_charge_minimum_until', NOW.replace(tzinfo=None)),
    ('grid_charge_minimum_until', 'later'),
])
def test_invalid_episode_configuration_is_rejected(field, value):
    result = build([(8, .1), (60, .6)], cfg=replace(BASE, **{field: value}))
    assert not result.valid
