"""Tests for PV Excess Control planner - timeline, battery, scheduling & plan."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import pytest

from custom_components.pv_excess_control.models import (
    Action,
    ApplianceConfig,
    BatteryAllocation,
    BatteryConfig,
    BatteryStrategy,
    BatteryTarget,
    ForecastData,
    HourlyForecast,
    Plan,
    PlanEntry,
    PlanReason,
    TariffInfo,
    TariffWindow,
    TimeSlot,
)
from custom_components.pv_excess_control.planner import Planner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dt(hour: int, minute: int = 0) -> datetime:
    """Create a UTC datetime on 2026-03-22 at the given hour:minute."""
    return datetime(2026, 3, 22, hour, minute, 0, tzinfo=timezone.utc)


def _make_hourly_forecast(
    start_hour: int,
    watts: float,
    duration_minutes: int = 60,
) -> HourlyForecast:
    """Create an HourlyForecast starting at the given hour."""
    start = _dt(start_hour)
    end = start + timedelta(minutes=duration_minutes)
    kwh = watts * (duration_minutes / 60.0) / 1000.0
    return HourlyForecast(start=start, end=end, expected_kwh=kwh, expected_watts=watts)


def _make_half_hour_forecast(
    hour: int,
    minute: int,
    watts: float,
) -> HourlyForecast:
    """Create a 30-minute HourlyForecast at the given hour:minute."""
    start = _dt(hour, minute)
    end = start + timedelta(minutes=30)
    kwh = watts * 0.5 / 1000.0  # 30 min at watts -> kWh
    return HourlyForecast(start=start, end=end, expected_kwh=kwh, expected_watts=watts)


def _make_tariff_window(
    start_hour: int,
    end_hour: int,
    price: float,
    is_cheap: bool = False,
) -> TariffWindow:
    """Create a TariffWindow between two hours on the same day."""
    return TariffWindow(
        start=_dt(start_hour),
        end=_dt(end_hour),
        price=price,
        is_cheap=is_cheap,
    )


def _make_battery_config(
    capacity_kwh: float = 10.0,
    target_soc: float = 90.0,
    strategy: BatteryStrategy = BatteryStrategy.BATTERY_FIRST,
    allow_grid_charging: bool = False,
) -> BatteryConfig:
    """Create a BatteryConfig with sensible defaults."""
    return BatteryConfig(
        capacity_kwh=capacity_kwh,
        max_discharge_entity=None,
        max_discharge_default=None,
        target_soc=target_soc,
        target_time=time(7, 0),
        strategy=strategy,
        allow_grid_charging=allow_grid_charging,
    )


# ===========================================================================
# TestBuildTimeline
# ===========================================================================

class TestBuildTimeline:
    """Tests for Planner.build_timeline()."""

    def test_simple_merge(self):
        """Merge 1h tariff windows with 1h solar forecast -> 1:1 slots."""
        forecast = ForecastData(
            remaining_today_kwh=3.0,
            hourly_breakdown=[
                _make_hourly_forecast(10, 1000),
                _make_hourly_forecast(11, 2000),
                _make_hourly_forecast(12, 3000),
            ],
        )
        tariffs = [
            _make_tariff_window(10, 11, 0.20),
            _make_tariff_window(11, 12, 0.25),
            _make_tariff_window(12, 13, 0.30),
        ]

        planner = Planner()
        slots = planner.build_timeline(forecast, tariffs, base_load_watts=500.0)

        assert len(slots) == 3

        # First slot: 10-11, 1000W solar, excess = 1000-500 = 500W
        assert slots[0].start == _dt(10)
        assert slots[0].end == _dt(11)
        assert slots[0].expected_solar_watts == 1000.0
        assert slots[0].expected_excess_watts == 500.0
        assert slots[0].price == 0.20

        # Second slot: 11-12, 2000W solar, excess = 2000-500 = 1500W
        assert slots[1].expected_solar_watts == 2000.0
        assert slots[1].expected_excess_watts == 1500.0
        assert slots[1].price == 0.25

        # Third slot: 12-13, 3000W solar, excess = 3000-500 = 2500W
        assert slots[2].expected_solar_watts == 3000.0
        assert slots[2].expected_excess_watts == 2500.0
        assert slots[2].price == 0.30

    def test_subdivide_coarse_tariff(self):
        """Subdivide 1h tariff against 30min solar forecast."""
        forecast = ForecastData(
            remaining_today_kwh=1.5,
            hourly_breakdown=[
                _make_half_hour_forecast(10, 0, 1000),
                _make_half_hour_forecast(10, 30, 2000),
            ],
        )
        tariffs = [
            _make_tariff_window(10, 11, 0.20),
        ]

        planner = Planner()
        slots = planner.build_timeline(forecast, tariffs, base_load_watts=500.0)

        # 1h tariff at 0.20, 2x 30min forecasts -> 2 sub-slots
        assert len(slots) == 2
        assert slots[0].start == _dt(10, 0)
        assert slots[0].end == _dt(10, 30)
        assert slots[0].expected_solar_watts == 1000.0
        assert slots[0].price == 0.20

        assert slots[1].start == _dt(10, 30)
        assert slots[1].end == _dt(11, 0)
        assert slots[1].expected_solar_watts == 2000.0
        assert slots[1].price == 0.20

    def test_merge_identical_adjacent(self):
        """Merge adjacent slots with same price and solar."""
        forecast = ForecastData(
            remaining_today_kwh=4.0,
            hourly_breakdown=[
                _make_hourly_forecast(10, 1000),
                _make_hourly_forecast(11, 1000),
                _make_hourly_forecast(12, 2000),
                _make_hourly_forecast(13, 2000),
            ],
        )
        # Same price for all 4 hours - but different solar between 10-11 and 12-13
        # 10-11 and 11-12 have same solar (1000W) and same price -> should merge
        # 12-13 and 13-14 have same solar (2000W) and same price -> should merge
        tariffs = [
            _make_tariff_window(10, 11, 0.20),
            _make_tariff_window(11, 12, 0.20),
            _make_tariff_window(12, 13, 0.20),
            _make_tariff_window(13, 14, 0.20),
        ]

        planner = Planner()
        slots = planner.build_timeline(forecast, tariffs, base_load_watts=500.0)

        # Should merge into 2 slots: (10-12, 1000W, 0.20) and (12-14, 2000W, 0.20)
        assert len(slots) == 2
        assert slots[0].start == _dt(10)
        assert slots[0].end == _dt(12)
        assert slots[0].expected_solar_watts == 1000.0
        assert slots[0].price == 0.20

        assert slots[1].start == _dt(12)
        assert slots[1].end == _dt(14)
        assert slots[1].expected_solar_watts == 2000.0
        assert slots[1].price == 0.20

    def test_empty_inputs(self):
        """Handle empty forecast or empty tariff windows."""
        planner = Planner()

        # Empty forecast, non-empty tariffs
        slots = planner.build_timeline(
            ForecastData(remaining_today_kwh=0.0),
            [_make_tariff_window(10, 11, 0.20)],
        )
        # Tariff windows without matching forecasts produce slots with 0 solar
        assert len(slots) == 1
        assert slots[0].expected_solar_watts == 0.0

        # Non-empty forecast, empty tariffs
        forecast = ForecastData(
            remaining_today_kwh=1.0,
            hourly_breakdown=[_make_hourly_forecast(10, 1000)],
        )
        slots = planner.build_timeline(forecast, [])
        # No tariff windows -> synthetic windows from forecast, so slots are generated
        assert len(slots) == 1
        assert slots[0].expected_solar_watts == 1000
        assert slots[0].price == 0.0

        # Both empty
        slots = planner.build_timeline(ForecastData(remaining_today_kwh=0.0), [])
        assert len(slots) == 0

    def test_excess_calculation(self):
        """Verify expected_excess = solar - base_load, clamped at 0."""
        forecast = ForecastData(
            remaining_today_kwh=1.0,
            hourly_breakdown=[
                _make_hourly_forecast(10, 300),   # Below base load
                _make_hourly_forecast(11, 1500),  # Above base load
            ],
        )
        tariffs = [
            _make_tariff_window(10, 11, 0.20),
            _make_tariff_window(11, 12, 0.20),
        ]

        planner = Planner()
        slots = planner.build_timeline(forecast, tariffs, base_load_watts=500.0)

        # 300W solar, 500W load -> excess = 0 (clamped, not negative)
        assert slots[0].expected_excess_watts == 0.0

        # 1500W solar, 500W load -> excess = 1000W
        assert slots[1].expected_excess_watts == 1000.0

    def test_is_cheap_propagated(self):
        """The is_cheap flag from tariff windows should propagate to time slots."""
        forecast = ForecastData(
            remaining_today_kwh=2.0,
            hourly_breakdown=[
                _make_hourly_forecast(10, 1000),
                _make_hourly_forecast(11, 1000),
            ],
        )
        tariffs = [
            _make_tariff_window(10, 11, 0.05, is_cheap=True),
            _make_tariff_window(11, 12, 0.30, is_cheap=False),
        ]

        planner = Planner()
        slots = planner.build_timeline(forecast, tariffs, base_load_watts=500.0)

        assert slots[0].is_cheap is True
        assert slots[1].is_cheap is False

    def test_forecast_finer_than_tariff(self):
        """When forecast has finer granularity, subdivide tariff accordingly."""
        # 2-hour tariff window, 4x 30-min forecasts
        forecast = ForecastData(
            remaining_today_kwh=5.0,
            hourly_breakdown=[
                _make_half_hour_forecast(10, 0, 500),
                _make_half_hour_forecast(10, 30, 1000),
                _make_half_hour_forecast(11, 0, 1500),
                _make_half_hour_forecast(11, 30, 2000),
            ],
        )
        tariffs = [
            _make_tariff_window(10, 12, 0.25),
        ]

        planner = Planner()
        slots = planner.build_timeline(forecast, tariffs, base_load_watts=0.0)

        # Should produce 4 sub-slots (forecast is finer)
        assert len(slots) == 4
        assert slots[0].expected_solar_watts == 500
        assert slots[1].expected_solar_watts == 1000
        assert slots[2].expected_solar_watts == 1500
        assert slots[3].expected_solar_watts == 2000
        for s in slots:
            assert s.price == 0.25

    def test_no_merge_different_prices(self):
        """Adjacent slots with different prices should not merge even if solar is same."""
        forecast = ForecastData(
            remaining_today_kwh=2.0,
            hourly_breakdown=[
                _make_hourly_forecast(10, 1000),
                _make_hourly_forecast(11, 1000),
            ],
        )
        tariffs = [
            _make_tariff_window(10, 11, 0.20),
            _make_tariff_window(11, 12, 0.25),
        ]

        planner = Planner()
        slots = planner.build_timeline(forecast, tariffs, base_load_watts=500.0)

        assert len(slots) == 2
        assert slots[0].price == 0.20
        assert slots[1].price == 0.25


# ===========================================================================
# TestBatteryStrategy
# ===========================================================================

class TestBatteryStrategy:
    """Tests for Planner.calculate_battery_strategy()."""

    def test_battery_first_reserves_excess(self):
        """BATTERY_FIRST reserves excess for charging before appliances."""
        # 80% SoC, target 90%, 10kWh battery -> need 1kWh
        # Timeline: 2 hours, each with 1000W excess -> 2kWh total
        slots = [
            TimeSlot(
                start=_dt(10), end=_dt(11),
                expected_solar_watts=1500, expected_excess_watts=1000,
                price=0.20, is_cheap=False,
            ),
            TimeSlot(
                start=_dt(11), end=_dt(12),
                expected_solar_watts=1500, expected_excess_watts=1000,
                price=0.25, is_cheap=False,
            ),
        ]
        config = _make_battery_config(
            capacity_kwh=10.0,
            target_soc=90.0,
            strategy=BatteryStrategy.BATTERY_FIRST,
        )

        planner = Planner()
        alloc = planner.calculate_battery_strategy(slots, config, current_soc=80.0)

        assert alloc.charging_needed_kwh == pytest.approx(1.0)
        # Should reserve slots for charging
        assert len(alloc.slots_reserved) >= 1
        # Excess after battery should account for reserved energy
        total_excess_after = sum(alloc.excess_after_battery.values())
        # 2kWh total excess - 1kWh charging = 1kWh remaining
        assert total_excess_after == pytest.approx(1.0, abs=0.01)

    def test_appliance_first_uses_remaining(self):
        """APPLIANCE_FIRST charges from whatever excess remains."""
        slots = [
            TimeSlot(
                start=_dt(10), end=_dt(11),
                expected_solar_watts=1500, expected_excess_watts=1000,
                price=0.20, is_cheap=False,
            ),
            TimeSlot(
                start=_dt(11), end=_dt(12),
                expected_solar_watts=1500, expected_excess_watts=1000,
                price=0.25, is_cheap=False,
            ),
        ]
        config = _make_battery_config(
            capacity_kwh=10.0,
            target_soc=90.0,
            strategy=BatteryStrategy.APPLIANCE_FIRST,
        )

        planner = Planner()
        alloc = planner.calculate_battery_strategy(slots, config, current_soc=80.0)

        assert alloc.charging_needed_kwh == pytest.approx(1.0)
        # APPLIANCE_FIRST: no slots reserved, full excess available for appliances
        assert len(alloc.slots_reserved) == 0
        total_excess_after = sum(alloc.excess_after_battery.values())
        # Full 2kWh excess remains available
        assert total_excess_after == pytest.approx(2.0, abs=0.01)

    def test_balanced_splits(self):
        """BALANCED splits excess proportionally (50/50)."""
        slots = [
            TimeSlot(
                start=_dt(10), end=_dt(11),
                expected_solar_watts=1500, expected_excess_watts=1000,
                price=0.20, is_cheap=False,
            ),
            TimeSlot(
                start=_dt(11), end=_dt(12),
                expected_solar_watts=1500, expected_excess_watts=1000,
                price=0.25, is_cheap=False,
            ),
        ]
        config = _make_battery_config(
            capacity_kwh=10.0,
            target_soc=90.0,
            strategy=BatteryStrategy.BALANCED,
        )

        planner = Planner()
        alloc = planner.calculate_battery_strategy(slots, config, current_soc=80.0)

        assert alloc.charging_needed_kwh == pytest.approx(1.0)
        # BALANCED: excess should be split - half for battery, half for appliances
        # With 2kWh total and 1kWh needed, battery gets 50% of each slot,
        # but capped at charging_needed
        total_excess_after = sum(alloc.excess_after_battery.values())
        # Each slot has 1kWh excess. Battery gets 50% = 0.5kWh per slot = 1.0kWh total
        # which exactly meets the 1kWh need. Remaining = 2.0 - 1.0 = 1.0 kWh
        assert total_excess_after == pytest.approx(1.0, abs=0.01)

    def test_no_charging_needed(self):
        """If current SoC >= target, no charging needed."""
        slots = [
            TimeSlot(
                start=_dt(10), end=_dt(11),
                expected_solar_watts=1500, expected_excess_watts=1000,
                price=0.20, is_cheap=False,
            ),
        ]
        config = _make_battery_config(
            capacity_kwh=10.0,
            target_soc=80.0,
            strategy=BatteryStrategy.BATTERY_FIRST,
        )

        planner = Planner()
        alloc = planner.calculate_battery_strategy(slots, config, current_soc=90.0)

        assert alloc.charging_needed_kwh == 0.0
        assert len(alloc.slots_reserved) == 0
        # All excess available
        assert alloc.excess_after_battery[0] == pytest.approx(1.0)

    def test_cheap_grid_charging(self):
        """Factor in cheap tariff windows for grid charging."""
        slots = [
            TimeSlot(
                start=_dt(2), end=_dt(3),
                expected_solar_watts=0, expected_excess_watts=0,
                price=0.05, is_cheap=True,  # Cheap nighttime slot
            ),
            TimeSlot(
                start=_dt(10), end=_dt(11),
                expected_solar_watts=1500, expected_excess_watts=1000,
                price=0.25, is_cheap=False,
            ),
        ]
        config = _make_battery_config(
            capacity_kwh=10.0,
            target_soc=90.0,
            strategy=BatteryStrategy.BATTERY_FIRST,
            allow_grid_charging=True,
        )

        planner = Planner()
        alloc = planner.calculate_battery_strategy(slots, config, current_soc=80.0)

        assert alloc.charging_needed_kwh == pytest.approx(1.0)
        # With cheap grid charging, the cheap slot should be included as a reserved slot
        cheap_reserved = [s for s in alloc.slots_reserved if s.is_cheap]
        assert len(cheap_reserved) >= 1
        # The solar excess slot should not be needed since cheap grid charging covers it
        # So excess from slot 1 should be fully available
        assert alloc.excess_after_battery[1] == pytest.approx(1.0)

    def test_cheap_grid_charging_disabled(self):
        """When allow_grid_charging=False, cheap windows are not used for battery."""
        slots = [
            TimeSlot(
                start=_dt(2), end=_dt(3),
                expected_solar_watts=0, expected_excess_watts=0,
                price=0.05, is_cheap=True,
            ),
            TimeSlot(
                start=_dt(10), end=_dt(11),
                expected_solar_watts=1500, expected_excess_watts=1000,
                price=0.25, is_cheap=False,
            ),
        ]
        config = _make_battery_config(
            capacity_kwh=10.0,
            target_soc=90.0,
            strategy=BatteryStrategy.BATTERY_FIRST,
            allow_grid_charging=False,
        )

        planner = Planner()
        alloc = planner.calculate_battery_strategy(slots, config, current_soc=80.0)

        assert alloc.charging_needed_kwh == pytest.approx(1.0)
        # No cheap slots should be reserved when grid charging is disabled
        cheap_reserved = [s for s in alloc.slots_reserved if s.is_cheap]
        assert len(cheap_reserved) == 0

    def test_charging_need_exceeds_available_excess(self):
        """When charging need exceeds total available excess, reserve everything."""
        slots = [
            TimeSlot(
                start=_dt(10), end=_dt(11),
                expected_solar_watts=600, expected_excess_watts=100,
                price=0.20, is_cheap=False,
            ),
        ]
        config = _make_battery_config(
            capacity_kwh=10.0,
            target_soc=90.0,
            strategy=BatteryStrategy.BATTERY_FIRST,
        )

        planner = Planner()
        # 50% SoC, target 90%, 10kWh -> need 4kWh, but only 0.1kWh available
        alloc = planner.calculate_battery_strategy(slots, config, current_soc=50.0)

        assert alloc.charging_needed_kwh == pytest.approx(4.0)
        assert len(alloc.slots_reserved) == 1
        # All excess reserved for battery
        assert alloc.excess_after_battery[0] == pytest.approx(0.0, abs=0.01)

    def test_soc_at_100_percent(self):
        """At 100% SoC, no charging needed regardless of target."""
        slots = [
            TimeSlot(
                start=_dt(10), end=_dt(11),
                expected_solar_watts=1500, expected_excess_watts=1000,
                price=0.20, is_cheap=False,
            ),
        ]
        config = _make_battery_config(
            capacity_kwh=10.0, target_soc=100.0,
            strategy=BatteryStrategy.BATTERY_FIRST,
        )

        planner = Planner()
        alloc = planner.calculate_battery_strategy(slots, config, current_soc=100.0)

        assert alloc.charging_needed_kwh == 0.0
        assert len(alloc.slots_reserved) == 0

    def test_empty_timeline(self):
        """Handle empty timeline gracefully."""
        config = _make_battery_config()
        planner = Planner()
        alloc = planner.calculate_battery_strategy([], config, current_soc=50.0)

        assert alloc.charging_needed_kwh == pytest.approx(4.0)  # 90%-50% of 10kWh
        assert len(alloc.slots_reserved) == 0
        assert len(alloc.excess_after_battery) == 0

    def test_battery_first_prefers_cheapest_slots(self):
        """BATTERY_FIRST should prefer cheaper slots for charging."""
        slots = [
            TimeSlot(
                start=_dt(10), end=_dt(11),
                expected_solar_watts=1500, expected_excess_watts=1000,
                price=0.30, is_cheap=False,
            ),
            TimeSlot(
                start=_dt(11), end=_dt(12),
                expected_solar_watts=1500, expected_excess_watts=1000,
                price=0.10, is_cheap=False,
            ),
        ]
        config = _make_battery_config(
            capacity_kwh=10.0,
            target_soc=90.0,
            strategy=BatteryStrategy.BATTERY_FIRST,
        )

        planner = Planner()
        # Need 1kWh, 2kWh available across 2 slots
        alloc = planner.calculate_battery_strategy(slots, config, current_soc=80.0)

        assert alloc.charging_needed_kwh == pytest.approx(1.0)
        # Should prefer the cheaper slot (slot 1 at 0.10)
        assert len(alloc.slots_reserved) >= 1
        # The expensive slot should have more excess remaining
        assert alloc.excess_after_battery[0] == pytest.approx(1.0, abs=0.01)
        # The cheap slot should have less excess (used for charging)
        assert alloc.excess_after_battery[1] == pytest.approx(0.0, abs=0.01)


# ---------------------------------------------------------------------------
# Appliance helper
# ---------------------------------------------------------------------------

def _make_appliance(
    appliance_id: str = "app1",
    name: str = "Test Appliance",
    priority: int = 1,
    nominal_power: float = 1000.0,
    min_daily_runtime: timedelta | None = None,
    max_daily_runtime: timedelta | None = None,
    schedule_deadline: time | None = None,
    allow_grid_supplement: bool = False,
    max_grid_power: float | None = None,
    dynamic_current: bool = False,
    min_current: float = 6.0,
    max_current: float = 16.0,
    phases: int = 1,
    on_only: bool = False,
    cheap_price_threshold: float | None = None,
) -> ApplianceConfig:
    """Create an ApplianceConfig with sensible defaults."""
    return ApplianceConfig(
        id=appliance_id,
        name=name,
        entity_id=f"switch.{appliance_id}",
        priority=priority,
        phases=phases,
        nominal_power=nominal_power,
        actual_power_entity=None,
        dynamic_current=dynamic_current,
        current_entity=f"number.{appliance_id}_current" if dynamic_current else None,
        min_current=min_current,
        max_current=max_current,
        ev_soc_entity=None,
        ev_connected_entity=None,
        is_big_consumer=False,
        battery_max_discharge_override=None,
        on_only=on_only,
        min_daily_runtime=min_daily_runtime,
        max_daily_runtime=max_daily_runtime,
        schedule_deadline=schedule_deadline,
        switch_interval=300,
        allow_grid_supplement=allow_grid_supplement,
        max_grid_power=max_grid_power,
        cheap_price_threshold=cheap_price_threshold,
    )


def _make_tariff_info(
    current_price: float = 0.25,
    feed_in_tariff: float = 0.08,
    cheap_threshold: float = 0.10,
    battery_charge_threshold: float | None = None,
    windows: list[TariffWindow] | None = None,
) -> TariffInfo:
    """Create a TariffInfo with sensible defaults."""
    return TariffInfo(
        current_price=current_price,
        feed_in_tariff=feed_in_tariff,
        cheap_price_threshold=cheap_threshold,
        battery_charge_price_threshold=battery_charge_threshold if battery_charge_threshold is not None else cheap_threshold,
        windows=windows or [],
    )


# ===========================================================================
# TestApplianceScheduling
# ===========================================================================

class TestApplianceScheduling:
    """Tests for appliance scheduling logic."""

    def test_greedy_priority_allocation(self):
        """Highest priority gets best (excess) slots first."""
        # Two appliances, limited excess slots: 2 hours of 1kW excess = 2kWh
        # Priority 1 needs 2kWh (1kW * 2h), priority 5 needs 1kWh (1kW * 1h)
        # Priority 1 should get the excess slots, priority 5 should get cheap/remaining
        forecast = ForecastData(
            remaining_today_kwh=4.0,
            hourly_breakdown=[
                _make_hourly_forecast(10, 1500),  # 1000W excess after 500W base
                _make_hourly_forecast(11, 1500),  # 1000W excess after 500W base
                _make_hourly_forecast(12, 500),   # 0W excess
                _make_hourly_forecast(13, 500),   # 0W excess
            ],
        )
        tariffs = [
            _make_tariff_window(10, 11, 0.25),
            _make_tariff_window(11, 12, 0.25),
            _make_tariff_window(12, 13, 0.05, is_cheap=True),
            _make_tariff_window(13, 14, 0.05, is_cheap=True),
        ]
        appliances = [
            _make_appliance(
                appliance_id="high_prio",
                priority=1,
                nominal_power=1000.0,
                min_daily_runtime=timedelta(hours=2),
            ),
            _make_appliance(
                appliance_id="low_prio",
                priority=5,
                nominal_power=1000.0,
                min_daily_runtime=timedelta(hours=1),
                allow_grid_supplement=True,
            ),
        ]

        planner = Planner()
        plan = planner.create_plan(
            now=_dt(0),
            forecast=forecast,
            tariff=_make_tariff_info(windows=tariffs),
            appliances=appliances,
            battery_config=None,
            current_soc=None,
            export_limit=None,
            base_load_watts=500.0,
        )

        # Priority 1 entries should reference EXCESS_AVAILABLE reason
        high_entries = [e for e in plan.entries if e.appliance_id == "high_prio"]
        low_entries = [e for e in plan.entries if e.appliance_id == "low_prio"]

        assert len(high_entries) >= 1
        # High priority should get excess slots
        excess_reasons = [e for e in high_entries if e.reason == PlanReason.EXCESS_AVAILABLE]
        assert len(excess_reasons) >= 1

        # Low priority should NOT get excess slots (taken by high prio)
        # It should use cheap tariff slots instead
        assert len(low_entries) >= 1
        low_excess = [e for e in low_entries if e.reason == PlanReason.EXCESS_AVAILABLE]
        # Low prio might get some excess if high prio doesn't consume all,
        # but the main point is low_prio gets scheduled somewhere
        low_cheap = [e for e in low_entries if e.reason == PlanReason.CHEAP_TARIFF]
        # At least one cheap slot or remaining slot for the low-priority appliance
        assert len(low_cheap) + len(low_excess) >= 1

    def test_min_daily_runtime_scheduling(self):
        """Appliance with min_daily_runtime gets enough slots allocated."""
        # Appliance needs 4h/day, 1kW -> 4kWh total
        # Timeline has 4 hours of excess (different solar per hour to prevent merging)
        # Each slot: excess_watts * 1h / 1000 = excess_kwh
        # Slots: 2kW, 1.5kW, 2.5kW, 3kW excess -> 9kWh total (more than 4kWh needed)
        forecast = ForecastData(
            remaining_today_kwh=8.0,
            hourly_breakdown=[
                _make_hourly_forecast(10, 2500),  # 2000W excess
                _make_hourly_forecast(11, 2000),  # 1500W excess
                _make_hourly_forecast(12, 3000),  # 2500W excess
                _make_hourly_forecast(13, 3500),  # 3000W excess
            ],
        )
        tariffs = [
            _make_tariff_window(10, 11, 0.25),
            _make_tariff_window(11, 12, 0.20),
            _make_tariff_window(12, 13, 0.30),
            _make_tariff_window(13, 14, 0.15),
        ]
        appliances = [
            _make_appliance(
                appliance_id="pool_pump",
                priority=3,
                nominal_power=1000.0,
                min_daily_runtime=timedelta(hours=4),
            ),
        ]

        planner = Planner()
        plan = planner.create_plan(
            now=_dt(0),
            forecast=forecast,
            tariff=_make_tariff_info(windows=tariffs),
            appliances=appliances,
            battery_config=None,
            current_soc=None,
            export_limit=None,
            base_load_watts=500.0,
        )

        # The appliance should get 4 scheduled entries (one per slot)
        entries = [e for e in plan.entries if e.appliance_id == "pool_pump"]
        assert len(entries) >= 4

    def test_deadline_backward_scheduling(self):
        """Deadline constraint: work backwards, prefer cheapest."""
        # EV needs charge by 7am, timeline has cheap overnight + expensive morning.
        # Use different prices per slot to prevent merging so we can verify
        # the cheapest slots are preferred.
        forecast = ForecastData(
            remaining_today_kwh=0.0,
            hourly_breakdown=[],
        )
        tariffs = [
            _make_tariff_window(0, 1, 0.04, is_cheap=True),
            _make_tariff_window(1, 2, 0.05, is_cheap=True),
            _make_tariff_window(2, 3, 0.06, is_cheap=True),
            _make_tariff_window(3, 4, 0.07, is_cheap=True),
            _make_tariff_window(4, 5, 0.28),
            _make_tariff_window(5, 6, 0.30),
            _make_tariff_window(6, 7, 0.32),
        ]
        appliances = [
            _make_appliance(
                appliance_id="ev_charger",
                priority=2,
                nominal_power=3000.0,
                min_daily_runtime=timedelta(hours=3),
                schedule_deadline=time(7, 0),
                allow_grid_supplement=True,
            ),
        ]

        planner = Planner()
        plan = planner.create_plan(
            now=_dt(0),
            forecast=forecast,
            tariff=_make_tariff_info(windows=tariffs),
            appliances=appliances,
            battery_config=None,
            current_soc=None,
            export_limit=None,
            base_load_watts=500.0,
        )

        entries = [e for e in plan.entries if e.appliance_id == "ev_charger"]
        assert len(entries) >= 3  # needs 3 hours

        # The DEADLINE reason should appear (deadline is driving constraint)
        deadline_entries = [e for e in entries if e.reason == PlanReason.DEADLINE]
        assert len(deadline_entries) >= 1

        # Should prefer cheapest slots (0.04, 0.05, 0.06 before expensive ones)
        # Verify that the cheapest slots are used first
        scheduled_prices = sorted(
            e.window.price for e in entries if e.window is not None
        )
        # The first 3 entries should be the 3 cheapest prices
        assert scheduled_prices[:3] == [0.04, 0.05, 0.06]

    def test_grid_supplement_in_cheap_slots(self):
        """Appliances with grid supplement enabled use cheap tariff slots."""
        # No excess, but cheap tariff available
        forecast = ForecastData(
            remaining_today_kwh=0.0,
            hourly_breakdown=[
                _make_hourly_forecast(10, 300),  # below base load
                _make_hourly_forecast(11, 300),
            ],
        )
        tariffs = [
            _make_tariff_window(10, 11, 0.05, is_cheap=True),
            _make_tariff_window(11, 12, 0.30, is_cheap=False),
        ]
        appliances = [
            _make_appliance(
                appliance_id="heater",
                priority=3,
                nominal_power=2000.0,
                min_daily_runtime=timedelta(hours=1),
                allow_grid_supplement=True,
            ),
        ]

        planner = Planner()
        plan = planner.create_plan(
            now=_dt(0),
            forecast=forecast,
            tariff=_make_tariff_info(windows=tariffs),
            appliances=appliances,
            battery_config=None,
            current_soc=None,
            export_limit=None,
            base_load_watts=500.0,
        )

        entries = [e for e in plan.entries if e.appliance_id == "heater"]
        assert len(entries) >= 1

        # Should be scheduled in the cheap slot
        cheap_entries = [e for e in entries if e.reason == PlanReason.CHEAP_TARIFF]
        assert len(cheap_entries) >= 1


# ===========================================================================
# TestExportLimitManagement
# ===========================================================================

class TestExportLimitManagement:
    """Tests for export limit management."""

    def test_export_limit_absorbs_curtailment(self):
        """Schedule appliances into slots where forecast exceeds export limit."""
        # Setup: two appliances. First one (high priority, small) consumes some
        # excess but not the curtailed portion. Second one (lower priority,
        # no min_daily_runtime) should get EXPORT_LIMIT entries for curtailment.
        # Forecast: 5kW solar at 10am, 4kW at 11am, 1kW at 12pm
        # Export limit: 3kW. Base load: 500W.
        # At 10am: export=4500W, limit=3000W -> 1500W curtailed
        # At 11am: export=3500W, limit=3000W -> 500W curtailed
        # At 12pm: export=500W -> no curtailment
        forecast = ForecastData(
            remaining_today_kwh=5.0,
            hourly_breakdown=[
                _make_hourly_forecast(10, 5000),
                _make_hourly_forecast(11, 4000),
                _make_hourly_forecast(12, 1000),
            ],
        )
        tariffs = [
            _make_tariff_window(10, 11, 0.25),
            _make_tariff_window(11, 12, 0.25),
            _make_tariff_window(12, 13, 0.25),
        ]
        # Appliance with NO min_daily_runtime won't be scheduled by normal
        # scheduling, but should be picked up by export limit management
        appliances = [
            _make_appliance(
                appliance_id="water_heater",
                priority=5,
                nominal_power=1500.0,
            ),
        ]

        planner = Planner()
        plan = planner.create_plan(
            now=_dt(0),
            forecast=forecast,
            tariff=_make_tariff_info(windows=tariffs),
            appliances=appliances,
            battery_config=None,
            current_soc=None,
            export_limit=3000.0,
            base_load_watts=500.0,
        )

        entries = [e for e in plan.entries if e.appliance_id == "water_heater"]
        assert len(entries) >= 1

        # At least one entry should reference EXPORT_LIMIT reason
        export_entries = [e for e in entries if e.reason == PlanReason.EXPORT_LIMIT]
        assert len(export_entries) >= 1


# ===========================================================================
# TestWeatherPreplanning
# ===========================================================================

class TestWeatherPreplanning:
    """Tests for weather pre-planning."""

    def test_poor_tomorrow_extends_today(self):
        """Poor solar tomorrow -> extend today's scheduling."""
        # Today has excess across 4 different-valued slots, tomorrow very poor.
        # Appliance needs 2h minimum. With different solar values, slots won't
        # merge, leaving room for weather pre-planning to schedule extra entries.
        forecast = ForecastData(
            remaining_today_kwh=10.0,
            hourly_breakdown=[
                _make_hourly_forecast(10, 2000),  # 1500W excess
                _make_hourly_forecast(11, 3000),  # 2500W excess
                _make_hourly_forecast(12, 4000),  # 3500W excess
                _make_hourly_forecast(13, 5000),  # 4500W excess
            ],
            tomorrow_total_kwh=1.0,  # Very poor tomorrow
        )
        tariffs = [
            _make_tariff_window(10, 11, 0.25),
            _make_tariff_window(11, 12, 0.20),
            _make_tariff_window(12, 13, 0.30),
            _make_tariff_window(13, 14, 0.15),
        ]
        appliances = [
            _make_appliance(
                appliance_id="pool_pump",
                priority=3,
                nominal_power=1000.0,
                min_daily_runtime=timedelta(hours=2),
            ),
        ]

        planner = Planner()
        plan = planner.create_plan(
            now=_dt(0),
            forecast=forecast,
            tariff=_make_tariff_info(windows=tariffs),
            appliances=appliances,
            battery_config=None,
            current_soc=None,
            export_limit=None,
            base_load_watts=500.0,
        )

        entries = [e for e in plan.entries if e.appliance_id == "pool_pump"]
        # With poor tomorrow, the planner should schedule additional entries
        # beyond the minimum 2 hours
        weather_entries = [e for e in entries if e.reason == PlanReason.WEATHER_PREPLANNING]
        # The plan should contain weather pre-planning entries
        assert len(weather_entries) >= 1


