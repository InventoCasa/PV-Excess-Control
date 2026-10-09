"""Forecast-aware grid charging with explicit AC/storage energy balances.

This module has no Home Assistant dependencies. A bounded search compares each
schedule with native self consumption. The search groups nearby storage states;
every candidate retains its exact energy, so discretization never creates energy.
The result is an approximate economic schedule, not a guarantee about forecasts.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math

from .models import HourlyForecast, TariffWindow


_EPS = 1e-8


@dataclass(frozen=True)
class BatteryPlanningConfig:
    """Physical limits and prices, expressed in kWh, W, percent and EUR/kWh.

    ``max_charge_power_w`` limits additional grid purchases. The optional
    ``max_pv_charge_power_w`` is the native/shared battery charge limit for all
    sources; when omitted, the same limit applies to both for compatibility.
    """

    capacity_kwh: float
    reserve_soc: float
    grid_target_soc: float
    max_charge_power_w: float
    max_discharge_power_w: float
    roundtrip_efficiency: float = 0.85
    wear_cost_per_kwh: float = 0.0
    charge_price_limit: float = 0.0
    hold_supported: bool = False
    horizon_hours: float = 24.0
    energy_step_kwh: float = 0.1
    max_pv_charge_power_w: float | None = None

    @property
    def native_charge_power_w(self) -> float:
        """Maximum combined PV and grid input to the battery."""
        return self.max_charge_power_w if self.max_pv_charge_power_w is None else self.max_pv_charge_power_w


@dataclass(frozen=True)
class BatteryGridSlot:
    """A UTC interval; charge and discharge energies are measured on the AC side."""

    start: datetime
    end: datetime
    price: float
    pv_kwh: float
    load_kwh: float
    grid_charge_kwh: float
    pv_charge_kwh: float
    discharge_kwh: float
    soc_end: float
    action: str

    @property
    def charge_power_w(self) -> float:
        """Mean requested grid charging power for this interval."""
        return self.grid_charge_kwh * 3_600_000 / (self.end - self.start).total_seconds()

    @property
    def battery_charge_power_w(self) -> float:
        """Total battery input power, including PV, for a forced-charge command."""
        return (self.grid_charge_kwh + self.pv_charge_kwh) * 3_600_000 / (self.end - self.start).total_seconds()


@dataclass(frozen=True)
class BatteryGridPlan:
    slots: tuple[BatteryGridSlot, ...]
    grid_energy_kwh: float
    estimated_savings: float
    reason: str
    grid_target_soc: float
    valid: bool = True


@dataclass(frozen=True)
class _Interval:
    start: datetime
    end: datetime
    price: float
    pv: float
    load: float

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600


@dataclass(frozen=True)
class _Node:
    energy: float
    cost: float
    grid_energy: float
    previous: _Node | None
    slot: BatteryGridSlot | None


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone_required")
    return value.astimezone(timezone.utc)


def _validate_config(config: BatteryPlanningConfig, soc: float | None) -> None:
    if not _finite(soc) or not 0 <= soc <= 100:
        raise ValueError("invalid_soc")
    values = (
        config.capacity_kwh, config.reserve_soc, config.grid_target_soc,
        config.max_charge_power_w, config.max_discharge_power_w,
        config.roundtrip_efficiency, config.wear_cost_per_kwh,
        config.charge_price_limit, config.horizon_hours, config.energy_step_kwh,
        config.native_charge_power_w,
    )
    if not all(_finite(value) for value in values):
        raise ValueError("invalid_configuration")
    if not (
        config.capacity_kwh > 0
        and 0 <= config.reserve_soc <= config.grid_target_soc <= 100
        and config.max_charge_power_w > 0
        and config.native_charge_power_w > 0
        and config.max_discharge_power_w > 0
        and 0 < config.roundtrip_efficiency <= 1
        and config.wear_cost_per_kwh >= 0
        and 0 < config.horizon_hours <= 48
        and config.energy_step_kwh > 0
    ):
        raise ValueError("invalid_configuration")


def _timeline(
    now: datetime,
    tariffs: list[TariffWindow],
    forecast: list[HourlyForecast],
    load_w: list[float],
    horizon_hours: float,
) -> list[_Interval]:
    start = _utc(now)
    if len(load_w) != 24 or not all(_finite(value) and value >= 0 for value in load_w):
        raise ValueError("invalid_load_profile")
    prices = sorted(((_utc(row.start), _utc(row.end), row.price) for row in tariffs), key=lambda row: row[0])
    solar = sorted(((_utc(row.start), _utc(row.end), row.expected_kwh) for row in forecast), key=lambda row: row[0])
    if not prices or not solar:
        raise ValueError("missing_forecast_or_tariff")
    end = min(start + timedelta(hours=horizon_hours), max(row[1] for row in prices), max(row[1] for row in solar))
    if end <= start:
        raise ValueError("missing_future_data")
    boundaries = {start, end}
    normalized = []
    for rows, kind in ((prices, "tariff"), (solar, "forecast")):
        relevant = [row for row in rows if row[1] > start and row[0] < end]
        cursor = start
        for row_start, row_end, value in relevant:
            if row_end <= row_start or not _finite(value) or (kind == "forecast" and value < 0):
                raise ValueError(f"invalid_{kind}")
            clipped_start, clipped_end = max(start, row_start), min(end, row_end)
            if clipped_start != cursor:
                raise ValueError(f"{kind}_gap_or_overlap")
            cursor = clipped_end
            boundaries.update((clipped_start, clipped_end))
        if cursor != end:
            raise ValueError(f"missing_{kind}_coverage")
        normalized.append(relevant)
    # Step through absolute hours: autumn's repeated hour occurs twice and
    # spring's nonexistent hour is never invented. Local profiles retain their
    # timezone's hour labels, including half-hour offsets from UTC.
    hour = _utc(now.replace(minute=0, second=0, microsecond=0)) + timedelta(hours=1)
    while hour < end:
        if hour > start:
            boundaries.add(hour)
        hour += timedelta(hours=1)
    points = sorted(boundaries)
    if len(points) > 289:
        raise ValueError("too_many_intervals")
    prices, solar = normalized
    pi = si = 0
    result = []
    for left, right in zip(points, points[1:]):
        while prices[pi][1] <= left:
            pi += 1
        while solar[si][1] <= left:
            si += 1
        duration = (right - left).total_seconds()
        solar_duration = (solar[si][1] - solar[si][0]).total_seconds()
        result.append(_Interval(
            left, right, prices[pi][2], solar[si][2] * duration / solar_duration,
            load_w[left.astimezone(now.tzinfo).hour] * duration / 3_600_000,
        ))
    return result


def _transition(
    interval: _Interval,
    node: _Node,
    config: BatteryPlanningConfig,
    efficiency: float,
    action: str,
    grid_charge: float = 0.0,
) -> tuple[_Node, float]:
    """Apply a physical interval and return its stored solar energy as well."""
    capacity = config.capacity_kwh
    reserve = capacity * config.reserve_soc / 100
    surplus = max(0.0, interval.pv - interval.load)
    deficit = max(0.0, interval.load - interval.pv)
    solar_stored = min(surplus, config.native_charge_power_w * interval.hours / 1000, max(0.0, capacity - node.energy) / efficiency) * efficiency
    energy = node.energy + solar_stored + grid_charge * efficiency
    discharge = 0.0
    if action == "self_consumption":
        discharge = min(deficit, config.max_discharge_power_w * interval.hours / 1000, max(0.0, energy - reserve) * efficiency)
        energy -= discharge / efficiency
    import_kwh = deficit - discharge + grid_charge
    cost = import_kwh * interval.price + discharge * config.wear_cost_per_kwh
    slot = BatteryGridSlot(
        interval.start, interval.end, interval.price, interval.pv, interval.load,
        grid_charge, solar_stored / efficiency, discharge, energy / capacity * 100, action,
    )
    return _Node(energy, node.cost + cost, node.grid_energy + grid_charge, node, slot), solar_stored


def _slots(node: _Node) -> tuple[BatteryGridSlot, ...]:
    slots = []
    while node.slot is not None:
        slots.append(node.slot)
        node = node.previous
    return tuple(reversed(slots))


def build_battery_grid_plan(
    now: datetime,
    soc: float | None,
    tariff_windows: list[TariffWindow],
    forecast: list[HourlyForecast],
    hourly_load_w: list[float],
    config: BatteryPlanningConfig,
) -> BatteryGridPlan:
    """Plan useful grid energy within contiguous, complete future input data.

    PV always has access to full battery capacity. The grid target is only a
    ceiling for grid charging, not a requested fill level. Invalid data returns
    an empty invalid plan. Freshness is checked by the caller before planning.

    Grid charging may never reduce baseline solar uptake or leave additional
    unused energy at the horizon. This excludes negative-price waste and export
    arbitrage. Export revenue is not modeled. When holding is unavailable, every
    non-charging interval follows native self consumption.
    """
    try:
        _validate_config(config, soc)
        timeline = _timeline(now, tariff_windows, forecast, hourly_load_w, config.horizon_hours)
    except (ValueError, TypeError, AttributeError) as exc:
        return BatteryGridPlan((), 0.0, 0.0, str(exc), 0.0, valid=False)

    efficiency = math.sqrt(config.roundtrip_efficiency)
    initial = _Node(soc / 100 * config.capacity_kwh, 0.0, 0.0, None, None)
    baseline = initial
    baseline_solar = []
    for interval in timeline:
        baseline, solar = _transition(interval, baseline, config, efficiency, "self_consumption")
        baseline_solar.append(solar)

    # The future bounds preserve baseline PV uptake and require all added grid
    # energy to supply forecast household demand before the horizon ends.
    bounds = [0.0] * (len(timeline) + 1)
    bounds[-1] = baseline.energy
    for i in range(len(timeline) - 1, -1, -1):
        interval = timeline[i]
        discharge = min(max(0.0, interval.load - interval.pv), config.max_discharge_power_w * interval.hours / 1000) / efficiency
        bounds[i] = min(config.capacity_kwh, max(0.0, bounds[i + 1] + discharge - baseline_solar[i]))

    # Bound CPU/memory for large batteries without rounding physical balances.
    step = max(config.energy_step_kwh, config.capacity_kwh / 200)
    states = {(round(initial.energy / step), 0): initial}
    grid_ceiling = config.capacity_kwh * config.grid_target_soc / 100
    for i, interval in enumerate(timeline):
        following: dict[tuple[int, int], _Node] = {}

        def retain(candidate: _Node, solar: float) -> None:
            if candidate.energy > bounds[i + 1] + _EPS or solar + _EPS < baseline_solar[i]:
                return
            bucket = round(candidate.energy / step)
            key = (bucket, 0)
            previous = following.get(key)
            if previous is None or (candidate.cost, candidate.grid_energy) < (previous.cost, previous.grid_energy):
                following[key] = candidate
            # Preserve exact upper endpoints as well as the cheapest state in a
            # bucket. Otherwise a partial current slot or a precise demand gap
            # can lose its physically available last few Wh through pruning.
            key = (bucket, 1)
            previous = following.get(key)
            if previous is None or (-candidate.energy, candidate.cost, candidate.grid_energy) < (-previous.energy, previous.cost, previous.grid_energy):
                following[key] = candidate

        for node in states.values():
            native, solar = _transition(interval, node, config, efficiency, "self_consumption")
            retain(native, solar)
            if config.hold_supported and interval.load > interval.pv and native.energy < node.energy - _EPS:
                retained, solar = _transition(interval, node, config, efficiency, "hold")
                retain(retained, solar)
            if interval.price > config.charge_price_limit:
                continue
            held, solar = _transition(interval, node, config, efficiency, "hold")
            maximum_grid = min(
                config.max_charge_power_w * interval.hours / 1000,
                config.native_charge_power_w * interval.hours / 1000 - solar / efficiency,
                (min(grid_ceiling, bounds[i + 1]) - held.energy) / efficiency,
            )
            if maximum_grid <= _EPS:
                continue
            # Include the exact endpoint: small residual demand is useful too.
            options = [j * step for j in range(1, int(maximum_grid / step) + 1)]
            options.append(maximum_grid)
            for grid in options:
                charged, solar = _transition(interval, node, config, efficiency, "charge", grid)
                retain(charged, solar)
        states = following
        if not states:
            # Approximate state pruning must never turn native operation into a
            # fabricated profitable schedule or make valid input unsafe.
            return BatteryGridPlan(_slots(baseline), 0.0, 0.0, "no_economic_benefit", soc)

    best = min(states.values(), key=lambda node: (node.cost, node.grid_energy))
    savings = baseline.cost - best.cost
    if savings <= 1e-6:
        return BatteryGridPlan(_slots(baseline), 0.0, 0.0, "no_economic_benefit", soc)
    slots = _slots(best)
    target = max((slot.soc_end for slot in slots if slot.action == "charge"), default=soc)
    return BatteryGridPlan(slots, best.grid_energy, savings, "economic_plan", target)
