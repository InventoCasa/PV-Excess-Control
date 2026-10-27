"""Battery tariff handoffs require coherent live prices and unambiguous UTC time."""
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import importlib
import importlib.util
from zoneinfo import ZoneInfo

import pytest

from custom_components.pv_excess_control.battery_planner import BatteryPlanningConfig, build_battery_grid_plan
from custom_components.pv_excess_control.energy import GenericTariffProvider
from custom_components.pv_excess_control.models import HourlyForecast, TariffInfo, TariffWindow

UTC = timezone.utc
NOW = datetime(2030, 1, 1, 0, 15, tzinfo=UTC)


def freeze(now, live_price, candidate):
    name = "custom_components.pv_excess_control.battery_tariff"
    assert importlib.util.find_spec(name) is not None, "Coherent battery tariff handoff is not implemented"
    return importlib.import_module(name).freeze_battery_tariff(now, live_price, candidate)


def tariff(price=.2, windows=None, cap=.8):
    return TariffInfo(price, .07, .17, cap, windows if windows is not None else [
        TariffWindow(NOW, NOW + timedelta(minutes=15), price, False),
    ])


def test_fresh_provider_handoff_at_quarter_hour_preserves_shared_old_snapshot():
    windows = [TariffWindow(NOW - timedelta(minutes=15), NOW, .18, False),
               TariffWindow(NOW, NOW + timedelta(minutes=15), .2, False)]
    old = tariff(.18, windows)
    with pytest.raises(ValueError, match="price_snapshot_changed"):
        freeze(NOW, .2, old)
    provider = GenericTariffProvider("sensor.price")
    current = provider.get_tariff_info({"sensor.price": {"state": ".2", "attributes": {
        "price_windows": [{"start": w.start.isoformat(), "end": w.end.isoformat(), "price": w.price} for w in windows],
    }}}, .17, .8, .07)
    snapshot = freeze(NOW, .2, current)
    assert snapshot.current_price == .2
    assert snapshot.windows[1].start == NOW
    assert old.current_price == .18 and old.windows == windows
    current.current_price = .9
    current.windows.clear()
    assert snapshot.current_price == .2 and len(snapshot.windows) == 2
    with pytest.raises(FrozenInstanceError):
        snapshot.current_price = .9


def test_rounded_window_uses_full_precision_current_price_without_mutating_provider():
    live = .2839697
    candidate = tariff(live, [TariffWindow(NOW, NOW + timedelta(minutes=15), .2840, False)])
    snapshot = freeze(NOW, live, candidate)
    assert snapshot.current_price == live and snapshot.windows[0].price == live
    assert candidate.windows[0].price == .2840


def test_live_price_above_cap_cannot_hide_in_rounded_current_window():
    live = .30004
    candidate = tariff(live, [TariffWindow(NOW, NOW + timedelta(hours=1), .3, False),
                             TariffWindow(NOW + timedelta(hours=1), NOW + timedelta(hours=2), .6, False)], cap=.3)
    snapshot = freeze(NOW, live, candidate)
    forecast = [HourlyForecast(NOW, NOW + timedelta(hours=2), 0., 0.)]
    config = BatteryPlanningConfig(2., 0., 100., 1000., 1000., charge_price_limit=.3,
                                   hold_supported=True, horizon_hours=2.)
    plan = build_battery_grid_plan(NOW, 0., snapshot.windows, forecast, [500.] * 24, config)
    assert plan.valid and plan.grid_energy_kwh == 0.


def test_safe_next_slot_can_still_be_economically_charged():
    candidate = tariff(.2, [TariffWindow(NOW, NOW + timedelta(hours=1), .2, False),
                           TariffWindow(NOW + timedelta(hours=1), NOW + timedelta(hours=2), .6, False)])
    snapshot = freeze(NOW, .2, candidate)
    config = BatteryPlanningConfig(2., 0., 100., 1000., 1000., charge_price_limit=.8,
                                   hold_supported=True, horizon_hours=2., grid_charge_active=True)
    plan = build_battery_grid_plan(NOW, 0., snapshot.windows,
                                  [HourlyForecast(NOW, NOW + timedelta(hours=2), 0., 0.)], [500.] * 24, config)
    assert plan.valid and plan.grid_energy_kwh > 0.


def test_real_mid_slot_price_change_does_not_reuse_old_window():
    candidate = tariff(.25, [TariffWindow(NOW, NOW + timedelta(minutes=15), .2, False)])
    with pytest.raises(ValueError, match="price_window_mismatch"):
        freeze(NOW + timedelta(minutes=5), .25, candidate)