# ===========================================================================
# TestCreatePlan
# ===========================================================================

class TestCreatePlan:
    """Tests for the full create_plan() method."""

    def test_full_plan_generation(self):
        """create_plan produces valid Plan with entries."""
        forecast = ForecastData(
            remaining_today_kwh=5.0,
            hourly_breakdown=[
                _make_hourly_forecast(10, 2000),
                _make_hourly_forecast(11, 3000),
                _make_hourly_forecast(12, 2500),
            ],
        )
        tariffs = [
            _make_tariff_window(10, 11, 0.25),
            _make_tariff_window(11, 12, 0.25),
            _make_tariff_window(12, 13, 0.25),
        ]
        battery_config = _make_battery_config(
            capacity_kwh=10.0,
            target_soc=90.0,
            strategy=BatteryStrategy.APPLIANCE_FIRST,
        )
        appliances = [
            _make_appliance(
                appliance_id="heater",
                priority=2,
                nominal_power=1000.0,
                min_daily_runtime=timedelta(hours=2),
            ),
        ]

        planner = Planner()
        plan = planner.create_plan(
            now=_dt(0),
            forecast=forecast,
            tariff=_make_tariff_info(windows=tariffs),
            appliances=appliances,
            battery_config=battery_config,
            current_soc=80.0,
            export_limit=None,
            base_load_watts=500.0,
        )

        assert isinstance(plan, Plan)
        assert len(plan.entries) >= 1
        assert isinstance(plan.battery_target, BatteryTarget)
        assert 0.0 <= plan.confidence <= 1.0
        assert plan.horizon > timedelta(0)

    def test_plan_without_battery(self):
        """create_plan works without battery (None config)."""
        forecast = ForecastData(
            remaining_today_kwh=3.0,
            hourly_breakdown=[
                _make_hourly_forecast(10, 2000),
                _make_hourly_forecast(11, 2000),
            ],
        )
        tariffs = [
            _make_tariff_window(10, 11, 0.25),
            _make_tariff_window(11, 12, 0.25),
        ]
        appliances = [
            _make_appliance(
                appliance_id="pump",
                priority=1,
                nominal_power=500.0,
                min_daily_runtime=timedelta(hours=1),
            ),
        ]

        planner = Planner()
        plan = planner.create_plan(
            now=_dt(0),
            forecast=forecast,
            tariff=_make_tariff_info(windows=tariffs),
            appliances=appliances,
            battery_config=None,
            current_soc=None,
            export_limit=None,
            base_load_watts=500.0,
        )

        assert isinstance(plan, Plan)
        assert len(plan.entries) >= 1
        # Battery target should use a default strategy
        assert isinstance(plan.battery_target, BatteryTarget)
        assert plan.battery_target.strategy == BatteryStrategy.APPLIANCE_FIRST

    def test_plan_without_tariff(self):
        """create_plan works with empty tariff windows."""
        forecast = ForecastData(
            remaining_today_kwh=2.0,
            hourly_breakdown=[
                _make_hourly_forecast(10, 2000),
                _make_hourly_forecast(11, 2000),
            ],
        )
        appliances = [
            _make_appliance(
                appliance_id="fan",
                priority=1,
                nominal_power=100.0,
                min_daily_runtime=timedelta(hours=1),
            ),
        ]

        planner = Planner()
        plan = planner.create_plan(
            now=_dt(0),
            forecast=forecast,
            tariff=_make_tariff_info(windows=[]),  # No tariff windows
            appliances=appliances,
            battery_config=None,
            current_soc=None,
            export_limit=None,
            base_load_watts=500.0,
        )

        assert isinstance(plan, Plan)
        # Without tariff windows, synthetic windows from forecast are used
        # so the planner can still schedule based on solar excess alone
        assert len(plan.entries) >= 1
        assert plan.entries[0].reason == PlanReason.EXCESS_AVAILABLE
        assert 0.0 <= plan.confidence <= 1.0


