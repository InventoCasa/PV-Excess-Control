"""HA-independent battery forecast scenarios without changing appliance forecasts.

The lower scenario is a sensitivity input, not a calibrated probability or a
promise about available solar energy. In particular, an aggregate of source
p10 estimates is not asserted to be the aggregate system's p10 distribution.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from itertools import pairwise
import math
import re
from typing import Any

from .models import HourlyForecast


@dataclass(frozen=True)
class BatteryForecastScenarios:
    """Calibrated nominal input and optional complete future downside input.

    ``nominal`` preserves all original interval boundaries and applies the
    configured factor once. ``low`` uses source lower estimates without that
    factor, bounded by nominal in each interval. Fully elapsed intervals are
    omitted only from ``low``; a current partial interval keeps its original
    boundaries so the planner performs the same clipping for both scenarios.
    """

    nominal: tuple[HourlyForecast, ...]
    low: tuple[HourlyForecast, ...] | None
    low_status: str
    source_ids: tuple[str, ...] = ()


class _Unavailable(ValueError):
    """An optional scenario cannot be constructed without inventing data."""


def _number(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not forecast energy")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("invalid forecast value") from exc
    if not math.isfinite(result) or result < 0:
        raise ValueError("invalid forecast value")
    return result


def _utc(value: Any) -> datetime:
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid forecast timestamp") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("forecast timestamp requires timezone")
    return result.astimezone(timezone.utc)


def _span(value: Any, default: timedelta) -> timedelta:
    if value is None:
        return default
    if isinstance(value, timedelta):
        seconds = value.total_seconds()
    else:
        match = re.fullmatch(
            r"PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?",
            str(value),
        )
        if match is None:
            raise ValueError("invalid interval duration")
        seconds = sum(float(v or 0) * scale for v, scale in zip(match.groups(), (3600, 60, 1)))
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("invalid interval duration")
    return timedelta(seconds=seconds)


def _source_rows(state: Mapping[str, Any] | None) -> list[tuple[datetime, datetime, Mapping]]:
    if not isinstance(state, Mapping):
        raise _Unavailable("missing_source")
    try:
        _number(state.get("state"))
        attrs = state.get("attributes")
        if not isinstance(attrs, Mapping):
            raise _Unavailable("missing_lower_forecast")
        key = next((key for key in ("forecasts", "detailedForecast", "detailedHourly") if attrs.get(key)), None)
        if key is None:
            raise _Unavailable("missing_lower_forecast")
        values = attrs[key]
        if not isinstance(values, (list, tuple)):
            raise ValueError("forecast intervals must be a sequence")
        default = timedelta(hours=1) if key == "detailedHourly" else timedelta(minutes=30)
        rows = []
        for slot in values:
            if not isinstance(slot, Mapping):
                raise ValueError("invalid forecast interval")
            start = _utc(slot["period_start"]) if slot.get("period_start") is not None else None
            end = _utc(slot["period_end"]) if slot.get("period_end") is not None else None
            span = _span(slot.get("period"), default)
            if start is None and end is not None:
                start = end - span
            if start is None:
                raise ValueError("missing interval start")
            end = end or start + span
            if end <= start:
                raise ValueError("nonpositive forecast interval")
            rows.append((start, end, slot))
        return rows
    except _Unavailable:
        raise
    except (ValueError, TypeError, OverflowError) as exc:
        raise _Unavailable("invalid_lower_forecast") from exc


def build_battery_forecast_scenarios(
    *,
    provider_type: str,
    nominal: Sequence[HourlyForecast],
    states: Mapping[str, Mapping[str, Any]],
    entities: Sequence[str],
    now: datetime,
    factor: float,
    tomorrow_entities: Sequence[str] = (),
) -> BatteryForecastScenarios:
    """Build independent battery scenarios from one coherent source snapshot.

    Optional lower data must cover every contributing source and every future
    nominal interval. Missing, malformed, overlapping or inconsistent source
    data disables that scenario; it never becomes zero solar. Unsupported
    providers retain the nominal forecast with an explicit availability reason.
    Actual provider acquisition times are deliberately supplied/validated by
    the HA adapter, not guessed from entity names or sensor report timestamps.
    """
    try:
        calibration = _number(factor)
        if not .1 <= calibration <= 1:
            raise ValueError
    except ValueError as exc:
        raise ValueError("invalid forecast factor") from exc
    instant = _utc(now)
    validated = []
    consistent_energy = True
    for row in nominal:
        try:
            start, end = _utc(row.start), _utc(row.end)
            if not all(isinstance(value, (int, float)) for value in (row.expected_watts, row.expected_kwh)):
                raise ValueError("invalid nominal number")
            watts, kwh = _number(row.expected_watts), _number(row.expected_kwh)
            if end <= start:
                raise ValueError("nonpositive nominal interval")
            if end > instant and not math.isclose(kwh, watts * (end - start).total_seconds() / 3600000,
                                                  rel_tol=1e-7, abs_tol=1e-8):
                consistent_energy = False
        except (AttributeError, ValueError) as exc:
            raise ValueError("invalid nominal forecast") from exc
        validated.append((start, end, row))
    scaled = tuple(replace(row, expected_kwh=row.expected_kwh * calibration,
                           expected_watts=row.expected_watts * calibration) for row in nominal)
    future = sorted((item for item in validated if item[1] > instant), key=lambda item: item[0])
    if any(a[1] > b[0] for a, b in pairwise(future)):
        raise ValueError("overlapping nominal forecast")
    if not future:
        return BatteryForecastScenarios(scaled, None, "no_future_forecast")
    if provider_type != "solcast":
        return BatteryForecastScenarios(scaled, None, "unsupported_provider")
    if not consistent_energy:
        # Operational planning uses interval energy; an inconsistent redundant
        # power estimate cannot invalidate that existing nominal behavior. It
        # does prevent trustworthy matching against source quantile power.
        return BatteryForecastScenarios(scaled, None, "nominal_power_energy_mismatch")
    if not isinstance(states, Mapping):
        return BatteryForecastScenarios(scaled, None, "missing_source")
    if any(not isinstance(e, str) or not e for e in (*entities, *tomorrow_entities)) or not entities:
        return BatteryForecastScenarios(scaled, None, "invalid_sources")
    primary = tuple(dict.fromkeys(entities))
    tomorrow = tuple(dict.fromkeys(tomorrow_entities))
    source_ids = tuple(dict.fromkeys((*primary, *tomorrow)))
    tomorrow_start = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), now.tzinfo).astimezone(timezone.utc)
    tomorrow_end = datetime.combine(now.date() + timedelta(days=2), datetime.min.time(), now.tzinfo).astimezone(timezone.utc)

    def selected(start):
        return tomorrow if tomorrow and tomorrow_start <= start < tomorrow_end else primary

    try:
        needed = set()
        for start, end, _ in future:
            boundaries = sorted({start, end, *(t for t in (tomorrow_start, tomorrow_end) if start < t < end)})
            for left, _ in pairwise(boundaries):
                needed.update(selected(left))
        sources = {entity: _source_rows(states.get(entity)) for entity in needed}
        low = []
        for start, end, row in future:
            boundaries = sorted({start, end,
                *(t for t in (tomorrow_start, tomorrow_end) if start < t < end),
                *(t for rows in sources.values() for a, b, _ in rows for t in (a, b) if start < t < end)})
            energy = 0.0
            for left, right in pairwise(boundaries):
                normal_watts = lower_watts = 0.0
                for entity in selected(left):
                    matching = [slot for a, b, slot in sources[entity] if a <= left and b >= right]
                    if not matching:
                        raise _Unavailable("incomplete_lower_forecast")
                    if len(matching) > 1:
                        raise _Unavailable("overlapping_lower_forecast")
                    slot = matching[0]
                    if "pv_estimate10" not in slot:
                        raise _Unavailable("missing_lower_estimate")
                    try:
                        normal_watts = _number(normal_watts + _number(slot.get("pv_estimate")) * 1000)
                        lower_watts = _number(lower_watts + _number(slot["pv_estimate10"]) * 1000)
                    except ValueError as exc:
                        raise _Unavailable("invalid_lower_forecast") from exc
                if not math.isclose(normal_watts, row.expected_watts, rel_tol=1e-7, abs_tol=1e-6):
                    raise _Unavailable("source_forecast_mismatch")
                watts = min(lower_watts, row.expected_watts * calibration)
                energy += watts * (right - left).total_seconds() / 3600000
            low.append(replace(row, expected_kwh=energy,
                               expected_watts=energy * 3600000 / (end - start).total_seconds()))
        return BatteryForecastScenarios(scaled, tuple(low), "available", source_ids)
    except _Unavailable as exc:
        return BatteryForecastScenarios(scaled, None, str(exc), source_ids)
