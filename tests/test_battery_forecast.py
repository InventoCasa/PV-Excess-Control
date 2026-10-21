"""Battery uncertainty inputs preserve the operational forecast and missing data."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib
from zoneinfo import ZoneInfo

import pytest

from custom_components.pv_excess_control.models import HourlyForecast

BASE = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
ENTITY = "sensor.array_forecast"


def row(start=BASE, hours=1, watts=2000):
    return HourlyForecast(start, start + timedelta(hours=hours), watts * hours / 1000, watts)


def source(rows):
    return {"state": "8", "attributes": {"detailedForecast": rows}}


def slot(start=BASE, hours=1, nominal=2, low=1):
    return {"period_start": start.isoformat(),
            "period_end": (start + timedelta(hours=hours)).isoformat(),
            "pv_estimate": nominal, "pv_estimate10": low}


def build(**kwargs):
    name = "custom_components.pv_excess_control.battery_forecast"
    assert importlib.util.find_spec(name) is not None, "battery forecast scenarios are not implemented"
    module = importlib.import_module(name)
    defaults = {"provider_type": "solcast", "nominal": [row()],
                "states": {ENTITY: source([slot()])}, "entities": [ENTITY],
                "now": BASE, "factor": .75}
    defaults.update(kwargs)
    return module.build_battery_forecast_scenarios(**defaults)


def test_lower_estimate_is_not_calibrated_twice_and_inputs_are_unchanged():
    nominal = [row()]
    states = {ENTITY: source([slot()])}
    before = deepcopy((nominal, states))
    result = build(nominal=nominal, states=states)
    assert result.low_status == "available"
    assert result.nominal[0].expected_kwh == 1.5
    assert result.low[0].expected_kwh == 1
    assert result.low[0].expected_watts == 1000
    assert (nominal, states) == before


def test_lower_scenario_never_exceeds_calibrated_nominal():
    result = build(states={ENTITY: source([slot(low=1.9)])})
    assert result.low[0].expected_kwh == result.nominal[0].expected_kwh == 1.5


def test_true_zero_lower_estimate_is_valid():
    result = build(states={ENTITY: source([slot(low=0)])})
    assert result.low_status == "available"
    assert result.low[0].expected_kwh == 0


@pytest.mark.parametrize("low", [None, True, -1, float("nan"), float("inf"), "unavailable", {}])
def test_invalid_lower_estimate_does_not_remove_nominal_or_become_zero(low):
    result = build(states={ENTITY: source([slot(low=low)])})
    assert result.low is None
    assert result.low_status != "available"
    assert result.nominal[0].expected_kwh == 1.5


def test_one_missing_quantile_invalidates_the_entire_future_lower_series():
    second = slot(BASE + timedelta(hours=1))
    del second["pv_estimate10"]
    result = build(nominal=[row(), row(BASE + timedelta(hours=1))],
                   states={ENTITY: source([slot(), second])})
    assert result.low is None
    assert result.low_status == "missing_lower_estimate"


def test_missing_quantile_for_a_fully_elapsed_interval_is_ignored():
    past = slot(BASE - timedelta(hours=1))
    del past["pv_estimate10"]
    result = build(nominal=[row(BASE - timedelta(hours=1)), row()],
                   states={ENTITY: source([past, slot()])})
    assert result.low_status == "available"
    assert len(result.nominal) == 2
    assert len(result.low) == 1
    assert result.low[0].start == BASE


def test_current_partial_interval_keeps_original_energy_and_boundaries():
    result = build(now=BASE + timedelta(minutes=20))
    assert result.low[0].start == BASE
    assert result.low[0].end == BASE + timedelta(hours=1)
    assert result.low[0].expected_kwh == 1


@pytest.mark.parametrize("provider", ["forecast_solar", "generic", "none"])
def test_provider_without_quantiles_has_explicit_unavailable_lower_scenario(provider):
    result = build(provider_type=provider)
    assert result.low is None
    assert result.low_status == "unsupported_provider"
    assert result.nominal[0].expected_kwh == 1.5


def test_incomplete_source_coverage_is_not_silently_zero_filled():
    result = build(nominal=[row(), row(BASE + timedelta(hours=1))])
    assert result.low is None
    assert result.low_status == "incomplete_lower_forecast"


def test_overlapping_source_rows_are_rejected_instead_of_last_wins():
    result = build(states={ENTITY: source([slot(), slot(BASE + timedelta(minutes=30))])})
    assert result.low is None
    assert result.low_status == "overlapping_lower_forecast"


def test_distinct_arrays_are_summed_without_losing_a_source():
    second = "sensor.other_array_forecast"
    result = build(nominal=[row(watts=3000)], entities=[ENTITY, second],
                   states={ENTITY: source([slot()]), second: source([slot(nominal=1, low=.4)])})
    assert result.low_status == "available"
    assert result.nominal[0].expected_kwh == 2.25
    assert result.low[0].expected_kwh == pytest.approx(1.4)
    assert result.source_ids == (ENTITY, second)


def test_one_array_missing_quantiles_cannot_disappear_from_aggregate():
    second = "sensor.other_array_forecast"
    missing = slot(nominal=1)
    del missing["pv_estimate10"]
    result = build(nominal=[row(watts=3000)], entities=[ENTITY, second],
                   states={ENTITY: source([slot()]), second: source([missing])})
    assert result.low is None
    assert result.low_status == "missing_lower_estimate"


def test_one_array_gap_is_unavailable_even_when_other_array_covers_it():
    second = "sensor.other_array_forecast"
    result = build(nominal=[row(hours=.5, watts=3000), row(BASE + timedelta(minutes=30), hours=.5)],
                   entities=[ENTITY, second],
                   states={ENTITY: source([slot()]), second: source([slot(hours=.5, nominal=1, low=.4)])})
    assert result.low is None
    assert result.low_status == "incomplete_lower_forecast"


def test_changed_source_snapshot_is_not_combined_with_old_nominal():
    result = build(states={ENTITY: source([slot(nominal=3)])})
    assert result.low is None
    assert result.low_status == "source_forecast_mismatch"


def test_duplicate_configured_entity_is_not_double_counted():
    result = build(entities=[ENTITY, ENTITY])
    assert result.low_status == "available"
    assert result.low[0].expected_kwh == 1
    assert result.source_ids == (ENTITY,)


def test_explicit_tomorrow_sources_replace_primary_tomorrow_values():
    tomorrow = BASE.replace(hour=0) + timedelta(days=1)
    second = "sensor.tomorrow_forecast"
    result = build(nominal=[row(), row(tomorrow, watts=4000)],
                   tomorrow_entities=[second],
                   states={ENTITY: source([slot(), slot(tomorrow, nominal=9, low=8)]),
                           second: source([slot(tomorrow, nominal=4, low=2)])})
    assert result.low_status == "available"
    assert [r.expected_kwh for r in result.low] == [1, 2]


def test_tomorrow_replacement_uses_local_midnight_across_clock_change():
    local = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 10, 24, 22, tzinfo=local)
    boundary = datetime(2026, 10, 25, tzinfo=local).astimezone(timezone.utc)
    second = "sensor.tomorrow_forecast"
    result = build(now=now, nominal=[row(boundary, watts=4000)],
                   tomorrow_entities=[second],
                   states={ENTITY: source([slot(boundary, nominal=9, low=8)]),
                           second: source([slot(boundary, nominal=4, low=2)])})
    assert result.low_status == "available"
    assert result.low[0].expected_kwh == 2


@pytest.mark.parametrize("field", ["period_start", "period_end"])
def test_naive_source_timestamp_is_not_assumed_to_be_local_or_utc(field):
    value = slot()
    value[field] = datetime.fromisoformat(value[field]).replace(tzinfo=None).isoformat()
    result = build(states={ENTITY: source([value])})
    assert result.low is None
    assert result.low_status == "invalid_lower_forecast"


def test_provider_interval_end_and_duration_are_supported():
    value = {"period_end": (BASE + timedelta(minutes=30)).isoformat(),
             "period": "PT30M", "pv_estimate": 2, "pv_estimate10": 1}
    result = build(nominal=[row(hours=.5)], states={ENTITY: source([value])})
    assert result.low_status == "available"
    assert result.low[0].expected_kwh == .5


def test_detailed_hourly_default_duration_matches_shared_provider():
    value = {"period_start": BASE.isoformat(), "pv_estimate": 2, "pv_estimate10": 1}
    result = build(states={ENTITY: {"state": "2", "attributes": {"detailedHourly": [value]}}})
    assert result.low_status == "available"
    assert result.low[0].expected_kwh == 1


@pytest.mark.parametrize("factor", [True, 0, 1.1, float("nan")])
def test_invalid_nominal_calibration_is_rejected(factor):
    with pytest.raises(ValueError, match="factor"):
        build(factor=factor)


def test_finite_kw_that_overflows_watts_is_invalid_not_nominal_fallback():
    result = build(states={ENTITY: source([slot(low=1e308)])})
    assert result.low is None
    assert result.low_status == "invalid_lower_forecast"


@pytest.mark.parametrize("values", [{}, {ENTITY: {"state": "unavailable", "attributes": {}}}])
def test_missing_source_is_unavailable_without_affecting_nominal(values):
    result = build(states=values)
    assert result.low is None
    assert result.nominal[0].expected_kwh == 1.5


def test_no_future_intervals_have_an_explicit_status():
    result = build(now=BASE + timedelta(hours=1))
    assert result.low is None
    assert result.low_status == "no_future_forecast"
    assert len(result.nominal) == 1


def test_zero_nighttime_interval_requires_and_accepts_explicit_zero_quantile():
    result = build(nominal=[row(watts=0)], states={ENTITY: source([slot(nominal=0, low=0)])})
    assert result.low_status == "available"
    assert result.low[0].expected_kwh == 0


def test_partial_malformed_source_sequence_is_not_silently_skipped():
    result = build(states={ENTITY: source([slot(), "invalid interval"])})
    assert result.low is None
    assert result.low_status == "invalid_lower_forecast"


@pytest.mark.parametrize("field,value", [("expected_watts", True), ("expected_kwh", float("nan")),
                                        ("expected_kwh", -1)])
def test_invalid_nominal_energy_fails_explicitly(field, value):
    interval = row()
    setattr(interval, field, value)
    with pytest.raises(ValueError, match="nominal"):
        build(nominal=[interval])


def test_legacy_forecasts_attribute_precedence_matches_shared_provider():
    result = build(states={ENTITY: {"state": "8", "attributes": {
        "forecasts": [slot(low=.5)], "detailedForecast": [slot(low=1.5)]}}})
    assert result.low[0].expected_kwh == .5


def test_energy_based_nominal_remains_usable_when_redundant_watts_disagree():
    interval = row(watts=0)
    interval.expected_kwh = 1
    result = build(nominal=[interval])
    assert result.nominal[0].expected_kwh == .75
    assert result.nominal[0].expected_watts == 0
    assert result.low is None
    assert result.low_status == "nominal_power_energy_mismatch"


@pytest.mark.parametrize("field", ["expected_kwh", "expected_watts"])
def test_numeric_string_in_typed_nominal_is_rejected_with_controlled_error(field):
    interval = row()
    setattr(interval, field, str(getattr(interval, field)))
    with pytest.raises(ValueError, match="nominal"):
        build(nominal=[interval])