# ===========================================================================
# TestBatteryChargeThreshold
# ===========================================================================

class TestBatteryChargeThreshold:
    """Test that battery grid charging uses battery_charge_price_threshold, not cheap_price_threshold."""

    def test_battery_charges_at_battery_threshold(self):
        """Battery charges from grid when price <= battery_charge_price_threshold."""
        # cheap_threshold=0.10, battery_charge_threshold=0.05
        # Slot price is 0.04 (below battery threshold) -> should charge
        slots = [
            TimeSlot(
                start=_dt(2), end=_dt(3),
                expected_solar_watts=0.0, expected_excess_watts=0.0,
                price=0.04, is_cheap=True,
            ),
        ]
        battery_config = _make_battery_config(
            capacity_kwh=10.0,
            target_soc=90.0,
            strategy=BatteryStrategy.BATTERY_FIRST,
            allow_grid_charging=True,
        )
        tariff = _make_tariff_info(
            cheap_threshold=0.10,
            battery_charge_threshold=0.05,
        )

        planner = Planner()
        alloc = planner.calculate_battery_strategy(
            slots, battery_config, current_soc=50.0, tariff=tariff,
        )

        # Should have reserved the slot for grid charging
        assert len(alloc.slots_reserved) == 1

    def test_battery_rejects_slot_above_battery_threshold(self):
        """Battery does NOT charge from grid when price > battery_charge_price_threshold,
        even if price <= cheap_price_threshold (is_cheap=True)."""
        # cheap_threshold=0.10, battery_charge_threshold=0.05
        # Slot price is 0.08 (above battery threshold but below cheap threshold)
        slots = [
            TimeSlot(
                start=_dt(2), end=_dt(3),
                expected_solar_watts=0.0, expected_excess_watts=0.0,
                price=0.08, is_cheap=True,  # is_cheap=True from global!
            ),
        ]
        battery_config = _make_battery_config(
            capacity_kwh=10.0,
            target_soc=90.0,
            strategy=BatteryStrategy.BATTERY_FIRST,
            allow_grid_charging=True,
        )
        tariff = _make_tariff_info(
            cheap_threshold=0.10,
            battery_charge_threshold=0.05,
        )

        planner = Planner()
        alloc = planner.calculate_battery_strategy(
            slots, battery_config, current_soc=50.0, tariff=tariff,
        )

        # Should NOT have reserved (price above battery threshold)
        assert len(alloc.slots_reserved) == 0


