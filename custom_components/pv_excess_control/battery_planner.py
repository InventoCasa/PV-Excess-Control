"""Forecast-aware grid charging with explicit AC/storage energy balances.

This module has no Home Assistant dependencies. A bounded search compares each
schedule with native self consumption. The search groups nearby storage states;
every candidate retains its exact energy, so discretization never creates energy.
The result is an approximate economic schedule, not a guarantee about forecasts.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from itertools import chain
import math

from .models import HourlyForecast, TariffWindow


_EPS = 1e-8
_ECONOMIC_TIE_EPS = 1e-9
# Additional search work is deterministic and cannot displace a completed plan
# with a partially explored trajectory when this budget is exhausted.
_REFINEMENT_TRANSITION_BUDGET = 100_000
_QUANTITY_REFINEMENT_TRANSITION_BUDGET = 100_000
_EPISODE_PRIMARY_TRANSITION_BUDGET = 300_000
_EPISODE_COARSE_TRANSITION_BUDGET = 100_000
_CHARGE_WINDOW_CANDIDATE_LIMIT = 4
_CHARGE_WINDOW_TRANSITION_BUDGET = 25_000
_HOLD_EXCHANGE_CANDIDATE_LIMIT = 4


@dataclass(frozen=True)
class BatteryLoadCommitment:
    """Bounded major-load demand and its battery constraints.

    ``power_w`` is external AC demand excluded from the historical household
    profile. It reserves PV before household/battery allocation. Remaining
    external and household demand share the actual global discharge limit;
    only a zero limit fully prevents battery supply. ``household_power_floor_w``
    describes total house demand
    already represented in that profile: it raises the profile by taking the
    maximum, never by addition. An optional discharge limit also constrains
    native household supply during this interval.
    """

    start: datetime
    end: datetime
    power_w: float
    max_discharge_power_w: float | None = None
    household_power_floor_w: float | None = None


@dataclass(frozen=True)
class BatteryPlanningConfig:
    """Physical limits and prices, expressed in kWh, W, percent and EUR/kWh.

    ``max_charge_power_w`` limits additional grid purchases. The optional
    ``max_pv_charge_power_w`` is the native/shared battery charge limit for all
    sources; when omitted, the same limit applies to both for compatibility.
    The restart delay is an off-time after a grid-charge stop, never a minimum
    on-time. An aware ``grid_charge_not_before`` carries existing restart
    eligibility; ``grid_charge_active`` allows an already running charge to
    continue. A new episode reserves preparation, enable confirmation and the
    requested minimum charging time. ``grid_charge_prepared`` skips only the
    initial preparation. ``grid_charge_minimum_until`` carries the unchanged
    deadline of an already confirmed episode: it may stop immediately or plan
    continuation through that deadline. These are planning constraints, never
    a physical lock against safety stops. Zero defaults preserve pure callers.
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
    forced_charge_min_power_w: float | None = None
    forced_charge_max_power_w: float | None = None
    forced_charge_power_step_w: float | None = None
    grid_restart_delay_minutes: float = 0.0
    grid_charge_not_before: datetime | None = None
    grid_charge_active: bool = False
    grid_charge_minimum_minutes: float = 0.0
    grid_charge_startup_seconds: float = 0.0
    grid_charge_enable_seconds: float = 0.0
    grid_charge_prepared: bool = False
    grid_charge_minimum_until: datetime | None = None

    @property
    def native_charge_power_w(self) -> float:
        """Maximum combined PV and grid input to the battery."""
        return self.max_charge_power_w if self.max_pv_charge_power_w is None else self.max_pv_charge_power_w


@dataclass(frozen=True)
class BatteryGridSlot:
    """A UTC interval with AC energies and PV remaining after external loads."""

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
    reserved_pv_kwh: float = 0.0
    external_load_kwh: float = 0.0
    grid_charge_available_at: datetime | None = None
    startup_pv_loss_kwh: float = 0.0

    @property
    def charging_hours(self) -> float:
        """Actual planned input time, excluding preparation before enable."""
        start = max(self.start, self.grid_charge_available_at or self.start)
        return max(0., (self.end - start).total_seconds() / 3600)

    @property
    def charge_power_w(self) -> float:
        """Mean requested grid charging power for this interval."""
        return self.grid_charge_kwh * 1000 / self.charging_hours if self.charging_hours else 0.

    @property
    def battery_charge_power_w(self) -> float:
        """Total battery input power, including PV, for a forced-charge command."""
        return ((self.grid_charge_kwh + self.pv_charge_kwh) * 1000 / self.charging_hours
                if self.charging_hours else 0.)


@dataclass(frozen=True)
class BatteryGridPlan:
    slots: tuple[BatteryGridSlot, ...]
    grid_energy_kwh: float
    estimated_savings: float
    reason: str
    grid_target_soc: float
    valid: bool = True


