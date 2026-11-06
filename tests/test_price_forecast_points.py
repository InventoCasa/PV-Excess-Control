"""Version 2 carries explicit point forecasts without inventing price bounds."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest

from tests.test_price_forecast import NOW, parser, payload


def point_payload():
    source = payload()
    source["schema_version"] = 2
    source["intervals"][0].update(lower=None, upper=None, source="external-provider")
    return source


def test_v2_point_preserves_null_bounds_source_and_schema():
    source = point_payload()
    expected = deepcopy(source)
    snapshot = parser().parse_price_forecast(source, now=NOW)
    assert snapshot.as_dict() == expected
    assert snapshot.schema_version == 2
    assert snapshot.intervals[0].lower is None
    assert snapshot.intervals[0].upper is None
    assert snapshot.intervals[0].source == "external-provider"
    source["intervals"][0]["source"] = "changed"
    exported = snapshot.as_dict()
    exported["intervals"][0]["price"] = 99
    assert snapshot.as_dict() == expected


def test_v2_mixes_explicit_point_and_bounded_intervals():
    source = point_payload()
    source["intervals"][0]["price"] = -.01
    source["intervals"].append({
        "start": (NOW + timedelta(minutes=15)).isoformat(),
        "end": (NOW + timedelta(minutes=30)).isoformat(),
        "price": 0.0, "lower": 0.0, "upper": 0.0, "source": "another-provider",
    })
    assert parser().parse_price_forecast(source, now=NOW).as_dict() == source


def test_v2_opaque_source_is_preserved_without_interpretation():
    source = point_payload()
    source["intervals"][0]["source"] = "x" * 128
    snapshot = parser().parse_price_forecast(source, now=NOW)
    assert snapshot.as_dict()["intervals"][0]["source"] == "x" * 128
    assert snapshot.as_dict()["quality"] == {"status": "experimental", "automation_eligible": False}


@pytest.mark.parametrize("field", ["price", "lower", "upper", "source"])
def test_v2_requires_explicit_keys_even_for_null_bounds(field):
    source = point_payload()
    del source["intervals"][0][field]
    with pytest.raises(ValueError):
        parser().parse_price_forecast(source, now=NOW)


def test_v2_missing_both_bounds_is_not_an_explicit_point():
    source = point_payload()
    del source["intervals"][0]["lower"]
    del source["intervals"][0]["upper"]
    with pytest.raises(ValueError):
        parser().parse_price_forecast(source, now=NOW)


@pytest.mark.parametrize("lower,upper", [
    (None, .1), (-.1, None), (False, .1), (-.1, True),
    (float("nan"), .1), (-.1, float("inf")), ("-.1", .1),
    (.1, .2), (-.2, -.1), (.1, -.1),
])
def test_v2_rejects_partial_nonnumeric_or_unordered_bounds(lower, upper):
    source = point_payload()
    source["intervals"][0].update(lower=lower, upper=upper)
    with pytest.raises(ValueError):
        parser().parse_price_forecast(source, now=NOW)


@pytest.mark.parametrize("value", [None, "", "  ", True, 0, [], {}, "x" * 129])
def test_v2_requires_bounded_nonempty_source(value):
    source = point_payload()
    source["intervals"][0]["source"] = value
    with pytest.raises(ValueError):
        parser().parse_price_forecast(source, now=NOW)


@pytest.mark.parametrize("value", [None, True, "0", float("nan"), float("inf")])
def test_v2_point_price_is_still_required_and_finite(value):
    source = point_payload()
    source["intervals"][0]["price"] = value
    with pytest.raises(ValueError):
        parser().parse_price_forecast(source, now=NOW)


@pytest.mark.parametrize("version", [True, 2.0, "2", 3])
def test_schema_version_remains_strict(version):
    source = point_payload()
    source["schema_version"] = version
    with pytest.raises(ValueError, match="unsupported_schema"):
        parser().parse_price_forecast(source, now=NOW)


def test_v1_still_rejects_null_bounds_and_ignores_source_metadata():
    source = payload()
    expected = deepcopy(source)
    source["intervals"][0]["source"] = "unused-metadata"
    assert parser().parse_price_forecast(source, now=NOW).as_dict() == expected
    source["intervals"][0].update(lower=None, upper=None)
    with pytest.raises(ValueError):
        parser().parse_price_forecast(source, now=NOW)


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(unit="ct/kWh"),
    lambda p: p.update(generated_at=(NOW + timedelta(seconds=1)).isoformat()),
    lambda p: p.update(expires_at=(NOW + timedelta(hours=7)).isoformat()),
    lambda p: p["quality"].update(automation_eligible=True),
    lambda p: p["intervals"][0].update(end=(NOW + timedelta(minutes=5)).isoformat()),
    lambda p: p["intervals"][0].update(start=(NOW + timedelta(days=7)).isoformat(), end=(NOW + timedelta(days=7, minutes=15)).isoformat()),
    lambda p: p["intervals"].append(dict(p["intervals"][0])),
    lambda p: p.update(intervals=p["intervals"] * 673),
])
def test_v2_keeps_shared_freshness_units_and_coverage_guards(mutation):
    source = point_payload()
    mutation(source)
    with pytest.raises(ValueError):
        parser().parse_price_forecast(source, now=NOW)


def test_v2_expiry_remains_a_hard_boundary():
    with pytest.raises(ValueError, match="expired"):
        parser().parse_price_forecast(point_payload(), now=NOW + timedelta(hours=6))


def test_v2_point_is_diagnostic_only_and_never_a_confirmed_tariff(coordinator_factory, mock_tariff_at):
    coordinator = coordinator_factory(config_data={"price_forecast_sensor": "sensor.forecast"})
    coordinator.hass.states.get.return_value = SimpleNamespace(state="experimental", attributes=point_payload())
    tariff = mock_tariff_at(.5)
    coordinator._last_tariff_info = tariff
    data = coordinator._read_price_forecast(now=NOW)
    assert data["status"] == "available"
    assert data["snapshot"]["schema_version"] == 2
    assert data["snapshot"]["intervals"][0]["lower"] is None
    assert data["snapshot"]["intervals"][0]["source"] == "external-provider"
    assert data["automation_eligible"] is False
    assert coordinator._last_tariff_info is tariff
    assert coordinator._grid_charge_engaged is False
    coordinator.hass.services.async_call.assert_not_called()