@pytest.mark.parametrize("live", [None, True, float("nan"), float("inf"), "unknown"])
def test_invalid_live_price_is_not_a_fallback(live):
    with pytest.raises(ValueError, match="price_unavailable"):
        freeze(NOW, live, tariff())


@pytest.mark.parametrize("candidate", [None, tariff(float("inf")), tariff(float("nan")), tariff(True)])
def test_invalid_provider_price_is_rejected(candidate):
    with pytest.raises(ValueError, match="price_unavailable"):
        freeze(NOW, .2, candidate)


@pytest.mark.parametrize("windows", [
    [],
    [TariffWindow(NOW - timedelta(minutes=15), NOW, .2, False)],
    [TariffWindow(NOW + timedelta(seconds=1), NOW + timedelta(minutes=15), .2, False)],
    [TariffWindow(NOW, NOW + timedelta(minutes=15), .2, False)] * 2,
    [TariffWindow(NOW, NOW + timedelta(minutes=15), .2, False),
     TariffWindow(NOW - timedelta(minutes=5), NOW + timedelta(minutes=20), .2, False)],
])
def test_missing_or_overlapping_coverage_is_rejected(windows):
    with pytest.raises(ValueError, match="price_window_mismatch"):
        freeze(NOW, .2, tariff(windows=windows))


@pytest.mark.parametrize("window", [
    TariffWindow(NOW.replace(tzinfo=None), NOW + timedelta(minutes=15), .2, False),
    TariffWindow(NOW, NOW, .2, False),
    TariffWindow(NOW + timedelta(minutes=1), NOW, .2, False),
    TariffWindow(NOW, NOW + timedelta(minutes=15), float("nan"), False),
    TariffWindow(NOW, NOW + timedelta(minutes=15), True, False),
])
def test_malformed_windows_fail_closed(window):
    with pytest.raises(ValueError, match="price_window_mismatch"):
        freeze(NOW, .2, tariff(windows=[window]))


def test_naive_current_time_is_rejected():
    with pytest.raises(ValueError, match="price_window_mismatch"):
        freeze(NOW.replace(tzinfo=None), .2, tariff())


def test_repeated_autumn_hour_uses_real_utc_coverage():
    zone = ZoneInfo("Europe/Berlin")
    first = datetime(2030, 10, 27, 2, tzinfo=zone, fold=0)
    second = datetime(2030, 10, 27, 2, tzinfo=zone, fold=1)
    end = datetime(2030, 10, 27, 3, tzinfo=zone)
    windows = [TariffWindow(first, second, .1, True), TariffWindow(second, end, .2, False)]
    snapshot = freeze(second, .2, tariff(.2, windows))
    assert snapshot.windows[0].end == snapshot.windows[1].start
    assert snapshot.windows[0].end - snapshot.windows[0].start == timedelta(hours=1)
    assert snapshot.windows[1].start == second.astimezone(UTC)


def test_future_prices_and_thresholds_are_preserved_in_detached_snapshot():
    windows = [TariffWindow(NOW, NOW + timedelta(minutes=15), .2, False),
               TariffWindow(NOW + timedelta(minutes=15), NOW + timedelta(minutes=30), -.01, True)]
    snapshot = freeze(NOW, .2, tariff(.2, windows))
    assert snapshot.windows[1] == windows[1]
    assert snapshot.feed_in_tariff == .07
    assert snapshot.cheap_price_threshold == .17
    assert snapshot.battery_charge_price_threshold == .8
    assert isinstance(snapshot.windows, tuple)


@pytest.mark.parametrize("future_start", [10, 20])
def test_future_overlap_or_gap_keeps_planner_rejection(future_start):
    candidate = tariff(.2, [TariffWindow(NOW, NOW + timedelta(minutes=15), .2, False),
                           TariffWindow(NOW + timedelta(minutes=future_start), NOW + timedelta(hours=1), .4, False)])
    snapshot = freeze(NOW, .2, candidate)
    config = BatteryPlanningConfig(2., 0., 100., 1000., 1000., charge_price_limit=.8)
    plan = build_battery_grid_plan(NOW, 0., snapshot.windows,
                                  [HourlyForecast(NOW, NOW + timedelta(hours=1), 0., 0.)], [500.] * 24, config)
    assert not plan.valid and plan.reason == "tariff_gap_or_overlap"