@dataclass(frozen=True)
class BatteryChargeEpisode:
    """One contiguous command episode across genuine accounting boundaries."""

    start: datetime
    available_at: datetime
    end: datetime
    target_soc: float
    power_w: float
    slot: BatteryGridSlot
    power_slot: BatteryGridSlot


def charge_episode_at(plan: BatteryGridPlan | None, now: datetime) -> BatteryChargeEpisode | None:
    """Describe the current episode, including an input-free setup interval."""
    if plan is None or not plan.valid:
        return None
    now = _utc(now)
    slots = plan.slots
    index = next((i for i, row in enumerate(slots) if row.start <= now < row.end), None)
    if index is None or slots[index].action != "charge":
        return None
    first = last = index
    while first and slots[first - 1].action == "charge" and slots[first - 1].end == slots[first].start:
        first -= 1
    while last + 1 < len(slots) and slots[last + 1].action == "charge" and slots[last].end == slots[last + 1].start:
        last += 1
    available = slots[first].grid_charge_available_at or slots[first].start
    power_slot = next((row for row in slots[index:last + 1]
                       if row.grid_charge_kwh > _EPS and row.charging_hours > 0), None)
    if power_slot is None:
        return None
    return BatteryChargeEpisode(
        slots[first].start, available, slots[last].end, slots[last].soc_end,
        power_slot.battery_charge_power_w, slots[index], power_slot,
    )


@dataclass(frozen=True, slots=True)
class _Interval:
    start: datetime
    end: datetime
    price: float
    pv: float
    load: float
    reserved_pv: float = 0.0
    max_discharge_power_w: float | None = None
    external_load: float = 0.0
    elapsed_hours: float = 0.0

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600

    @property
    def external_grid_deficit_kwh(self) -> float:
        return max(0.0, self.external_load - self.reserved_pv)

    @property
    def deficit_kwh(self) -> float:
        """Both load classes share the physical battery's discharge limit."""
        return max(0.0, self.load - self.pv) + self.external_grid_deficit_kwh

    def discharge_limit(self, config: BatteryPlanningConfig) -> float:
        """The tightest native or concurrently active external-load limit."""
        if self.max_discharge_power_w is None:
            return config.max_discharge_power_w
        return min(config.max_discharge_power_w, self.max_discharge_power_w)


@dataclass(frozen=True, slots=True)
class _Node:
    energy: float
    cost: float
    grid_energy: float
    previous: _Node | None
    slot: BatteryGridSlot | None
    charge_moment: float = 0.0
    charge_active: bool = False
    charge_not_before: datetime | None = None
    charge_available_at: datetime | None = None
    charge_minimum_until: datetime | None = None
    startup_pv_loss: float = 0.0
    charge_grid_delivered: bool = False


def _prefer_economic_node(candidate: _Node, previous: _Node) -> bool:
    """Break numerical economic ties by earlier useful grid purchases.

    Constant external-load costs must not rearrange equivalent schedules through
    float summation noise. Cost and total grid energy remain primary; the timing
    moment is measured from this horizon's start, not from an absolute epoch.
    """
    for left, right in ((candidate.cost, previous.cost), (candidate.grid_energy, previous.grid_energy)):
        if abs(left - right) > _ECONOMIC_TIE_EPS:
            return left < right
    return candidate.charge_moment < previous.charge_moment


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
        config.grid_restart_delay_minutes,
        config.grid_charge_minimum_minutes, config.grid_charge_startup_seconds,
        config.grid_charge_enable_seconds,
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
        and 0 <= config.grid_restart_delay_minutes <= 60
        and isinstance(config.grid_charge_active, bool)
        and 0 <= config.grid_charge_minimum_minutes <= 60
        and 0 <= config.grid_charge_startup_seconds <= 300
        and 0 <= config.grid_charge_enable_seconds <= 300
        and isinstance(config.grid_charge_prepared, bool)
    ):
        raise ValueError("invalid_configuration")
    if config.grid_charge_not_before is not None:
        _utc(config.grid_charge_not_before)
    if config.grid_charge_minimum_until is not None:
        _utc(config.grid_charge_minimum_until)
    minimum = config.forced_charge_min_power_w
    maximum = config.forced_charge_max_power_w
    step = config.forced_charge_power_step_w
    if (any(value is not None and not _finite(value) for value in (minimum, maximum, step))
            or (minimum is not None and minimum < 0)
            or (maximum is not None and maximum <= 0)
            or (step is not None and step <= 0)
            or (minimum is not None and maximum is not None and minimum > maximum)):
        raise ValueError("invalid_charge_power_constraints")


