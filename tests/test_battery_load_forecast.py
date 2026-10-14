"""Energy-preserving estimates for unknown household background demand."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.pv_excess_control.battery_load_forecast import RecentHouseholdLoad

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def sample(model, seconds, watts, prior=1300):
    return model.observe(BASE + timedelta(seconds=seconds), watts, prior)


def test_short_pulse_contributes_energy_without_becoming_a_sustained_step():
    model = RecentHouseholdLoad()
    assert sample(model, 0, 2700) == 1300
    assert sample(model, 30, 1300) == 1650
    assert sample(model, 120, 1300) == 1650
    assert sample(model, 150, 1300) == 1300


def test_sustained_rise_is_fully_recognized_after_two_minutes():
    model = RecentHouseholdLoad()
    sample(model, 0, 2700)
    assert sample(model, 30, 2700) == 1650
    assert sample(model, 60, 2700) == 2000
    assert sample(model, 120, 2700) == 2700


def test_repeated_duty_cycle_preserves_average_energy():
    model = RecentHouseholdLoad()
    for seconds, watts in [(0, 2700), (30, 1300), (60, 2700), (90, 1300)]:
        sample(model, seconds, watts)
    assert sample(model, 120, 2700) == 2000
    assert sample(model, 150, 1300) == 2000


def test_irregular_reports_are_time_weighted_and_window_edges_are_clipped():
    model = RecentHouseholdLoad()
    sample(model, 0, 2500, prior=1000)
    sample(model, 10, 1000, prior=1000)
    sample(model, 90, 1000, prior=1000)
    assert sample(model, 120, 1000, prior=1000) == 1125
    assert sample(model, 125, 1000, prior=1000) == 1062.5
    assert sample(model, 130, 1000, prior=1000) == 1000


def test_duplicate_report_does_not_change_value_or_prior():
    model = RecentHouseholdLoad()
    sample(model, 0, 2700)
    for _ in range(20):
        assert sample(model, 0, 1000, prior=9000) == 1300
    assert sample(model, 120, 1300) == 2700


def test_initial_prior_is_fixed_until_reset():
    model = RecentHouseholdLoad()
    sample(model, 0, 2700)
    assert sample(model, 30, 2700, prior=0) == 1650
    model.reset()
    assert sample(model, 40, 2700, prior=1000) == 1000


@pytest.mark.parametrize('seconds', [-1, 121])
def test_backwards_time_or_observation_gap_resets_history(seconds):
    model = RecentHouseholdLoad()
    sample(model, 0, 2700)
    assert sample(model, seconds, 2000, prior=1000) == 1000
    assert sample(model, seconds + 60, 2000, prior=0) == 1500


def test_real_zero_is_valid_and_does_not_reset_history():
    model = RecentHouseholdLoad()
    sample(model, 0, 2400, prior=2400)
    assert sample(model, 60, 0, prior=0) == 2400
    assert sample(model, 120, 0, prior=0) == 1200
    assert sample(model, 180, 0, prior=0) == 0


@pytest.mark.parametrize('field,value', [
    ('watts', float('nan')), ('watts', float('inf')), ('watts', -1),
    ('watts', None), ('watts', True), ('prior', -1), ('prior', float('nan')),
    ('timestamp', None), ('timestamp', datetime(2026, 1, 1)),
])
def test_invalid_observations_clear_history_without_becoming_zero(field, value):
    model = RecentHouseholdLoad()
    sample(model, 0, 2700)
    values = {'timestamp': BASE + timedelta(seconds=10), 'watts': 2700, 'prior': 1300}
    values[field] = value
    with pytest.raises(ValueError):
        model.observe(values['timestamp'], values['watts'], values['prior'])
    assert sample(model, 20, 4000, prior=1000) == 1000


def test_elapsed_time_uses_utc_across_local_clock_fallback():
    model = RecentHouseholdLoad()
    berlin = ZoneInfo('Europe/Berlin')
    before = datetime(2026, 10, 25, 2, 59, tzinfo=berlin, fold=0)
    after = datetime(2026, 10, 25, 2, 1, tzinfo=berlin, fold=1)
    assert model.observe(before, 2700, 1300) == 1300
    assert model.observe(after, 2700, 1300) == 2700
