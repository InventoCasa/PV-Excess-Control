"""Tests for Planner.plan_battery_charge_curve (#17)."""
from __future__ import annotations

from datetime import datetime, time, timezone

import pytest

from custom_components.pv_excess_control.models import (
    HourlyForecast,
    ForecastData,
    BatteryChargeCurve,
)
from custom_components.pv_excess_control.planner import Planner


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

UTC = timezone.utc


def _hourly(start_h: int, expected_watts: float) -> HourlyForecast:
    return HourlyForecast(
        start=datetime(2026, 5, 5, start_h, 0, tzinfo=UTC),
        end=datetime(2026, 5, 5, start_h + 1, 0, tzinfo=UTC),
        expected_kwh=expected_watts / 1000.0,
        expected_watts=expected_watts,
    )


def _forecast(*hours: HourlyForecast) -> ForecastData:
    fd = ForecastData(remaining_today_kwh=0.0)
    fd.hourly_breakdown = list(hours)
    return fd


def _planner() -> Planner:
    return Planner()


def _energy_kwh(curve: BatteryChargeCurve, until: datetime | None = None) -> float:
    total = 0.0
    for sp in curve.setpoints:
        if until is not None and sp.start >= until:
            break
        end = min(sp.end, until) if until else sp.end
        hours = (end - sp.start).total_seconds() / 3600.0
        if hours <= 0:
            continue
        total += sp.max_charge_w * hours / 1000.0
    return total


# ---------------------------------------------------------------------------
# Algorithm tests
# ---------------------------------------------------------------------------