def _timeline(
    now: datetime,
    tariffs: list[TariffWindow],
    forecast: list[HourlyForecast],
    load_w: list[float],
    horizon_hours: float,
    load_commitments: tuple[BatteryLoadCommitment, ...] = (),
    *, grid_charge_not_before: datetime | None = None,
    protected_instants: tuple[datetime, ...] = (),
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
    protected_boundaries = {start, end}
    for instant in protected_instants:
        instant = _utc(instant)
        if start < instant < end:
            boundaries.add(instant)
            protected_boundaries.add(instant)
    if grid_charge_not_before is not None:
        eligible = _utc(grid_charge_not_before)
        if start < eligible < end:
            boundaries.add(eligible)
            protected_boundaries.add(eligible)
    commitments = []
    for row in load_commitments:
        row_start, row_end = _utc(row.start), _utc(row.end)
        cap = row.max_discharge_power_w
        floor = row.household_power_floor_w
        if (
            row_end <= row_start
            or not _finite(row.power_w)
            or row.power_w < 0
            or (cap is not None and (not _finite(cap) or cap < 0))
            or (floor is not None and (not _finite(floor) or floor < 0))
        ):
            raise ValueError("invalid_load_commitment")
        if row_end > start and row_start < end:
            clipped_start, clipped_end = max(start, row_start), min(end, row_end)
            commitments.append((clipped_start, clipped_end, row.power_w, cap, floor))
            boundaries.update((clipped_start, clipped_end))
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
            protected_boundaries.update((clipped_start, clipped_end))
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
            protected_boundaries.add(hour)
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
        pv = solar[si][2] * duration / solar_duration
        active = [row for row in commitments if row[0] <= left < row[1]]
        external_load = sum(row[2] for row in active) * duration / 3_600_000
        reserved_pv = min(pv, external_load)
        caps = [row[3] for row in active if row[3] is not None]
        floors = [row[4] for row in active if row[4] is not None]
        household_power = max(load_w[left.astimezone(now.tzinfo).hour], max(floors, default=0))
        interval = _Interval(
            left, right, prices[pi][2], pv - reserved_pv,
            household_power * duration / 3_600_000,
            reserved_pv, min(caps) if caps else None, external_load,
            (left - start).total_seconds() / 3600,
        )
        # Only load metadata may introduce removable boundaries. Price, solar,
        # local-hour and restart-eligibility edges remain authoritative even
        # when their adjacent numeric values happen to match.
        if (result and left not in protected_boundaries
                and _same_physical_rates(result[-1], interval)):
            previous = result[-1]
            result[-1] = replace(
                previous, end=right, pv=previous.pv + interval.pv,
                load=previous.load + interval.load,
                reserved_pv=previous.reserved_pv + interval.reserved_pv,
                external_load=previous.external_load + interval.external_load,
            )
        else:
            result.append(interval)
    return result


def _same_physical_rates(left: _Interval, right: _Interval) -> bool:
    """Ignore only duration arithmetic noise, never a genuine load/cap change."""
    return (
        left.price == right.price
        and left.max_discharge_power_w == right.max_discharge_power_w
        and all(math.isclose(a / left.hours, b / right.hours, rel_tol=1e-12, abs_tol=1e-12)
                for a, b in ((left.pv, right.pv), (left.load, right.load),
                             (left.reserved_pv, right.reserved_pv),
                             (left.external_load, right.external_load)))
    )


def _charge_timing(
    interval: _Interval, node: _Node, config: BatteryPlanningConfig,
) -> tuple[datetime | None, datetime | None]:
    """Return the enable instant and minimum end of this planned episode."""
    if node.charge_active:
        return node.charge_available_at, node.charge_minimum_until
    prepared = config.grid_charge_prepared and node.slot is None
    seconds = 0. if prepared else config.grid_charge_startup_seconds
    if not seconds and not config.grid_charge_minimum_minutes and not config.grid_charge_enable_seconds:
        return None, None
    available = interval.start + timedelta(seconds=seconds)
    minimum = available + timedelta(minutes=config.grid_charge_minimum_minutes,
                                    seconds=config.grid_charge_enable_seconds)
    return available, minimum


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
    deficit = interval.deficit_kwh
    natural_solar = min(surplus, config.native_charge_power_w * interval.hours / 1000, max(0.0, capacity - node.energy) / efficiency) * efficiency
    available = minimum = None
    solar_stored = natural_solar
    startup_loss = 0.
    if action == "charge":
        available, minimum = _charge_timing(interval, node, config)
        active_hours = max(0., (interval.end - max(interval.start, available or interval.start)).total_seconds() / 3600)
        if active_hours < interval.hours:
            # Forced-mode preparation can suspend native PV charging. Account
            # for that interval explicitly instead of inventing solar uptake.
            solar_stored = min(
                surplus * active_hours / interval.hours,
                config.native_charge_power_w * active_hours / 1000,
                max(0., capacity - node.energy) / efficiency,
            ) * efficiency
            startup_loss = (natural_solar - solar_stored) / efficiency
    energy = node.energy + solar_stored + grid_charge * efficiency
    discharge = 0.0
    if action == "self_consumption":
        discharge = min(deficit, interval.discharge_limit(config) * interval.hours / 1000, max(0.0, energy - reserve) * efficiency)
        energy -= discharge / efficiency
    import_kwh = deficit - discharge + grid_charge
    cost = import_kwh * interval.price + discharge * config.wear_cost_per_kwh
    slot = BatteryGridSlot(
        interval.start, interval.end, interval.price, interval.pv, interval.load,
        grid_charge, solar_stored / efficiency, discharge, energy / capacity * 100, action,
        interval.reserved_pv, interval.external_load,
        available, startup_loss,
    )
    not_before = node.charge_not_before
    if node.charge_active and action != "charge" and config.grid_restart_delay_minutes:
        stopped_until = interval.start + timedelta(minutes=config.grid_restart_delay_minutes)
        not_before = max(not_before, stopped_until) if not_before is not None else stopped_until
    if not_before is not None and not_before <= interval.end:
        not_before = None
    return _Node(
        energy, node.cost + cost, node.grid_energy + grid_charge, node, slot,
        node.charge_moment + grid_charge * interval.elapsed_hours,
        action == "charge", not_before,
        available if available is not None and available > interval.end else None,
        minimum if minimum is not None and minimum > interval.end else None,
        node.startup_pv_loss + startup_loss * efficiency,
        action == "charge" and (node.charge_grid_delivered or grid_charge > _EPS),
    ), solar_stored


def _slots(node: _Node) -> tuple[BatteryGridSlot, ...]:
    slots = []
    while node.slot is not None:
        slots.append(node.slot)
        node = node.previous
    return tuple(reversed(slots))


def _add_grid_charge(
    previous: _Node, prepared: _Node, config: BatteryPlanningConfig,
    efficiency: float, grid: float, elapsed_hours: float,
) -> _Node:
    """Reuse one interval's physical preparation balance for its quantities."""
    base = prepared.slot
    energy = prepared.energy + grid * efficiency
    slot = BatteryGridSlot(
        base.start, base.end, base.price, base.pv_kwh, base.load_kwh,
        grid, base.pv_charge_kwh, 0., energy / config.capacity_kwh * 100,
        "charge", base.reserved_pv_kwh, base.external_load_kwh,
        base.grid_charge_available_at, base.startup_pv_loss_kwh,
    )
    return _Node(
        energy, prepared.cost + grid * base.price, prepared.grid_energy + grid,
        previous, slot, prepared.charge_moment + grid * elapsed_hours, True,
        prepared.charge_not_before, prepared.charge_available_at,
        prepared.charge_minimum_until, prepared.startup_pv_loss, True,
    )


def _executable_grid_energy(
    interval: _Interval, config: BatteryPlanningConfig, solar_ac: float, grid: float,
    *, charging_hours: float | None = None,
) -> float:
    """Quantize the combined PV/grid command without purchasing extra energy.

    The native PV model is unchanged. An actuator limit that would curtail that
    solar input excludes this forced-charge candidate instead of hiding PV loss.
    """
    minimum = config.forced_charge_min_power_w or 0.
    maximum = config.forced_charge_max_power_w
    step = config.forced_charge_power_step_w
    if config.forced_charge_min_power_w is None and maximum is None and step is None:
        return grid
    hours = interval.hours if charging_hours is None else charging_hours
    if hours <= 0:
        return 0.
    requested = (solar_ac + grid) * 1000 / hours
    watts = min(requested, maximum) if maximum is not None else requested
    if step is not None:
        watts = minimum + math.floor((watts - minimum + 1e-9) / step) * step
    if watts <= 0 or watts < minimum:
        return 0.
    return max(0., min(grid, watts * hours / 1000 - solar_ac))


def _search_schedule(
    timeline: list[_Interval], config: BatteryPlanningConfig, efficiency: float,
    initial: _Node, baseline_solar: list[float], bounds: list[float],
    step: float, bucket_step: float, *, transition_budget: int | None = None,
    baseline_energy: list[float] | None = None,
    charge_window: tuple[int, int] | None = None,
) -> _Node | None:
    """Search exact states, optionally requiring one bounded charging window.

    A fixed window explores only its executable upper charge endpoints. Keeping
    it separate prevents cheaper partial purchases from pruning that complete
    episode before its stored energy can serve a later peak. All physical,
    solar, timing and terminal guards remain common to both search modes.
    """
    attempts = 0
    states = {(round(initial.energy / bucket_step), 0): initial}
    starts = [interval.start for interval in timeline]
    restart_delay = timedelta(minutes=config.grid_restart_delay_minutes)
    short_intervals = [interval.end - interval.start < restart_delay for interval in timeline]
    episode_constraints = bool(config.grid_charge_minimum_minutes or config.grid_charge_startup_seconds
                               or config.grid_charge_enable_seconds or initial.charge_minimum_until)
    reserve = config.capacity_kwh * config.reserve_soc / 100
    if baseline_energy is None:
        baseline_energy = []
        native_trace = initial
        for row in timeline:
            native_trace, _ = _transition(row, native_trace, config, efficiency, "self_consumption")
            baseline_energy.append(native_trace.energy)
    native_margin = [max(0., energy - reserve) for energy in baseline_energy]
    future_margin = list(native_margin)
    for index in range(len(future_margin) - 2, -1, -1):
        future_margin[index] = min(future_margin[index], future_margin[index + 1])
    end = timeline[-1].end
    grid_ceiling = config.capacity_kwh * config.grid_target_soc / 100
    for i, interval in enumerate(timeline):
        following: dict[tuple, _Node] = {}
        required_charge = charge_window is not None and charge_window[0] <= i < charge_window[1]

        def loss_debt(candidate: _Node, solar: float) -> float:
            # Only lost baseline uptake can be unused terminal inventory. Loss
            # spent before a later native reserve hit must not debit that later
            # refill. Extra later PV uptake is conservatively not credited.
            if not candidate.startup_pv_loss:
                return 0.
            physical_loss = candidate.slot.startup_pv_loss_kwh * efficiency
            baseline_loss = min(physical_loss, max(0., baseline_solar[i] - solar))
            outstanding = max(0., candidate.startup_pv_loss - physical_loss + baseline_loss)
            return min(outstanding, native_margin[i])

        def retain(candidate: _Node, solar: float) -> None:
            outstanding = loss_debt(candidate, solar)
            if outstanding != candidate.startup_pv_loss:
                candidate = replace(candidate, startup_pv_loss=outstanding)
            debit = min(outstanding, future_margin[i])
            # Only actual setup loss at this same prior energy may relax the
            # interval solar guard. Later capacity crowding remains forbidden.
            setup_loss = candidate.slot.startup_pv_loss_kwh * efficiency
            if (candidate.energy > max(0., bounds[i + 1] - debit) + _EPS
                    or solar + setup_loss + _EPS < baseline_solar[i]):
                return
            if i == len(timeline) - 1 and candidate.charge_active and (
                not candidate.charge_grid_delivered or candidate.charge_minimum_until is not None
                or candidate.charge_available_at is not None
            ):
                return
            bucket = round(candidate.energy / bucket_step)
            # Two equal-energy states are not interchangeable if one may keep
            # charging or restart earlier. Deadlines with the same next usable
            # interval are equivalent; expired deadlines collapse to one class.
            # When the next entire interval already spans the off-delay, both
            # states may charge immediately or stop for that interval and then
            # restart. Keeping their active flags separate adds no capability.
            active_matters = (episode_constraints or candidate.charge_not_before is not None
                              or (i + 1 < len(timeline) and short_intervals[i + 1]))
            active = candidate.charge_active if active_matters else False
            eligible = (bisect_left(starts, candidate.charge_not_before, lo=i + 1)
                        if candidate.charge_not_before is not None else i + 1)
            minimum = (bisect_left(starts, candidate.charge_minimum_until, lo=i + 1)
                       if candidate.charge_minimum_until is not None else i + 1)
            phase = ((candidate.charge_available_at, minimum, candidate.charge_grid_delivered)
                     if episode_constraints and candidate.charge_active and (
                         candidate.charge_available_at is not None
                         or candidate.charge_minimum_until is not None
                         or not candidate.charge_grid_delivered
                     ) else None)
            loss_bucket = round(debit / bucket_step) if future_margin[i] else 0
            key = (bucket, 0, active, eligible, phase, loss_bucket)
            previous = following.get(key)
            if previous is None or _prefer_economic_node(candidate, previous):
                following[key] = candidate
            # Preserve exact upper endpoints as well as the cheapest state in a
            # bucket. Otherwise a partial current slot or a precise demand gap
            # can lose its physically available last few Wh through pruning.
            key = (bucket, 1, active, eligible, phase, loss_bucket)
            previous = following.get(key)
            if (previous is None or candidate.energy > previous.energy
                    or (candidate.energy == previous.energy and _prefer_economic_node(candidate, previous))):
                following[key] = candidate

        # The cheapest and upper-endpoint keys often refer to the same node.
        # Explore that identical trajectory once; retain both distinct choices.
        for node in {id(value): value for value in states.values()}.values():
            # Count conservatively before each node's native/hold transitions.
            attempts += 3
            if transition_budget is not None and attempts > transition_budget:
                return None
            unfinished = node.charge_active and (
                not node.charge_grid_delivered
                or (node.charge_minimum_until is not None and interval.start < node.charge_minimum_until)
                or (node.charge_available_at is not None and interval.start < node.charge_available_at)
            )
            # A fresh economic evaluation may stop an existing real episode
            # immediately. Choosing continuation carries its original deadline;
            # it may not become another short planned pulse at a later subslot.
            may_stop_now = node.slot is None and config.grid_charge_active
            if (not unfinished or may_stop_now) and not required_charge:
                native, solar = _transition(interval, node, config, efficiency, "self_consumption")
                retain(native, solar)
                if config.hold_supported and interval.deficit_kwh > 0 and native.energy < node.energy - _EPS:
                    retained, solar = _transition(interval, node, config, efficiency, "hold")
                    retain(retained, solar)
            if ((charge_window is not None and not required_charge)
                    or interval.price > config.charge_price_limit
                    or (not node.charge_active and node.charge_not_before is not None
                        and interval.start < node.charge_not_before)):
                continue
            available, minimum = _charge_timing(interval, node, config)
            if minimum is not None and minimum > end:
                continue
            held, solar = _transition(interval, node, config, efficiency, "charge")
            hours = held.slot.charging_hours
            if hours <= 0:
                retain(held, solar)
                continue
            debit = min(loss_debt(held, solar), future_margin[i])
            maximum_grid = min(
                config.max_charge_power_w * hours / 1000,
                config.native_charge_power_w * hours / 1000 - solar / efficiency,
                (min(grid_ceiling, max(0., bounds[i + 1] - debit)) - held.energy) / efficiency,
            )
            if maximum_grid <= _EPS:
                continue
            # Include the exact endpoint: small residual demand is useful too.
            options = chain(
                (() if charge_window is not None else
                 (j * step for j in range(1, int(maximum_grid / step) + 1))),
                (maximum_grid,),
            )
            seen = set()
            for grid in options:
                attempts += 1
                if transition_budget is not None and attempts > transition_budget:
                    return None
                grid = _executable_grid_energy(interval, config, solar / efficiency, grid, charging_hours=hours)
                if grid <= _EPS or grid in seen:
                    continue
                seen.add(grid)
                charged = _add_grid_charge(node, held, config, efficiency, grid, interval.elapsed_hours)
                retain(charged, solar)
        states = following
        if not states:
            return None

    best = next(iter(states.values()))
    for candidate in states.values():
        if _prefer_economic_node(candidate, best):
            best = candidate
    return best


def _charge_windows(
    timeline: list[_Interval], config: BatteryPlanningConfig, initial: _Node,
) -> list[tuple[int, int]]:
    """Select a few cheap windows long enough for an executable new episode.

    Real price/load boundaries remain intact. A short starting interval may
    extend across adjacent eligible intervals to cover preparation and minimum
    input time; search transitions validate the resulting physical trajectory.
    """
    windows = []
    duration = timedelta(minutes=config.grid_charge_minimum_minutes,
                         seconds=config.grid_charge_startup_seconds + config.grid_charge_enable_seconds)
    for index, row in enumerate(timeline):
        if (row.price > config.charge_price_limit
                or (initial.charge_not_before is not None and row.start < initial.charge_not_before
                    and not (index == 0 and initial.charge_active))):
            continue
        minimum = row.start + duration
        if index == 0:
            _, minimum = _charge_timing(row, initial, config)
            minimum = minimum or row.start
        end = index + 1
        while timeline[end - 1].end < minimum and end < len(timeline):
            end += 1
        if (timeline[end - 1].end >= minimum
                and all(part.price <= config.charge_price_limit for part in timeline[index:end])):
            windows.append((index, end))
    windows.sort(key=lambda window: (timeline[window[0]].price, window[0]))
    return windows[:_CHARGE_WINDOW_CANDIDATE_LIMIT]


def _improve_current_hold(
    timeline: list[_Interval], config: BatteryPlanningConfig, efficiency: float,
    initial: _Node, incumbent: _Node, baseline_solar: list[float], bounds: list[float],
) -> _Node:
    """Check a few complete alternatives to a quantized current hold.

    Nearby energy buckets can lose a cheaper native-first trajectory. Replaying
    the completed suffix checks that alternative without another broad search.
    An equal-energy exchange with a cheaper later discharge also preserves the
    inventory intended for more expensive demand. Only native/hold plans are
    eligible: preparation, active charging and future charge commands retain
    their existing validated trajectories and timing constraints.
    """
    slots = _slots(incumbent)
    if (not config.hold_supported or config.grid_charge_active or config.grid_charge_prepared
            or not slots or slots[0].action != "hold"
            or any(slot.action == "charge" for slot in slots)):
        return incumbent

    def replay(exchange: int | None) -> _Node | None:
        node = initial
        for index, (interval, slot) in enumerate(zip(timeline, slots)):
            parts = [(interval, "self_consumption" if index == 0 else slot.action)]
            if index == exchange:
                previous_energy = slots[index - 1].soc_end * config.capacity_kwh / 100
                debt_ac = (previous_energy - node.energy) * efficiency
                possible = min(interval.deficit_kwh, interval.discharge_limit(config) * interval.hours / 1000)
                # No solar charging inside the exchanged interval: a prefix
                # hold then native discharge can restore its exact endpoint.
                if (debt_ac <= _EPS or debt_ac > slot.discharge_kwh + _EPS
                        or possible <= _EPS or interval.pv > interval.load):
                    return None
                native_fraction = max(0., slot.discharge_kwh - debt_ac) / possible
                edge = interval.end - (interval.end - interval.start) * native_fraction
                if edge <= interval.start:
                    return None
                if edge >= interval.end:
                    parts = [(interval, "hold")]
                else:
                    # Scale every extensive quantity by the actual rounded
                    # timestamp so the split retains the original power rates.
                    fraction = (edge - interval.start) / (interval.end - interval.start)
                    held = replace(interval, end=edge, pv=interval.pv * fraction,
                                   load=interval.load * fraction, reserved_pv=interval.reserved_pv * fraction,
                                   external_load=interval.external_load * fraction)
                    native = replace(interval, start=edge, pv=interval.pv * (1 - fraction),
                                     load=interval.load * (1 - fraction),
                                     reserved_pv=interval.reserved_pv * (1 - fraction),
                                     external_load=interval.external_load * (1 - fraction),
                                     elapsed_hours=interval.elapsed_hours + (edge - interval.start).total_seconds() / 3600)
                    parts = [(held, "hold"), (native, "self_consumption")]
            solar = 0.
            for part, action in parts:
                node, captured = _transition(part, node, config, efficiency, action)
                solar += captured
                if not -_EPS <= node.energy <= config.capacity_kwh + _EPS:
                    return None
            # Preserve the existing native-solar, future-use and terminal
            # bounds. No setup-loss allowance is needed without grid charging.
            if node.energy > bounds[index + 1] + _EPS or solar + _EPS < baseline_solar[index]:
                return None
            if index == exchange and abs(node.energy - slot.soc_end * config.capacity_kwh / 100) > _EPS:
                return None
        return node

    exchanges = sorted(
        (index for index, slot in enumerate(slots[1:], 1)
         if slot.action == "self_consumption" and slot.discharge_kwh > _EPS
         and slot.price < slots[0].price),
        key=lambda index: (slots[index].price, index),
    )[:_HOLD_EXCHANGE_CANDIDATE_LIMIT]
    best = incumbent
    for exchange in [None, *exchanges]:
        candidate = replay(exchange)
        if candidate is not None and candidate.cost < best.cost - _ECONOMIC_TIE_EPS:
            best = candidate
    return best


def build_battery_grid_plan(
    now: datetime,
    soc: float | None,
    tariff_windows: list[TariffWindow],
    forecast: list[HourlyForecast],
    hourly_load_w: list[float],
    config: BatteryPlanningConfig,
    *,
    load_commitments: tuple[BatteryLoadCommitment, ...] = (),
    continuation_plan: BatteryGridPlan | None = None,
) -> BatteryGridPlan:
    """Plan useful grid energy within contiguous, complete future input data.

    PV always has access to full battery capacity. The grid target is only a
    ceiling for grid charging, not a requested fill level. Invalid data returns
    an empty invalid plan. Freshness is checked by the caller before planning.

    Grid charging may not crowd out baseline solar uptake or leave additional
    unused energy at the horizon. Only unavoidable preparation loss at the same
    starting energy is admitted and explicitly accounted; its remaining unused
    baseline inventory tightens the terminal bound. This excludes negative-price
    waste and export arbitrage. Export revenue is not modeled. When holding is
    unavailable, every non-charging interval follows native self consumption.

    Bounded external loads reserve their share of forecast PV exactly once.
    Their remaining demand can receive native battery discharge up to the actual
    global limit, including any residual leakage under a small nonzero cap.
    Their grid purchases are included in candidate and baseline costs. They do
    not become permanent household demand after their bounded interval ends.
    Known house demand can raise the historical profile for a bounded interval;
    overlapping total house floors combine by maximum to avoid double counting.
    """
    try:
        _validate_config(config, soc)
        minimum_until = (_utc(config.grid_charge_minimum_until)
                         if config.grid_charge_active and config.grid_charge_minimum_until is not None else None)
        if minimum_until is not None and minimum_until <= _utc(now):
            minimum_until = None
        controls = (() if minimum_until is None else (
            minimum_until, _utc(now) + timedelta(minutes=config.grid_restart_delay_minutes),
        ))
        timeline = _timeline(
            now, tariff_windows, forecast, hourly_load_w, config.horizon_hours,
            load_commitments, grid_charge_not_before=config.grid_charge_not_before,
            protected_instants=controls,
        )
    except (ValueError, TypeError, AttributeError) as exc:
        return BatteryGridPlan((), 0.0, 0.0, str(exc), 0.0, valid=False)

    efficiency = math.sqrt(config.roundtrip_efficiency)
    initial = _Node(
        soc / 100 * config.capacity_kwh, 0.0, 0.0, None, None,
        charge_active=config.grid_charge_active,
        charge_not_before=_utc(config.grid_charge_not_before) if config.grid_charge_not_before is not None else None,
        charge_grid_delivered=config.grid_charge_active,
        charge_minimum_until=minimum_until,
    )
    baseline = initial
    baseline_solar = []
    baseline_energy = []
    for interval in timeline:
        baseline, solar = _transition(interval, baseline, config, efficiency, "self_consumption")
        baseline_solar.append(solar)
        baseline_energy.append(baseline.energy)

    # The future bounds preserve baseline PV uptake and require all added grid
    # energy to supply forecast household demand before the horizon ends.
    bounds = [0.0] * (len(timeline) + 1)
    bounds[-1] = baseline.energy
    for i in range(len(timeline) - 1, -1, -1):
        interval = timeline[i]
        discharge = min(interval.deficit_kwh, interval.discharge_limit(config) * interval.hours / 1000) / efficiency
        bounds[i] = min(config.capacity_kwh, max(0.0, bounds[i + 1] + discharge - baseline_solar[i]))

    # Keep both established searches and every completed incumbent. Smaller
    # purchases are considered only with verified hold support, so they cannot
    # introduce an unverified pseudo-hold through tiny forced-charge commands.
    step = max(config.energy_step_kwh, config.capacity_kwh / 200)
    best = baseline
    constrained = bool(config.grid_charge_minimum_minutes or config.grid_charge_startup_seconds
                       or config.grid_charge_enable_seconds or minimum_until)
    # A small full-horizon candidate provides a useful incumbent even when a
    # dense sequence of real boundaries exhausts the finer searches' budgets.
    coarse_step = max(step, config.capacity_kwh / 8)
    searches = ([(coarse_step, coarse_step, _EPISODE_COARSE_TRANSITION_BUDGET)]
                if constrained else [])
    searches += [(step, step, _EPISODE_PRIMARY_TRANSITION_BUDGET if constrained else None),
                 (step, step / 2, _REFINEMENT_TRANSITION_BUDGET)]
    if config.hold_supported:
        searches.append((step / 2, step / 2, _QUANTITY_REFINEMENT_TRANSITION_BUDGET))
    for quantity_step, bucket_step, budget in searches:
        candidate = _search_schedule(
            timeline, config, efficiency, initial, baseline_solar, bounds,
            quantity_step, bucket_step, transition_budget=budget, baseline_energy=baseline_energy,
        )
        if candidate is not None and _prefer_economic_node(candidate, best):
            best = candidate
    if constrained and config.hold_supported:
        # At most four additional searches of 25,000 transitions each. They
        # retain the established incumbents even when a window is infeasible
        # or its suffix cannot finish within the deterministic work budget.
        for window in _charge_windows(timeline, config, initial):
            candidate = _search_schedule(
                timeline, config, efficiency, initial, baseline_solar, bounds,
                step, step / 2, transition_budget=_CHARGE_WINDOW_TRANSITION_BUDGET,
                baseline_energy=baseline_energy, charge_window=window,
            )
            if candidate is not None and _prefer_economic_node(candidate, best):
                best = candidate
    if continuation_plan is not None:
        from .battery_continuation import replay_continuation

        candidate = replay_continuation(timeline, config, efficiency, initial, continuation_plan)
        if candidate is not None and _prefer_economic_node(candidate, best):
            best = candidate
    best = _improve_current_hold(timeline, config, efficiency, initial, best, baseline_solar, bounds)
    savings = baseline.cost - best.cost
    if savings <= 1e-6:
        return BatteryGridPlan(_slots(baseline), 0.0, 0.0, "no_economic_benefit", soc)
    slots = _slots(best)
    target = max((slot.soc_end for slot in slots if slot.action == "charge"), default=soc)
    return BatteryGridPlan(slots, best.grid_energy, savings, "economic_plan", target)
