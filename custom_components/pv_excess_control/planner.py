"""Planner for PV Excess Control - scheduling & plan generation.

Produces a timeline of time slots with expected solar production and energy prices,
calculates battery charging allocation, schedules appliances, applies weather
pre-planning and export limit management, and outputs a complete Plan.

Pure Python - no HA dependencies.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .models import (
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


_LOGGER = logging.getLogger(__name__)


class Planner:
    """Builds planning timelines, schedules appliances, and generates plans.

    The planner merges solar forecast data with tariff windows to create a
    unified timeline of time slots, each annotated with expected excess power
    and energy price. It then determines how battery charging should be
    allocated across that timeline based on the configured strategy, schedules
    appliances by priority, applies weather pre-planning and export limit
    management, and produces a complete Plan.
    """

    def __init__(self, grid_voltage: int = 230, timezone_str: str = "UTC") -> None:
        self.grid_voltage = grid_voltage
        try:
            self.tz = ZoneInfo(timezone_str)
        except Exception:
            _LOGGER.warning("Planner: unknown timezone %r, falling back to UTC", timezone_str)
            self.tz = ZoneInfo("UTC")

    # ------------------------------------------------------------------
    # Step 1: BUILD TIMELINE
    # ------------------------------------------------------------------

    def build_timeline(
        self,
        forecast: ForecastData,
        tariff_windows: list[TariffWindow],
        base_load_watts: float = 500.0,
    ) -> list[TimeSlot]:
        """Merge forecast and tariff data into planning time slots.

        Algorithm:
        1. Use tariff windows as the base time structure.
        2. For each tariff window, find overlapping forecast entries.
        3. Subdivide tariff windows against finer forecast resolution.
        4. Each resulting slot gets the tariff price and the forecast's solar watts.
        5. Calculate excess = max(solar - base_load, 0).
        6. Merge adjacent slots with identical conditions.

        Args:
            forecast: Solar forecast data with hourly breakdown.
            tariff_windows: Tariff windows with pricing information.
            base_load_watts: Estimated household base load in watts.

        Returns:
            List of TimeSlot objects covering the planning horizon.
        """
        if not tariff_windows:
            # No tariff windows available — generate synthetic hourly windows
            # from the forecast data so the planner can still schedule based
            # on solar excess alone, using a flat price.
            if not forecast.hourly_breakdown:
                return []
            tariff_windows = []
            for hf in forecast.hourly_breakdown:
                tariff_windows.append(TariffWindow(
                    start=hf.start, end=hf.end,
                    price=0.0, is_cheap=False,
                ))

        # Sort tariff windows and forecasts chronologically
        sorted_tariffs = sorted(tariff_windows, key=lambda tw: tw.start)
        sorted_forecasts = sorted(
            forecast.hourly_breakdown, key=lambda hf: hf.start
        )

        raw_slots: list[TimeSlot] = []

        for tw in sorted_tariffs:
            # Find forecast entries that overlap with this tariff window
            overlapping = self._find_overlapping_forecasts(tw, sorted_forecasts)

            if not overlapping:
                # No forecast data for this window -> slot with 0 solar
                excess = max(0.0 - base_load_watts, 0.0)
                raw_slots.append(TimeSlot(
                    start=tw.start,
                    end=tw.end,
                    expected_solar_watts=0.0,
                    expected_excess_watts=excess,
                    price=tw.price,
                    is_cheap=tw.is_cheap,
                ))
            else:
                # Subdivide: create a sub-slot for each forecast entry
                # that falls within this tariff window
                for fc in overlapping:
                    # Clip forecast to tariff window boundaries
                    slot_start = max(tw.start, fc.start)
                    slot_end = min(tw.end, fc.end)

                    if slot_start >= slot_end:
                        continue

                    excess = max(fc.expected_watts - base_load_watts, 0.0)
                    raw_slots.append(TimeSlot(
                        start=slot_start,
                        end=slot_end,
                        expected_solar_watts=fc.expected_watts,
                        expected_excess_watts=excess,
                        price=tw.price,
                        is_cheap=tw.is_cheap,
                    ))

        # Sort by start time
        raw_slots.sort(key=lambda s: s.start)

        # Merge adjacent slots with identical conditions
        return self._merge_identical_adjacent(raw_slots)

    def _find_overlapping_forecasts(
        self,
        window: TariffWindow,
        forecasts: list[HourlyForecast],
    ) -> list[HourlyForecast]:
        """Find forecast entries that overlap with a tariff window."""
        result: list[HourlyForecast] = []
        for fc in forecasts:
            # Check for overlap: fc.start < window.end AND fc.end > window.start
            if fc.start < window.end and fc.end > window.start:
                result.append(fc)
        return result

    def _merge_identical_adjacent(self, slots: list[TimeSlot]) -> list[TimeSlot]:
        """Merge adjacent slots with identical price, solar, and is_cheap.

        Two slots are considered mergeable if:
        - They are temporally adjacent (slot1.end == slot2.start)
        - They have the same price, expected_solar_watts, and is_cheap flag
        """
        if not slots:
            return []

        merged: list[TimeSlot] = [TimeSlot(
            start=slots[0].start,
            end=slots[0].end,
            expected_solar_watts=slots[0].expected_solar_watts,
            expected_excess_watts=slots[0].expected_excess_watts,
            price=slots[0].price,
            is_cheap=slots[0].is_cheap,
        )]

        for slot in slots[1:]:
            prev = merged[-1]
            if (
                prev.end == slot.start
                and prev.price == slot.price
                and prev.expected_solar_watts == slot.expected_solar_watts
                and prev.is_cheap == slot.is_cheap
            ):
                # Merge: create a new TimeSlot instead of mutating
                merged[-1] = TimeSlot(
                    start=prev.start,
                    end=slot.end,
                    expected_solar_watts=prev.expected_solar_watts,
                    expected_excess_watts=prev.expected_excess_watts,
                    price=prev.price,
                    is_cheap=prev.is_cheap,
                )
            else:
                merged.append(TimeSlot(
                    start=slot.start,
                    end=slot.end,
                    expected_solar_watts=slot.expected_solar_watts,
                    expected_excess_watts=slot.expected_excess_watts,
                    price=slot.price,
                    is_cheap=slot.is_cheap,
                ))

        return merged

    # ------------------------------------------------------------------
    # Step 2: CALCULATE BATTERY STRATEGY
    # ------------------------------------------------------------------

    def calculate_battery_strategy(
        self,
        timeline: list[TimeSlot],
        battery_config: BatteryConfig,
        current_soc: float,
        tariff: TariffInfo | None = None,
    ) -> BatteryAllocation:
        """Determine battery charging allocation across the timeline.

        Args:
            timeline: List of TimeSlot objects from build_timeline().
            battery_config: Battery configuration including capacity, target, strategy.
            current_soc: Current battery state of charge as a percentage (0-100).
            tariff: Optional tariff info; when provided, battery_charge_price_threshold
                    is used instead of slot.is_cheap for grid charging decisions.

        Returns:
            BatteryAllocation with charging needs and slot reservations.
        """
        # Calculate how much energy is needed to reach target SoC
        soc_gap = max(battery_config.target_soc - current_soc, 0.0)
        charging_needed_kwh = (soc_gap / 100.0) * battery_config.capacity_kwh

        if charging_needed_kwh <= 0.0 or not timeline:
            # No charging needed or no slots to work with
            excess_after = {
                i: self._slot_excess_kwh(slot)
                for i, slot in enumerate(timeline)
            }
            return BatteryAllocation(
                charging_needed_kwh=charging_needed_kwh,
                slots_reserved=[],
                excess_after_battery=excess_after,
            )

        strategy = battery_config.strategy

        if strategy == BatteryStrategy.APPLIANCE_FIRST:
            return self._appliance_first_strategy(
                timeline, charging_needed_kwh, battery_config
            )
        elif strategy == BatteryStrategy.BATTERY_FIRST:
            return self._battery_first_strategy(
                timeline, charging_needed_kwh, battery_config, tariff
            )
        elif strategy == BatteryStrategy.BALANCED:
            return self._balanced_strategy(
                timeline, charging_needed_kwh, battery_config
            )
        else:
            # Fallback to appliance_first
            return self._appliance_first_strategy(
                timeline, charging_needed_kwh, battery_config
            )

    def _slot_excess_kwh(self, slot: TimeSlot) -> float:
        """Calculate excess energy in kWh for a time slot."""
        duration_hours = (slot.end - slot.start).total_seconds() / 3600.0
        return max(slot.expected_excess_watts * duration_hours / 1000.0, 0.0)

    def _appliance_first_strategy(
        self,
        timeline: list[TimeSlot],
        charging_needed_kwh: float,
        battery_config: BatteryConfig,
    ) -> BatteryAllocation:
        """APPLIANCE_FIRST: charge battery from whatever excess remains.

        No slots are reserved for battery - all excess goes to appliances first.
        Battery charges from whatever is left over (handled at runtime).
        """
        excess_after = {
            i: self._slot_excess_kwh(slot)
            for i, slot in enumerate(timeline)
        }
        return BatteryAllocation(
            charging_needed_kwh=charging_needed_kwh,
            slots_reserved=[],
            excess_after_battery=excess_after,
        )

    def _battery_first_strategy(
        self,
        timeline: list[TimeSlot],
        charging_needed_kwh: float,
        battery_config: BatteryConfig,
        tariff: TariffInfo | None = None,
    ) -> BatteryAllocation:
        """BATTERY_FIRST: reserve expected excess for charging until target met.

        Prefers cheapest slots first. If allow_grid_charging is enabled,
        cheap tariff windows are considered for grid charging.
        When tariff is provided, battery_charge_price_threshold is used instead
        of slot.is_cheap for grid charging decisions.
        """
        slots_reserved: list[TimeSlot] = []
        excess_after: dict[int, float] = {}
        remaining_need = charging_needed_kwh

        # Build a list of (slot_index, slot, excess_kwh, priority_price) tuples
        # Sort by price ascending so we use cheapest energy first
        slot_candidates: list[tuple[int, TimeSlot, float]] = []

        for i, slot in enumerate(timeline):
            slot_excess = self._slot_excess_kwh(slot)

            battery_cheap = (
                slot.price <= tariff.battery_charge_price_threshold
                if tariff is not None
                else slot.is_cheap
            )

            if battery_config.allow_grid_charging and battery_cheap:
                # For cheap grid charging slots, the battery can charge from grid
                # even if there's no solar excess. Use the full slot duration
                # at some reasonable grid charging rate.
                # We mark the slot as reservable with its excess (could be 0 for nighttime)
                slot_candidates.append((i, slot, slot_excess))
            elif slot_excess > 0:
                slot_candidates.append((i, slot, slot_excess))

        # Sort by price ascending to prefer cheapest slots
        slot_candidates.sort(key=lambda x: x[1].price)

        reserved_indices: set[int] = set()
        allocated: dict[int, float] = {}  # index -> kwh allocated to battery

        for idx, slot, slot_excess in slot_candidates:
            if remaining_need <= 0:
                break

            battery_cheap = (
                slot.price <= tariff.battery_charge_price_threshold
                if tariff is not None
                else slot.is_cheap
            )

            if battery_config.allow_grid_charging and battery_cheap:
                # For cheap grid charging: reserve the slot, battery charges from grid
                # Calculate how much energy this slot can actually provide (based on duration)
                slot_hours = (slot.end - slot.start).total_seconds() / 3600
                # Assume C/4 charge rate (conservative 4-hour full charge), capped at 10 kW
                max_charge_rate_kw = min(battery_config.capacity_kwh / 4, 10.0) if battery_config.capacity_kwh > 0 else 5.0
                slot_capacity = max_charge_rate_kw * slot_hours
                can_provide = min(slot_capacity, remaining_need)
                allocate = can_provide
                allocated[idx] = allocate
                remaining_need -= allocate
                reserved_indices.add(idx)
                slots_reserved.append(slot)
            elif slot_excess > 0:
                allocate = min(slot_excess, remaining_need)
                allocated[idx] = allocate
                remaining_need -= allocate
                reserved_indices.add(idx)
                slots_reserved.append(slot)

        # Calculate excess after battery for all slots
        for i, slot in enumerate(timeline):
            slot_excess = self._slot_excess_kwh(slot)
            battery_use = allocated.get(i, 0.0)
            excess_after[i] = max(slot_excess - battery_use, 0.0)

        return BatteryAllocation(
            charging_needed_kwh=charging_needed_kwh,
            slots_reserved=slots_reserved,
            excess_after_battery=excess_after,
        )

    def _balanced_strategy(
        self,
        timeline: list[TimeSlot],
        charging_needed_kwh: float,
        battery_config: BatteryConfig,
    ) -> BatteryAllocation:
        """BALANCED: split excess proportionally between battery and appliances.

        Allocate up to 50% of each slot's excess to battery (capped by
        remaining charging need).
        """
        slots_reserved: list[TimeSlot] = []
        excess_after: dict[int, float] = {}
        remaining_need = charging_needed_kwh

        for i, slot in enumerate(timeline):
            slot_excess = self._slot_excess_kwh(slot)

            if remaining_need <= 0 or slot_excess <= 0:
                excess_after[i] = slot_excess
                continue

            # Split 50/50
            battery_share = slot_excess * 0.5
            battery_allocation = min(battery_share, remaining_need)
            remaining_need -= battery_allocation

            excess_after[i] = slot_excess - battery_allocation
            if battery_allocation > 0:
                slots_reserved.append(slot)

        return BatteryAllocation(
            charging_needed_kwh=charging_needed_kwh,
            slots_reserved=slots_reserved,
            excess_after_battery=excess_after,
        )

    # ------------------------------------------------------------------
    # Step 3: SCHEDULE APPLIANCES
    # ------------------------------------------------------------------

    def schedule_appliances(
        self,
        timeline: list[TimeSlot],
        battery_allocation: BatteryAllocation,
        appliances: list[ApplianceConfig],
        cheap_price_threshold: float = 0.0,
    ) -> list[PlanEntry]:
        """Schedule appliances using greedy allocation, highest priority first.

        For each appliance (in priority order):
        a. Calculate total energy needed (from min_daily_runtime)
        b. Find optimal windows:
           - First: slots with excess solar after battery allocation
           - Second: cheap tariff slots (if grid supplementation allowed)
           - Third: any remaining slots (for must-run appliances with min_runtime)
        c. Dynamic current appliances: plan variable current across windows
        d. Deadline constraints: work backwards from deadline, allocate cheapest first

        Args:
            timeline: List of TimeSlot objects from build_timeline().
            battery_allocation: Battery allocation from calculate_battery_strategy().
            appliances: List of appliance configurations to schedule.

        Returns:
            List of PlanEntry objects for all scheduled appliances.
        """
        entries: list[PlanEntry] = []

        # Sort appliances by priority (1 = highest priority, first)
        sorted_appliances = sorted(appliances, key=lambda a: a.priority)

        # Track remaining excess per slot (will be consumed as appliances are allocated)
        remaining_excess: dict[int, float] = dict(battery_allocation.excess_after_battery)

        for appliance in sorted_appliances:
            app_entries = self._schedule_single_appliance(
                timeline, remaining_excess, appliance, cheap_price_threshold
            )
            entries.extend(app_entries)

        return entries

    def _schedule_single_appliance(
        self,
        timeline: list[TimeSlot],
        remaining_excess: dict[int, float],
        appliance: ApplianceConfig,
        cheap_price_threshold: float = 0.0,
    ) -> list[PlanEntry]:
        """Schedule a single appliance into available slots.

        Uses a tiered approach:
        1. Excess solar slots
        2. Cheap tariff slots (if grid supplementation allowed)
        3. Any remaining slots (for must-run appliances)

        For deadline constraints, works backwards from deadline and prefers
        cheapest slots first.
        """
        # Default to 1 hour if no min_daily_runtime specified, so greedy scheduler
        # can still pick up opportunistic excess slots for this appliance.
        min_daily_runtime_seconds = (
            appliance.min_daily_runtime.total_seconds()
            if appliance.min_daily_runtime is not None
            else 3600.0
        )

        # Calculate energy needed in kWh
        energy_needed_kwh = (
            appliance.nominal_power
            * min_daily_runtime_seconds
            / 3600.0
            / 1000.0
        )

        if energy_needed_kwh <= 0:
            return []

        # Calculate how many slot-hours this appliance consumes per hour
        power_kwh_per_hour = appliance.nominal_power / 1000.0

        # Handle deadline-constrained scheduling
        if appliance.schedule_deadline is not None:
            return self._schedule_with_deadline(
                timeline, remaining_excess, appliance, energy_needed_kwh, power_kwh_per_hour
            )

        # Standard greedy scheduling (no deadline)
        return self._schedule_greedy(
            timeline, remaining_excess, appliance, energy_needed_kwh, power_kwh_per_hour,
            cheap_price_threshold,
        )

    def _schedule_greedy(
        self,
        timeline: list[TimeSlot],
        remaining_excess: dict[int, float],
        appliance: ApplianceConfig,
        energy_needed_kwh: float,
        power_kwh_per_hour: float,
        cheap_price_threshold: float = 0.0,
    ) -> list[PlanEntry]:
        """Greedy scheduling: excess slots first, then cheap, then any remaining."""
        entries: list[PlanEntry] = []
        remaining_energy = energy_needed_kwh

        # Tier 1: Slots with excess solar
        excess_slots = [
            (i, slot) for i, slot in enumerate(timeline)
            if remaining_excess.get(i, 0.0) > 0
        ]
        # Sort excess slots by most excess first for best utilization
        excess_slots.sort(key=lambda x: remaining_excess.get(x[0], 0.0), reverse=True)

        for idx, slot in excess_slots:
            if remaining_energy <= 0:
                break
            pre_excess = remaining_excess.get(idx, 0.0)
            consumed = self._allocate_slot(
                slot, idx, remaining_excess, appliance, remaining_energy, power_kwh_per_hour
            )
            if consumed > 0:
                target_current = self._calculate_target_current(appliance, slot, pre_excess)
                entries.append(PlanEntry(
                    appliance_id=appliance.id,
                    action=Action.SET_CURRENT if appliance.dynamic_current else Action.ON,
                    target_current=target_current if appliance.dynamic_current else None,
                    window=TariffWindow(
                        start=slot.start, end=slot.end,
                        price=slot.price, is_cheap=slot.is_cheap,
                    ),
                    reason=PlanReason.EXCESS_AVAILABLE,
                    priority=appliance.priority,
                ))
                remaining_energy -= consumed

        # Tier 2: Cheap tariff slots (if grid supplementation allowed)
        if remaining_energy > 0 and appliance.allow_grid_supplement:
            effective_threshold = (
                appliance.cheap_price_threshold
                if appliance.cheap_price_threshold is not None
                else cheap_price_threshold
            )
            cheap_slots = [
                (i, slot) for i, slot in enumerate(timeline)
                if slot.price <= effective_threshold
            ]
            # Sort cheap slots by price ascending
            cheap_slots.sort(key=lambda x: x[1].price)

            for idx, slot in cheap_slots:
                if remaining_energy <= 0:
                    break
                # Check if we already scheduled this slot in tier 1
                already_scheduled = any(
                    e.window is not None
                    and e.window.start == slot.start
                    and e.window.end == slot.end
                    and e.appliance_id == appliance.id
                    for e in entries
                )
                if already_scheduled:
                    continue

                duration_hours = (slot.end - slot.start).total_seconds() / 3600.0
                slot_excess = remaining_excess.get(idx, 0.0)
                consumed = min(power_kwh_per_hour * duration_hours, remaining_energy)
                if consumed > 0:
                    target_current = self._calculate_target_current(appliance, slot, slot_excess)
                    entries.append(PlanEntry(
                        appliance_id=appliance.id,
                        action=Action.SET_CURRENT if appliance.dynamic_current else Action.ON,
                        target_current=target_current if appliance.dynamic_current else None,
                        window=TariffWindow(
                            start=slot.start, end=slot.end,
                            price=slot.price, is_cheap=slot.is_cheap,
                        ),
                        reason=PlanReason.CHEAP_TARIFF,
                        priority=appliance.priority,
                    ))
                    remaining_energy -= consumed
                    # Deduct solar portion from remaining excess (grid portion doesn't consume solar)
                    if slot_excess > 0:
                        solar_consumed = min(slot_excess, consumed)
                        remaining_excess[idx] = slot_excess - solar_consumed

        # Tier 3: Any remaining slots (for must-run appliances that still need runtime)
        if remaining_energy > 0 and appliance.min_daily_runtime is not None:
            all_slots = [
                (i, slot) for i, slot in enumerate(timeline)
            ]
            # Sort by price ascending
            all_slots.sort(key=lambda x: x[1].price)

            for idx, slot in all_slots:
                if remaining_energy <= 0:
                    break
                # Skip if already scheduled
                already_scheduled = any(
                    e.window is not None
                    and e.window.start == slot.start
                    and e.window.end == slot.end
                    and e.appliance_id == appliance.id
                    for e in entries
                )
                if already_scheduled:
                    continue

                duration_hours = (slot.end - slot.start).total_seconds() / 3600.0
                slot_excess = remaining_excess.get(idx, 0.0)
                consumed = min(power_kwh_per_hour * duration_hours, remaining_energy)
                if consumed > 0:
                    target_current = self._calculate_target_current(appliance, slot, slot_excess)
                    entries.append(PlanEntry(
                        appliance_id=appliance.id,
                        action=Action.SET_CURRENT if appliance.dynamic_current else Action.ON,
                        target_current=target_current if appliance.dynamic_current else None,
                        window=TariffWindow(
                            start=slot.start, end=slot.end,
                            price=slot.price, is_cheap=slot.is_cheap,
                        ),
                        reason=PlanReason.MIN_RUNTIME,
                        priority=appliance.priority,
                    ))
                    remaining_energy -= consumed
                    # Deduct solar portion from remaining excess (grid portion doesn't consume solar)
                    if slot_excess > 0:
                        solar_consumed = min(slot_excess, consumed)
                        remaining_excess[idx] = slot_excess - solar_consumed

        return entries

    def _schedule_with_deadline(
        self,
        timeline: list[TimeSlot],
        remaining_excess: dict[int, float],
        appliance: ApplianceConfig,
        energy_needed_kwh: float,
        power_kwh_per_hour: float,
    ) -> list[PlanEntry]:
        """Schedule with deadline constraint: work backwards, prefer cheapest.

        Filters slots to only those before the deadline, then sorts by price
        ascending to allocate cheapest slots first.
        """
        entries: list[PlanEntry] = []
        remaining_energy = energy_needed_kwh
        deadline = appliance.schedule_deadline
        assert deadline is not None

        # Filter slots to those whose START is before the deadline
        eligible_slots: list[tuple[int, TimeSlot]] = []
        # A deadline before noon suggests overnight scheduling (e.g., "EV by 7am")
        is_overnight = deadline < time(12, 0)
        for i, slot in enumerate(timeline):
            slot_start_time = slot.start.astimezone(self.tz).time() if hasattr(slot.start, 'astimezone') else slot.start.time() if hasattr(slot.start, 'time') else slot.start
            if is_overnight:
                # For overnight: include slots starting before deadline OR after noon (afternoon/evening/night)
                if slot_start_time < deadline or slot_start_time >= time(12, 0):
                    eligible_slots.append((i, slot))
            else:
                # For daytime deadlines: include slots starting before deadline
                if slot_start_time < deadline:
                    eligible_slots.append((i, slot))

        if not eligible_slots:
            _LOGGER.warning(
                "No eligible slots before deadline %s for appliance %s "
                "(deadline may be past)",
                deadline, appliance.name,
            )
            return []

        # Sort eligible slots by price ascending (cheapest first)
        eligible_slots.sort(key=lambda x: x[1].price)

        for idx, slot in eligible_slots:
            if remaining_energy <= 0:
                break

            # Skip if we already scheduled this appliance in this slot
            already_scheduled = any(
                e.appliance_id == appliance.id
                and e.window is not None
                and e.window.start == slot.start
                and e.window.end == slot.end
                for e in entries
            )
            if already_scheduled:
                continue

            duration_hours = (slot.end - slot.start).total_seconds() / 3600.0
            slot_excess = remaining_excess.get(idx, 0.0)

            # For deadline-constrained scheduling, we always use DEADLINE reason
            # since the deadline is the driving constraint for the scheduling.
            reason = PlanReason.DEADLINE
            if slot_excess > 0:
                consumed = self._allocate_slot(
                    slot, idx, remaining_excess, appliance, remaining_energy, power_kwh_per_hour
                )
            else:
                consumed = min(power_kwh_per_hour * duration_hours, remaining_energy)

            if consumed > 0:
                target_current = self._calculate_target_current(appliance, slot, slot_excess)
                entries.append(PlanEntry(
                    appliance_id=appliance.id,
                    action=Action.SET_CURRENT if appliance.dynamic_current else Action.ON,
                    target_current=target_current if appliance.dynamic_current else None,
                    window=TariffWindow(
                        start=slot.start, end=slot.end,
                        price=slot.price, is_cheap=slot.is_cheap,
                    ),
                    reason=reason,
                    priority=appliance.priority,
                ))
                remaining_energy -= consumed

        return entries

    def _allocate_slot(
        self,
        slot: TimeSlot,
        slot_index: int,
        remaining_excess: dict[int, float],
        appliance: ApplianceConfig,
        energy_needed: float,
        power_kwh_per_hour: float,
    ) -> float:
        """Allocate appliance power from a slot's excess, updating remaining_excess.

        Returns the energy consumed in kWh.
        """
        duration_hours = (slot.end - slot.start).total_seconds() / 3600.0
        slot_excess = remaining_excess.get(slot_index, 0.0)

        # How much can the appliance consume in this slot?
        max_consumption = power_kwh_per_hour * duration_hours
        # How much is available from excess?
        available = min(slot_excess, max_consumption)
        # How much do we actually need?
        consumed = min(available, energy_needed)

        if consumed > 0:
            remaining_excess[slot_index] = slot_excess - consumed

        return consumed

    def _calculate_target_current(
        self,
        appliance: ApplianceConfig,
        slot: TimeSlot,
        available_excess_kwh: float,
    ) -> float | None:
        """Calculate target current for dynamic current appliances.

        For dynamic current appliances, determines the optimal current based
        on available excess power.
        """
        if not appliance.dynamic_current:
            return None

        # Calculate available power in watts from excess
        duration_hours = (slot.end - slot.start).total_seconds() / 3600.0
        if duration_hours > 0:
            available_watts = (available_excess_kwh / duration_hours) * 1000.0
        else:
            available_watts = 0.0

        # Calculate current: P = V * I * phases
        if self.grid_voltage > 0 and appliance.phases > 0:
            current = available_watts / (self.grid_voltage * appliance.phases)
        else:
            current = appliance.min_current

        # Clamp to min/max current
        current = max(appliance.min_current, min(current, appliance.max_current))

        return current

    # ------------------------------------------------------------------
    # Step 4: WEATHER PRE-PLANNING
    # ------------------------------------------------------------------

    def apply_weather_preplanning(
        self,
        entries: list[PlanEntry],
        timeline: list[TimeSlot],
        remaining_excess: dict[int, float],
        appliances: list[ApplianceConfig],
        forecast: ForecastData,
    ) -> list[PlanEntry]:
        """Apply weather-based pre-planning adjustments.

        Poor solar forecast for tomorrow:
        - Extend today's runtime for pre-runnable appliances by scheduling
          additional slots during today's excess

        Excellent solar forecast for tomorrow:
        - Reduce today's optional runtime (no action needed - just don't
          schedule extra)

        Args:
            entries: Current list of plan entries.
            timeline: Planning timeline.
            remaining_excess: Remaining excess after current scheduling.
            appliances: Appliance configurations.
            forecast: Forecast data including tomorrow's total.

        Returns:
            Updated list of plan entries with weather adjustments.
        """
        if forecast.tomorrow_total_kwh is None:
            return entries

        # Determine if tomorrow is "poor" relative to today
        # Poor = tomorrow produces less than 50% of today's remaining potential
        today_total = forecast.remaining_today_kwh
        tomorrow_total = forecast.tomorrow_total_kwh

        if today_total <= 0:
            return entries

        ratio = tomorrow_total / today_total

        if ratio >= 0.5:
            # Tomorrow is decent or better - no pre-planning needed
            return entries

        # Tomorrow is poor (< 50% of today) - extend today's runtime
        additional_entries: list[PlanEntry] = []
        sorted_appliances = sorted(appliances, key=lambda a: a.priority)

        for appliance in sorted_appliances:
            if appliance.min_daily_runtime is None:
                continue

            # Check if this appliance already has enough entries
            app_entries = [e for e in entries if e.appliance_id == appliance.id]
            current_slots = len(app_entries)

            # Calculate how many additional slots to add based on how poor
            # tomorrow is. More poor = more additional slots.
            # Add up to 50% more runtime when tomorrow is very poor
            extra_factor = max(0.0, 1.0 - ratio * 2)  # 1.0 at ratio=0, 0.0 at ratio=0.5
            min_hours = appliance.min_daily_runtime.total_seconds() / 3600.0
            extra_hours = min_hours * extra_factor * 0.5  # up to 50% extra

            if extra_hours < 0.5:
                continue  # Not enough to justify an extra slot

            power_kwh_per_hour = appliance.nominal_power / 1000.0
            extra_energy_needed = power_kwh_per_hour * extra_hours

            # Find excess slots not already scheduled for this appliance
            scheduled_windows = {
                (e.window.start, e.window.end)
                for e in app_entries
                if e.window is not None
            }

            for i, slot in enumerate(timeline):
                if extra_energy_needed <= 0:
                    break

                slot_key = (slot.start, slot.end)
                if slot_key in scheduled_windows:
                    continue

                slot_excess = remaining_excess.get(i, 0.0)
                if slot_excess <= 0:
                    continue

                duration_hours = (slot.end - slot.start).total_seconds() / 3600.0
                consumed = min(
                    power_kwh_per_hour * duration_hours,
                    slot_excess,
                    extra_energy_needed,
                )

                if consumed > 0:
                    remaining_excess[i] = slot_excess - consumed
                    target_current = self._calculate_target_current(appliance, slot, slot_excess)
                    additional_entries.append(PlanEntry(
                        appliance_id=appliance.id,
                        action=Action.SET_CURRENT if appliance.dynamic_current else Action.ON,
                        target_current=target_current if appliance.dynamic_current else None,
                        window=TariffWindow(
                            start=slot.start, end=slot.end,
                            price=slot.price, is_cheap=slot.is_cheap,
                        ),
                        reason=PlanReason.WEATHER_PREPLANNING,
                        priority=appliance.priority,
                    ))
                    extra_energy_needed -= consumed

        return entries + additional_entries

    # ------------------------------------------------------------------
    # Step 5: EXPORT LIMIT MANAGEMENT
    # ------------------------------------------------------------------

    def apply_export_limit(
        self,
        entries: list[PlanEntry],
        timeline: list[TimeSlot],
        remaining_excess: dict[int, float],
        appliances: list[ApplianceConfig],
        export_limit: float | None,
        base_load_watts: float,
    ) -> list[PlanEntry]:
        """Apply export limit management.

        If a feed-in cap is configured, identifies slots where forecast exceeds
        the cap and schedules appliances into those slots to absorb curtailed
        power. These are "free energy" slots with lower activation thresholds.

        Args:
            entries: Current list of plan entries.
            timeline: Planning timeline.
            remaining_excess: Remaining excess after current scheduling.
            appliances: Appliance configurations.
            export_limit: Feed-in limit in watts, or None if not configured.
            base_load_watts: Household base load in watts.

        Returns:
            Updated list of plan entries with export limit adjustments.
        """
        if export_limit is None or export_limit <= 0:
            return entries

        # Find slots where expected solar minus base load exceeds export limit
        # This means power would be curtailed
        curtailment_slots: list[tuple[int, TimeSlot, float]] = []
        for i, slot in enumerate(timeline):
            # Excess that would be exported
            export_power = slot.expected_solar_watts - base_load_watts
            if export_power > export_limit:
                curtailed_watts = export_power - export_limit
                curtailment_slots.append((i, slot, curtailed_watts))

        if not curtailment_slots:
            return entries

        # Sort appliances by priority for scheduling into curtailment slots
        sorted_appliances = sorted(appliances, key=lambda a: a.priority)
        additional_entries: list[PlanEntry] = []

        for appliance in sorted_appliances:
            power_kwh_per_hour = appliance.nominal_power / 1000.0

            # Find curtailment slots not already scheduled for this appliance
            scheduled_windows = {
                (e.window.start, e.window.end)
                for e in entries + additional_entries
                if e.appliance_id == appliance.id and e.window is not None
            }

            for idx, slot, curtailed_watts in curtailment_slots:
                slot_key = (slot.start, slot.end)
                if slot_key in scheduled_windows:
                    continue

                # Only schedule if the appliance can absorb some of the curtailed power
                if appliance.nominal_power > curtailed_watts * 3:
                    # Appliance is too big for the curtailed amount (would need 3x grid)
                    continue

                duration_hours = (slot.end - slot.start).total_seconds() / 3600.0
                consumed = power_kwh_per_hour * duration_hours

                if consumed > 0:
                    target_current = self._calculate_target_current(
                        appliance, slot, curtailed_watts * duration_hours / 1000.0
                    )
                    additional_entries.append(PlanEntry(
                        appliance_id=appliance.id,
                        action=Action.SET_CURRENT if appliance.dynamic_current else Action.ON,
                        target_current=target_current if appliance.dynamic_current else None,
                        window=TariffWindow(
                            start=slot.start, end=slot.end,
                            price=slot.price, is_cheap=slot.is_cheap,
                        ),
                        reason=PlanReason.EXPORT_LIMIT,
                        priority=appliance.priority,
                    ))
                    # Update remaining excess
                    slot_excess = remaining_excess.get(idx, 0.0)
                    remaining_excess[idx] = max(slot_excess - consumed, 0.0)

        return entries + additional_entries

    # ------------------------------------------------------------------
    # Full plan creation
    # ------------------------------------------------------------------

    def create_plan(
        self,
        forecast: ForecastData,
        tariff: TariffInfo,
        appliances: list[ApplianceConfig],
        battery_config: BatteryConfig | None,
        current_soc: float | None,
        export_limit: float | None,
        base_load_watts: float = 500.0,
    ) -> Plan:
        """Create a complete plan for the next 24 hours.

        Orchestrates all planning steps:
        1. Build timeline from forecast and tariff data
        2. Calculate battery strategy (if battery present)
        3. Schedule appliances (greedy, priority-ordered)
        4. Apply weather pre-planning
        5. Apply export limit management
        6. Return Plan with entries, battery target, and confidence

        Args:
            forecast: Solar forecast data.
            tariff: Tariff information with pricing windows.
            appliances: List of appliance configurations.
            battery_config: Battery configuration, or None if no battery.
            current_soc: Current battery SoC (0-100), or None if no battery.
            export_limit: Feed-in limit in watts, or None.
            base_load_watts: Household base load in watts.

        Returns:
            A complete Plan for the planning horizon.
        """
        now = datetime.now(self.tz)

        # 1. Build timeline
        timeline = self.build_timeline(forecast, tariff.windows, base_load_watts)

        # 2. Calculate battery strategy
        if battery_config is not None and current_soc is not None:
            battery_allocation = self.calculate_battery_strategy(
                timeline, battery_config, current_soc, tariff=tariff
            )
            battery_target = BatteryTarget(
                target_soc=battery_config.target_soc,
                target_time=datetime.combine(
                    now.date(), battery_config.target_time, tzinfo=self.tz
                ),
                strategy=battery_config.strategy,
            )
        else:
            # No battery: all excess is available for appliances
            battery_allocation = BatteryAllocation(
                charging_needed_kwh=0.0,
                slots_reserved=[],
                excess_after_battery={
                    i: self._slot_excess_kwh(slot)
                    for i, slot in enumerate(timeline)
                },
            )
            battery_target = BatteryTarget(
                target_soc=0.0,
                target_time=now,
                strategy=BatteryStrategy.APPLIANCE_FIRST,
            )

        # 3. Schedule appliances
        entries = self.schedule_appliances(
            timeline, battery_allocation, appliances,
            cheap_price_threshold=tariff.cheap_price_threshold,
        )

        # Rebuild remaining excess from battery allocation and subtract scheduled
        remaining_excess = dict(battery_allocation.excess_after_battery)
        self._deduct_scheduled_entries(entries, timeline, remaining_excess, appliances)

        # 4. Apply weather pre-planning
        entries = self.apply_weather_preplanning(
            entries, timeline, remaining_excess, appliances, forecast
        )

        # 5. Apply export limit management
        entries = self.apply_export_limit(
            entries, timeline, remaining_excess, appliances, export_limit, base_load_watts
        )

        # 6. Calculate confidence and build plan

        # Check whether solar forecast alone covers the battery need, without
        # any grid charging. If not, grid charging is recommended. This flag
        # lets the coordinator avoid unnecessary grid imports on sunny days.
        grid_charge_recommended = False
        if battery_config is not None and battery_config.allow_grid_charging:
            solar_only_remaining = battery_allocation.charging_needed_kwh
            for slot in timeline:
                if solar_only_remaining <= 0:
                    break
                solar_only_remaining -= max(self._slot_excess_kwh(slot), 0.0)
            grid_charge_recommended = solar_only_remaining > 0.1  # 100 Wh buffer avoids noise
        confidence = self._calculate_confidence(forecast, timeline, entries)

        # Calculate horizon
        if timeline:
            horizon = timeline[-1].end - now
            if horizon < timedelta(0):
                horizon = timedelta(hours=1)
        else:
            horizon = timedelta(hours=24)

        plan = Plan(
            created_at=now,
            horizon=horizon,
            entries=entries,
            battery_target=battery_target,
            confidence=confidence,
            grid_charge_recommended=grid_charge_recommended,
        )
        _LOGGER.debug(
            "Planner: %d timeline slots, %d plan entries, confidence=%.1f%%",
            len(timeline), len(plan.entries), plan.confidence * 100,
        )
        return plan

    def _deduct_scheduled_entries(
        self,
        entries: list[PlanEntry],
        timeline: list[TimeSlot],
        remaining_excess: dict[int, float],
        appliances: list[ApplianceConfig],
    ) -> None:
        """Deduct energy consumed by scheduled entries from remaining excess."""
        # Build a lookup for appliance power
        app_power: dict[str, float] = {a.id: a.nominal_power for a in appliances}

        # Deduct scheduled entries, but only from slots that have solar excess.
        # Grid-only slots (remaining_excess == 0) don't consume solar, so
        # deducting from them would incorrectly drive remaining_excess negative
        # (clamped to 0) and misrepresent available solar for later steps.
        for entry in entries:
            if entry.window is None:
                continue
            for i, slot in enumerate(timeline):
                if slot.start == entry.window.start and slot.end == entry.window.end:
                    current = remaining_excess.get(i, 0.0)
                    if current > 0:
                        duration_hours = (slot.end - slot.start).total_seconds() / 3600.0
                        power = app_power.get(entry.appliance_id, 0.0)
                        consumed_kwh = power / 1000.0 * duration_hours
                        remaining_excess[i] = max(current - consumed_kwh, 0.0)
                    break

    def _calculate_confidence(
        self,
        forecast: ForecastData,
        timeline: list[TimeSlot],
        entries: list[PlanEntry],
    ) -> float:
        """Calculate plan confidence score (0.0-1.0).

        Confidence is based on:
        - How much of the plan relies on forecast data vs known data
        - Availability of forecast data (tomorrow forecast vs only today)
        - Number of slots with useful forecast data
        """
        if not entries:
            return 0.5  # Neutral confidence when nothing is scheduled

        # Base confidence
        confidence = 0.6

        # Boost if we have tomorrow's forecast
        if forecast.tomorrow_total_kwh is not None:
            confidence += 0.1

        # Boost if we have hourly breakdown data
        if forecast.hourly_breakdown:
            coverage = len(forecast.hourly_breakdown) / max(len(timeline), 1)
            confidence += min(coverage, 1.0) * 0.2

        # Reduce if many entries depend on uncertain sources
        excess_entries = sum(1 for e in entries if e.reason == PlanReason.EXCESS_AVAILABLE)
        total_entries = len(entries)
        if total_entries > 0:
            forecast_dependency = excess_entries / total_entries
            # Higher forecast dependency = lower confidence (forecasts are uncertain)
            confidence -= forecast_dependency * 0.1

        return max(0.0, min(1.0, confidence))