class TestBatteryChargeCurve:
    def _common_kwargs(self):
        return dict(
            now=datetime(2026, 5, 5, 8, tzinfo=UTC),
            current_soc=30.0,
            battery_capacity_kwh=10.0,
            battery_max_charge_power_w=5000,
            battery_trickle_charge_power_w=100,
            export_soft_ceiling_w=6000,
            target_soc=80.0,
            target_time=time(18, 0),
            base_load_watts=500,
            appliance_absorption_per_slot={},
        )

    def test_no_curtailment_no_deferral(self):
        forecast = _forecast(*[_hourly(h, 4000.0) for h in range(8, 18)])
        curve = _planner().plan_battery_charge_curve(forecast=forecast, **self._common_kwargs())
        assert curve.fallback_reason is None
        assert curve.setpoints[0].max_charge_w == 5000

    def test_curtailment_alone_covers_target(self):
        forecast = _forecast(*[
            _hourly(h, 1000.0) if h < 11 or h >= 15 else _hourly(h, 8000.0)
            for h in range(8, 18)
        ])
        curve = _planner().plan_battery_charge_curve(forecast=forecast, **self._common_kwargs())
        morning = [sp for sp in curve.setpoints if sp.start.hour < 11]
        assert all(sp.max_charge_w == 100 for sp in morning)
        peak = [sp for sp in curve.setpoints if 11 <= sp.start.hour < 15]
        assert all(sp.max_charge_w > 100 for sp in peak)

    def test_curtailment_insufficient_no_deferral(self):
        forecast = _forecast(*[
            _hourly(h, 1000.0) if h != 13 else _hourly(h, 6500.0)
            for h in range(8, 18)
        ])
        curve = _planner().plan_battery_charge_curve(forecast=forecast, **self._common_kwargs())
        assert curve.setpoints[0].max_charge_w > 100

    def test_target_time_invariant(self):
        forecast = _forecast(*[_hourly(h, 4000.0) for h in range(8, 18)])
        kwargs = self._common_kwargs()
        curve = _planner().plan_battery_charge_curve(forecast=forecast, **kwargs)
        target_dt = datetime(2026, 5, 5, 18, tzinfo=UTC)
        target_kwh = (kwargs["target_soc"] - kwargs["current_soc"]) * kwargs["battery_capacity_kwh"] / 100.0
        assert _energy_kwh(curve, until=target_dt) + 0.01 >= target_kwh

    def test_setpoint_bounded(self):
        forecast = _forecast(*[_hourly(h, 9000.0) for h in range(8, 18)])
        curve = _planner().plan_battery_charge_curve(forecast=forecast, **self._common_kwargs())
        assert all(100 <= sp.max_charge_w <= 5000 for sp in curve.setpoints)

    def test_curtailment_residual_after_appliances(self):
        forecast = _forecast(*[
            _hourly(h, 1000.0) if h != 13 else _hourly(h, 11000.0)
            for h in range(8, 18)
        ])
        kwargs = self._common_kwargs()
        kwargs["appliance_absorption_per_slot"] = {
            datetime(2026, 5, 5, 13, tzinfo=UTC): 2000.0,
        }
        curve = _planner().plan_battery_charge_curve(forecast=forecast, **kwargs)
        slot_13 = next(sp for sp in curve.setpoints if sp.start.hour == 13)
        # Gross 11000-500=10500, minus ceiling 6000 = 4500W gross curtailment,
        # minus 2000W appliance = 2500W net for battery.
        assert 2400 <= slot_13.max_charge_w <= 2600

    def test_fallback_forecast_unavailable(self):
        forecast = _forecast()
        curve = _planner().plan_battery_charge_curve(forecast=forecast, **self._common_kwargs())
        assert curve.fallback_reason == "forecast_unavailable"
        assert curve.setpoints[0].max_charge_w == 5000

    def test_fallback_soc_unavailable(self):
        forecast = _forecast(*[_hourly(h, 4000.0) for h in range(8, 18)])
        kwargs = self._common_kwargs()
        kwargs["current_soc"] = None
        curve = _planner().plan_battery_charge_curve(forecast=forecast, **kwargs)
        assert curve.fallback_reason == "soc_unavailable"
        assert curve.setpoints[0].max_charge_w == 5000

    def test_already_at_target_soc(self):
        forecast = _forecast(*[_hourly(h, 4000.0) for h in range(8, 18)])
        kwargs = self._common_kwargs()
        kwargs["current_soc"] = 85.0
        curve = _planner().plan_battery_charge_curve(forecast=forecast, **kwargs)
        assert curve.fallback_reason == "already_at_target"
        assert all(sp.max_charge_w == 100 for sp in curve.setpoints)

    def test_backfill_when_curtailment_short(self):
        # Construct a scenario where:
        # - deferrable=True (curtailment_kwh >= target_kwh, and at least one
        #   curtailment slot lands before target_dt)
        # - first-pass projected energy by target_dt is INSUFFICIENT,
        #   forcing the backfill helper to promote pre-target trickle slots
        #
        # Hours 8-15: small excess, no curtailment (becomes trickle slots).
        # Hour 16: small curtailment slot just before target_dt=17:00.
        # Hours 17-21: heavy curtailment AFTER target_dt (drives deferrable=True
        #   via curtailment_kwh, but contributes nothing to the pre-target integral).
        forecast = _forecast(
            *[_hourly(h, 1000.0) for h in range(8, 16)],   # hours 8-15: no curt
            _hourly(16, 7000.0),                            # hour 16: small curt
            *[_hourly(h, 15000.0) for h in range(17, 22)], # hours 17-21: heavy curt (post-target)
        )
        kwargs = self._common_kwargs()
        kwargs["target_time"] = time(17, 0)
        curve = _planner().plan_battery_charge_curve(forecast=forecast, **kwargs)

        target_dt = datetime(2026, 5, 5, 17, tzinfo=UTC)
        target_kwh = (kwargs["target_soc"] - kwargs["current_soc"]) * kwargs["battery_capacity_kwh"] / 100.0

        # Backfill must ensure target met by target_time.
        assert _energy_kwh(curve, until=target_dt) + 0.01 >= target_kwh

        # And we should observe that backfill actually ran: at least one slot
        # ending at or before target_dt has been promoted to max charge power
        # (i.e., is no longer at trickle).
        promoted = [
            sp for sp in curve.setpoints
            if sp.end <= target_dt and sp.max_charge_w == kwargs["battery_max_charge_power_w"]
        ]
        assert len(promoted) >= 1