# ===========================================================================
# TestPlannerPerApplianceThreshold
# ===========================================================================

class TestPlannerPerApplianceThreshold:
    """Test that planner Tier 2 scheduling uses per-appliance cheap threshold."""

    def test_per_appliance_threshold_selects_cheap_slots(self):
        """Appliance with higher per-appliance threshold uses slots the global would reject."""
        # Global threshold=0.05, per-appliance=0.15
        # Slot at price 0.10: is_cheap=False (global), but below per-appliance threshold
        slots = [
            TimeSlot(
                start=_dt(1), end=_dt(2),
                expected_solar_watts=0.0, expected_excess_watts=0.0,
                price=0.10, is_cheap=False,
            ),
        ]
        appliance = _make_appliance(
            appliance_id="ev",
            priority=1,
            nominal_power=3000.0,
            min_daily_runtime=timedelta(hours=1),
            allow_grid_supplement=True,
            max_grid_power=3000.0,
            cheap_price_threshold=0.15,
        )
        battery_alloc = BatteryAllocation(
            charging_needed_kwh=0.0,
            slots_reserved=[],
            excess_after_battery={0: 0.0},
        )

        planner = Planner()
        entries = planner.schedule_appliances(
            slots, battery_alloc, [appliance], cheap_price_threshold=0.05,
        )

        # Should schedule in the slot via Tier 2 (per-appliance threshold allows it)
        assert len(entries) >= 1
        assert entries[0].appliance_id == "ev"
        assert entries[0].reason == PlanReason.CHEAP_TARIFF

    def test_none_threshold_uses_global_in_planner(self):
        """Appliance with no per-appliance threshold falls back to global."""
        # Global threshold=0.05, slot price=0.10 (above global)
        slots = [
            TimeSlot(
                start=_dt(1), end=_dt(2),
                expected_solar_watts=0.0, expected_excess_watts=0.0,
                price=0.10, is_cheap=False,
            ),
        ]
        appliance = _make_appliance(
            appliance_id="pump",
            priority=1,
            nominal_power=1000.0,
            min_daily_runtime=timedelta(hours=1),
            allow_grid_supplement=True,
            max_grid_power=1000.0,
            cheap_price_threshold=None,
        )
        battery_alloc = BatteryAllocation(
            charging_needed_kwh=0.0,
            slots_reserved=[],
            excess_after_battery={0: 0.0},
        )

        planner = Planner()
        entries = planner.schedule_appliances(
            slots, battery_alloc, [appliance], cheap_price_threshold=0.05,
        )

        # Tier 2 should NOT schedule (price above global threshold, no per-appliance override)
        # Tier 3 (must-run) will schedule it because of min_daily_runtime
        # The entry's reason should be MIN_RUNTIME, not CHEAP_TARIFF
        assert len(entries) >= 1
        assert entries[0].reason == PlanReason.MIN_RUNTIME


# ===========================================================================
# TestCreatePlanWithCurve
# ===========================================================================

class TestCreatePlanWithCurve:
    """Tests for create_plan() wiring of plan_battery_charge_curve."""

    def test_create_plan_returns_curve_when_enabled(self):
        """create_plan populates battery_charge_curve when dynamic charge is enabled."""
        from datetime import datetime, time, timezone
        from custom_components.pv_excess_control.models import BatteryConfig, TariffInfo
        from custom_components.pv_excess_control.const import BatteryStrategy

        UTC = timezone.utc
        forecast = ForecastData(
            remaining_today_kwh=20.0,
            hourly_breakdown=[
                HourlyForecast(
                    start=datetime(2026, 5, 5, h, tzinfo=UTC),
                    end=datetime(2026, 5, 5, h + 1, tzinfo=UTC),
                    expected_kwh=4.0,
                    expected_watts=4000.0,
                )
                for h in range(8, 18)
            ],
        )
        battery = _make_battery_config(
            capacity_kwh=10.0,
            target_soc=80.0,
            strategy=BatteryStrategy.BALANCED,
        )
        tariff = _make_tariff_info()
        plan = Planner().create_plan(
            forecast=forecast,
            tariff=tariff,
            appliances=[],
            battery_config=battery,
            current_soc=30.0,
            base_load_watts=500.0,
            now=datetime(2026, 5, 5, 8, tzinfo=UTC),
            export_limit=6000,
            dynamic_battery_charge_enabled=True,
            battery_max_charge_power_w=5000,
            battery_trickle_charge_power_w=100,
        )
        assert plan.battery_charge_curve is not None

    def test_create_plan_skips_curve_when_disabled(self):
        """create_plan leaves battery_charge_curve as None when disabled."""
        from datetime import datetime, timezone
        from custom_components.pv_excess_control.const import BatteryStrategy

        UTC = timezone.utc
        forecast = ForecastData(remaining_today_kwh=0.0, hourly_breakdown=[])
        battery = _make_battery_config(
            capacity_kwh=10.0,
            target_soc=80.0,
            strategy=BatteryStrategy.BALANCED,
        )
        tariff = _make_tariff_info()
        plan = Planner().create_plan(
            forecast=forecast,
            tariff=tariff,
            appliances=[],
            battery_config=battery,
            current_soc=30.0,
            base_load_watts=500.0,
            now=datetime(2026, 5, 5, 8, tzinfo=UTC),
            export_limit=6000,
            dynamic_battery_charge_enabled=False,
        )
        assert plan.battery_charge_curve is None

    def test_create_plan_curve_omitted_when_no_battery_config(self):
        """With dynamic charge enabled but no battery_config, the curve must be None."""
        from datetime import datetime, timezone

        UTC = timezone.utc
        forecast = ForecastData(
            remaining_today_kwh=20.0,
            hourly_breakdown=[
                HourlyForecast(
                    start=datetime(2026, 5, 5, h, tzinfo=UTC),
                    end=datetime(2026, 5, 5, h + 1, tzinfo=UTC),
                    expected_kwh=4.0,
                    expected_watts=4000.0,
                )
                for h in range(8, 18)
            ],
        )
        tariff = _make_tariff_info()
        plan = Planner().create_plan(
            forecast=forecast,
            tariff=tariff,
            appliances=[],
            battery_config=None,
            current_soc=30.0,
            base_load_watts=500.0,
            now=datetime(2026, 5, 5, 8, tzinfo=UTC),
            export_limit=6000,
            dynamic_battery_charge_enabled=True,
            battery_max_charge_power_w=5000,
            battery_trickle_charge_power_w=100,
        )
        assert plan.battery_charge_curve is None
